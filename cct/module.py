"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the MIT license. See the LICENSE file for details.
"""
import hashlib
import imp
import inspect
import glob
import logging
import os
import re
import shlex
import string
import subprocess
import yaml

from pkg_resources import resource_string, resource_filename

from cct.errors import CCTError
from cct.lib.git import clone_repo

try:
    import urllib.request as urlrequest
except ImportError:
    import urllib as urlrequest

logger = logging.getLogger('cct')


class ModuleManager(object):
    modules = {}

    def __init__(self, directory):
        self.directory = directory
        self.modules['base.Dummy'] = Dummy('base.Dummy', None)

    def discover_modules(self, directory=None):
        directory = directory if directory is not None else self.directory
        module_dirs = []
        for root, _, files in os.walk(directory):
            if 'module.yaml' in files:
                module_dirs.append(root)

        for mod_dir in module_dirs:
            try:
                with open(os.path.join(mod_dir, "module.yaml")) as stream:
                    config = yaml.load(stream)
                    if 'deps' in config:
                        self.process_module_deps(config['deps'])
                    self.find_modules(mod_dir, config['language'])
            except Exception as ex:
                logger.debug("Cannot process module.yaml %s" % ex, exc_info=True)

    def install_module(self, url, version):
        repo_dir = "%s/%s" % (self.directory, os.path.basename(url))
        if repo_dir.endswith('git'):
            repo_dir = repo_dir[:-4]
        logger.info("Cloning module into %s" % repo_dir)
        clone_repo(url, repo_dir, version)
        self.discover_modules(repo_dir)

    def process_module_deps(self, deps):
        for dep in deps:
            logger.debug("Fetching module from %s" % dep['url'])
            self.install_module(dep['url'], dep['version'] if 'version' in deps else None)

    def find_modules(self, directory, language):
        """
        Finds all modules in the subdirs of directory
        """
        if not os.path.exists(directory):
            return {}

        logger.debug("discovering modules in %s" % directory)

        if 'bash' in language:
            pattern = os.path.join(os.path.abspath(directory), '*.sh')
            for candidate in glob.glob(pattern):
                self.check_module_sh(candidate)
        else:
            pattern = os.path.join(os.path.abspath(directory), '*.py')
            for candidate in glob.glob(pattern):
                self.check_module_py(candidate)

    def check_module_py(self, candidate):
        module_name = "cct.module." + os.path.dirname(candidate).split('/')[-1]
        logger.debug("importing module %s to %s" % (os.path.abspath(candidate), module_name))
        module = imp.load_source(module_name, os.path.abspath(candidate))
        # Get all classes from our module
        for name, clazz in inspect.getmembers(module, inspect.isclass):
            # Check that class is from our namespace
            if module_name == clazz.__module__:
                # Instantiate class
                cls = getattr(module, name)
                if issubclass(cls, Module):
                    self.modules[module_name.split('.')[-1] + "." + cls.__name__] = cls(module_name.split('.')[-1] + "." + cls.__name__, os.path.dirname(candidate))

    def check_module_sh(self, candidate):
        module_name = "cct.module." + os.path.dirname(candidate).split('/')[-1]
        logger.debug("importing module %s to %s" % (os.path.abspath(candidate), module_name))
        name = module_name.split('.')[-1] + "." + os.path.basename(candidate)[:-3]
        self.modules[name] = ShellModule(name, os.path.dirname(candidate), candidate)

    def list(self):
        print("available cct modules:")
        for module, _ in self.modules.iteritems():
            print("  %s" % module)

    def list_module_oper(self, name):
        module = None
        if name not in self.modules:
            print("Module %s cannot be found!" % name)
            exit(1)
        else:
            module = self.modules[name]
        print("Module %s contains commands: " % name)

        if getattr(module, "setup").__doc__:
            print("  setup: %s " % getattr(module, "setup").__doc__)

        for method in dir(module):
            if callable(getattr(module, method)):
                if method[0] in string.ascii_lowercase and method not in ['run', 'setup', 'url', 'version', 'teardown']:
                    print("  %s: %s" % (method, getattr(module, method).__doc__))

        if getattr(module, "teardown").__doc__:
            print("  teardown: %s " % getattr(module, "teardown").__doc__)


class ModuleRunner(object):
    def __init__(self, module):
        self.module = module
        self.state = "Processing"

    def run(self):
        self.module.instance.setup()
        for operation in self.module.operations:
            if operation.command in ['setup', 'run', 'url', 'version', 'teardown']:
                continue
            self.module.instance._process_environment(operation)
            try:
                logger.debug("executing module %s operation %s with args %s" % (self.module.name, operation.command, operation.args))
                self.module.instance.run(operation)
                self.state = "Passed"
            except Exception as e:
                self.state = "Error"
                logger.error("module %s cannot execute %s with args %s" % (self.module.name, operation.command, operation.args))
                logger.debug(e, exc_info=True)
                raise e
        self.module.instance.teardown()
        self.state = "Passed"


class Module(object):
    artifacts = {}

    def __init__(self, name, directory):
        self.name = name
        self.environment = {}
        self.deps = []
        self.operations = []
        self.instance = None
        self.state = "NotRun"
        self.logger = logger
        if not directory:
            return
        try:
            with open(os.path.join(directory, "module.yaml")) as stream:
                config = yaml.load(stream)
                if 'artifacts' in config:
                    self._get_artifacts(config['artifacts'], directory)
        except Exception as ex:
            logger.debug("Cannot process module.yaml %s" % ex, exc_info=True)

    def getenv(self, name, default=None):
        if os.environ.get(name):
            return os.environ.get(name)
        if name in self.environment:
            return self.environment[name]
        return default

    def _update_env(self, env):
        self.environment.update(env)

    def _process_operations(self, ops):
        for op in ops:
            for name, args in op.items():
                logger.debug("processing operation %s with args '%s'" % (name, args))
                if name == "environment":
                    self._update_env(self._create_env_dict(args))
                else:
                    self._add_operation(name, args)

    def _add_operation(self, name, args):
        operation = Operation(name, shlex.split(str(args)) if args else None)
        self.operations.append(operation)

    def _replace_variables(self, string):
        result = ""
        for token in string.split(" "):
            logger.debug("processing token %s", token)
            if token.startswith("$"):
                var_name = token[1:]
                # set value from environment
                if os.environ.get(var_name):
                    logger.debug("Using host variable %s" % token)
                    token = os.environ[var_name]
                elif self.environment.get(var_name):
                    logger.debug("Using yaml file variable %s" % token)
                    token = self.environment[var_name]
            result += token + " "
        return result

    def _get_artifacts(self, artifacts, destination):
        for artifact in artifacts:
            cct_artifact = CctArtifact(**artifact)
            cct_artifact.fetch(destination)
            self.artifacts[cct_artifact.name] = cct_artifact

    def setup(self):
        pass

    def teardown(self):
        pass

    def _get_resource(self, resource):
        return resource_string(inspect.getmodule(self.__class__).__name__, resource)

    def _get_resource_path(self, resource):
        return resource_filename(inspect.getmodule(self.__class__).__name__, resource)

    def _process_environment(self, operation):
        if '$' in operation.command:
            operation.command = self._replace_variables(operation.command)
        for i in range(len(operation.args)):
            if '$' in operation.args[i]:
                operation.args[i] = self._replace_variables(operation.args[i])

    def run(self, operation):
        try:
            logger.debug("invoking command %s", operation.command)
            method = getattr(self, operation.command)
            method_params = inspect.getargspec(method)
            args = []
            kwargs = {}
            for arg in operation.args:
                if '=' in arg:
                    key, value = arg.split('=', 1)
                    if key in method_params.args:
                        kwargs[key] = value
                    else:
                        args.append(arg.strip())
                else:
                    args.append(arg.strip())
            method(*args, **kwargs)
            logger.debug("operation '%s' Passed" % operation.command)
            operation.state = "Passed"
        except Exception as e:
            logger.error("%s is not supported by module %s", operation.command, e, exc_info=True)
            operation.state = "Error"
            self.state = "Error"
            raise e


class CctArtifact(object):
    """
    Object representing artifact file for changes
    name - name of the file
    md5sum - md5sum
    """
    def __init__(self, name, chksum, url):
        self.name = name
        self.chksum = chksum
        self.url = self.replace_variables(url) if '$' in url else url
        self.filename = name
        self.path = None

    def fetch(self, directory):
        self.path = os.path.join(directory, self.filename)

        if self.check_sum():
            logger.info("Using cached artifact for %s" % self.name)
            return

        logger.info("Fetching %s as an artifact for module %s" % (self.url, self.name))

        try:
            urlrequest.urlretrieve(self.url, self.path)
        except Exception as ex:
            raise CCTError("Cannot download artifact from url %s, error: %s" % (self.url, ex))

        if not self.check_sum():
            raise CCTError("Artifact from %s doesn't match required chksum %s" % (self.url, self.chksum))

    def check_sum(self):
        if not os.path.exists(self.path):
            return False
        hash = getattr(hashlib, self.chksum[:self.chksum.index(':')])()
        with open(self.path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                hash.update(block)
        if self.chksum[self.chksum.index(':') + 1:] == hash.hexdigest():
            return True
        return False

    def replace_variables(self, string):
        var_regex = re.compile('\$\{.*\}')
        variable = var_regex.search(string).group(0)[2:-1]
        if os.environ.get(variable):
            logger.debug("Using host variable %s" % variable)
            string = var_regex.sub(os.environ[variable], string)
        return string


class Operation(object):
    """
    Object representing single operation
    """
    command = None
    args = []
    state = "NotRun"

    def __init__(self, command, args):
        self.command = command
        self.args = []
        if args:
            for arg in args:
                self.args.append(arg.rstrip())


class ShellModule(Module):

    def __init__(self, name, directory, path):
        Module.__init__(self, name, directory)
        self.script = path
        self._discover()

    def __getattr__(self, name):
        def wrapper(*args, **kwargs):
            if name in self.names:
                self._run_function(name, *args)
            else:
                raise Exception("no such method")
        return wrapper

    def _discover(self):
        self.names = {}
        comment = ""
        param = re.compile("\$\d+")
        with open(self.script, "r") as f:
            name = ""
            for line in f:
                if line.startswith('#'):
                    comment += line[1:]
                elif 'function' in line:
                    name = line.split()[1][:-2]
                    function = {"name": name,
                                "comment": comment,
                                "params": {}}
                    self.names[name] = function
                elif param.search(line):
                    param_name = line.split('=')[0].split()[-1]
                    self.logger.debug("function: %s, param_id: %s, param_name: %s"
                                      % (name, param_name, param.search(line).group(0)))
                    self.names[name]["params"][param_name] = param.search(line).group(0)
                else:
                    comment = ""

    def _run_function(self, name, *args):
        cmd = '/bin/bash -c " source %s ; %s %s"' % (self.script, name, " ".join(args))
        try:
            env = dict(os.environ)
            env['CCT_MODULE_PATH'] = os.path.dirname(self.script)
            for name, artifact in self.artifacts.items():
                var_name = 'CCT_ARTIFACT_PATH_' + name.upper()
                logger.info('Created %s environment variable pointing to %s.' % (var_name, artifact.path))
                env['CCT_ARTIFACT_PATH_' + name.upper()] = artifact.path
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, env=env, shell=True)
            self.logger.debug("Step ended with output: %s" % out)
        except subprocess.CalledProcessError as e:
            raise CCTError(e.output)


class Dummy(Module):
    def dump(self, *args):
        """
        Dumps arguments to a logfile.

        Args:
         *args: Will be dumped :).
        """
        logger.info("dummy module performed dump with args %s and environment: %s" % (args, self.environment))
