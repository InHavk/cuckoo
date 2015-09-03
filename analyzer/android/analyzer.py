# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.
# Originally contributed by Check Point Software Technologies, Ltd.

import os
import logging
import pkgutil
import shutil
import sys
import xmlrpclib
import time

from lib.core.packages import choose_package
from lib.common.exceptions import CuckooError, CuckooPackageError
from lib.common.abstracts import Package, Auxiliary
from lib.common.constants import PATHS
from lib.core.config import Config
from lib.core.startup import init_logging
from modules import auxiliary

logging.disable(level=logging.DEBUG)
log = logging.getLogger()

class Analyzer(object):
    def __init__(self):
        self.config = None
        self.target = None

    def complete(self):
        """End analysis."""
        log.info("Analysis completed")

    def get_options(self):
        """Get analysis options.
        @return: options dict.
        """
        # The analysis package can be provided with some options in the
        # following format:
        #   option1=value1,option2=value2,option3=value3
        #
        # Here we parse such options and provide a dictionary that will be made
        # accessible to the analysis package.
        options = {}
        if self.config.options:
            try:
                # Split the options by comma.
                fields = self.config.options.strip().split(",")
            except ValueError as e:
                log.warning("Failed parsing the options: %s", e)
            else:
                for field in fields:
                    # Split the name and the value of the option.
                    try:
                        key, value = field.strip().split("=")
                    except ValueError as e:
                        log.warning("Failed parsing option (%s): %s", field, e)
                    else:
                        # If the parsing went good, we add the option to the
                        # dictionary.
                        options[key.strip()] = value.strip()

        return options

    def prepare(self):
        # Initialize logging.
        init_logging()

        # Parse the analysis configuration file generated by the agent.
        self.config = Config(cfg="analysis.conf")

        # We update the target according to its category. If it's a file, then
        # we store the path.
        if self.config.category == "file":
            self.target = os.path.join("/data/local/tmp", str(self.config.file_name))
            shutil.copyfile("config/hooks.json", "/data/local/tmp/hooks.json")
        # If it's a URL, well.. we store the URL.
        else:
            self.target = self.config.target

    def run(self):
        self.prepare()

        log.info("Starting analyzer from: {0}".format(os.getcwd()))
        log.info("Storing results at: {0}".format(PATHS["root"]))
        log.info("Target is: {0}".format(self.target))

        # If no analysis package was specified at submission, we try to select
        # one automatically.
        if not self.config.package:
            log.info("No analysis package specified, trying to detect it automagically")
            # If the analysis target is a file, we choose the package according
            # to the file format.
            if self.config.category == "file":
                package = choose_package(self.config.file_type, self.config.file_name)
            # If it's an URL, we'll just use the default Internet Explorer
            # package.
            else:
                package = "default_browser"

            # If we weren't able to automatically determine the proper package,
            # we need to abort the analysis.
            if not package:
                raise CuckooError("No valid package available for file type: {0}".format(self.config.file_type))

            log.info("Automatically selected analysis package \"%s\"", package)
        # Otherwise just select the specified package.
        else:
            package = self.config.package

        # Generate the package path.
        package_name = "modules.packages.%s" % package

        # Try to import the analysis package.
        try:
            __import__(package_name, globals(), locals(), ["dummy"], -1)
        # If it fails, we need to abort the analysis.
        except ImportError:
            raise CuckooError("Unable to import package \"{0}\", does not exist.".format(package_name))

        # Initialize the package parent abstract.
        Package()

        # Enumerate the abstract's subclasses.
        try:
            package_class = Package.__subclasses__()[0]
        except IndexError as e:
            raise CuckooError("Unable to select package class (package={0}): {1}".format(package_name, e))

        # Initialize the analysis package.
        pack = package_class(self.get_options())

        # Initialize Auxiliary modules
        Auxiliary()
        prefix = auxiliary.__name__ + "."
        for loader, name, ispkg in pkgutil.iter_modules(auxiliary.__path__, prefix):
            if ispkg:
                continue

            # Import the auxiliary module.
            try:
                __import__(name, globals(), locals(), ["dummy"], -1)
            except ImportError as e:
                log.warning("Unable to import the auxiliary module "
                            "\"%s\": %s", name, e)

        # Walk through the available auxiliary modules.
        aux_enabled = []
        for module in Auxiliary.__subclasses__():
            # Try to start the auxiliary module.
            try:
                aux = module()
                aux.start()
            except (NotImplementedError, AttributeError):
                log.warning("Auxiliary module %s was not implemented",
                            aux.__class__.__name__)
                continue
            except Exception as e:
                log.warning("Cannot execute auxiliary module %s: %s",
                            aux.__class__.__name__, e)
                continue
            finally:
                log.info("Started auxiliary module %s",
                         aux.__class__.__name__)
                aux_enabled.append(aux)

        # Start analysis package. If for any reason, the execution of the
        # analysis package fails, we have to abort the analysis.
        try:
            pack.start(self.target)
        except NotImplementedError:
            raise CuckooError("The package \"{0}\" doesn't contain a run "
                              "function.".format(package_name))
        except CuckooPackageError as e:
            raise CuckooError("The package \"{0}\" start function raised an "
                              "error: {1}".format(package_name, e))
        except Exception as e:
            raise CuckooError("The package \"{0}\" start function encountered "
                              "an unhandled exception: "
                              "{1}".format(package_name, e))

        time_counter = 0
        while True:
            time_counter += 1
            if time_counter == int(self.config.timeout):
                log.info("Analysis timeout hit, terminating analysis")
                break

            try:
                # The analysis packages are provided with a function that
                # is executed at every loop's iteration. If such function
                # returns False, it means that it requested the analysis
                # to be terminate.
                if not pack.check():
                    log.info("The analysis package requested the "
                             "termination of the analysis...")
                    break

                # If the check() function of the package raised some exception
                # we don't care, we can still proceed with the analysis but we
                # throw a warning.
            except Exception as e:
                log.warning("The package \"%s\" check function raised "
                            "an exception: %s", package_name, e)
            finally:
                # Zzz.
                time.sleep(1)

        try:
            # Before shutting down the analysis, the package can perform some
            # final operations through the finish() function.
            pack.finish()
        except Exception as e:
            log.warning("The package \"%s\" finish function raised an "
                        "exception: %s", package_name, e)

        # Terminate the Auxiliary modules.
        for aux in aux_enabled:
            try:
                aux.stop()
            except (NotImplementedError, AttributeError):
                continue
            except Exception as e:
                log.warning("Cannot terminate auxiliary module %s: %s",
                            aux.__class__.__name__, e)

        # Let's invoke the completion procedure.
        self.complete()
        return True

if __name__ == "__main__":
    success = False
    error = ""

    try:
        # Initialize the main analyzer class.
        analyzer = Analyzer()
        # Run it and wait for the response.
        success = analyzer.run()
    # This is not likely to happen.
    except KeyboardInterrupt:
        error = "Keyboard Interrupt"
    # If the analysis process encountered a critical error, it will raise a
    # CuckooError exception, which will force the termination of the analysis
    # weill notify the agent of the failure. Also catched unexpected
    # exceptions.
    except Exception as e:
        # Store the error.
        error = str(e)

        # Just to be paranoid.
        if len(log.handlers) > 0:
            log.critical(error)
        else:
            sys.stderr.write("{0}\n".format(e))
    # Once the analysis is completed or terminated for any reason, we report
    # back to the agent, notifying that it can report back to the host.
    finally:
        # Establish connection with the agent XMLRPC server.
        server = xmlrpclib.Server("http://127.0.0.1:8000")
        server.complete(success, error, PATHS["root"])
