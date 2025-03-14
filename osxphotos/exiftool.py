""" Yet another simple exiftool wrapper 
    I rolled my own for following reasons: 
    1. I wanted something under MIT license (best alternative was licensed under GPL/BSD)
    2. I wanted singleton behavior so only a single exiftool process was ever running
    3. When used as a context manager, I wanted the operations to batch until exiting the context (improved performance)
    If these aren't important to you, I highly recommend you use Sven Marnach's excellent 
    pyexiftool: https://github.com/smarnach/pyexiftool which provides more functionality """

import json
import logging
import os
import re
import shutil
import subprocess
from functools import lru_cache  # pylint: disable=syntax-error

# exiftool -stay_open commands outputs this EOF marker after command is run
EXIFTOOL_STAYOPEN_EOF = "{ready}"
EXIFTOOL_STAYOPEN_EOF_LEN = len(EXIFTOOL_STAYOPEN_EOF)


@lru_cache(maxsize=1)
def get_exiftool_path():
    """ return path of exiftool, cache result """
    exiftool_path = shutil.which("exiftool")
    if exiftool_path:
        return exiftool_path.rstrip()
    else:
        raise FileNotFoundError(
            "Could not find exiftool. Please download and install from "
            "https://exiftool.org/"
        )


class _ExifToolProc:
    """Runs exiftool in a subprocess via Popen
    Creates a singleton object"""

    def __new__(cls, *args, **kwargs):
        """ create new object or return instance of already created singleton """
        if not hasattr(cls, "instance") or not cls.instance:
            cls.instance = super().__new__(cls)

        return cls.instance

    def __init__(self, exiftool=None):
        """construct _ExifToolProc singleton object or return instance of already created object
        exiftool: optional path to exiftool binary (if not provided, will search path to find it)"""

        if hasattr(self, "_process_running") and self._process_running:
            # already running
            if exiftool is not None and exiftool != self._exiftool:
                logging.warning(
                    f"exiftool subprocess already running, "
                    f"ignoring exiftool={exiftool}"
                )
            return

        self._process_running = False
        self._exiftool = exiftool or get_exiftool_path()
        self._start_proc()

    @property
    def process(self):
        """ return the exiftool subprocess """
        if self._process_running:
            return self._process
        else:
            raise ValueError("exiftool process is not running")

    @property
    def pid(self):
        """ return process id (PID) of the exiftool process """
        return self._process.pid

    @property
    def exiftool(self):
        """ return path to exiftool process """
        return self._exiftool

    def _start_proc(self):
        """ start exiftool in batch mode """

        if self._process_running:
            logging.warning("exiftool already running: {self._process}")
            return

        # open exiftool process
        self._process = subprocess.Popen(
            [
                self._exiftool,
                "-stay_open",  # keep process open in batch mode
                "True",  # -stay_open=True, keep process open in batch mode
                "-@",  # read command-line arguments from file
                "-",  # read from stdin
                "-common_args",  # specifies args common to all commands subsequently run
                "-n",  # no print conversion (e.g. print tag values in machine readable format)
                "-P",  # Preserve file modification date/time
                "-G",  # print group name for each tag
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._process_running = True

    def _stop_proc(self):
        """ stop the exiftool process if it's running, otherwise, do nothing """

        if not self._process_running:
            return

        self._process.stdin.write(b"-stay_open\n")
        self._process.stdin.write(b"False\n")
        self._process.stdin.flush()
        try:
            self._process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            logging.warning(
                f"exiftool pid {self._process.pid} did not exit, killing it"
            )
            self._process.kill()
            self._process.communicate()

        del self._process
        self._process_running = False

    def __del__(self):
        self._stop_proc()


class ExifTool:
    """ Basic exiftool interface for reading and writing EXIF tags """

    def __init__(self, filepath, exiftool=None, overwrite=True, flags=None):
        """Create ExifTool object

        Args:
            file: path to image file
            exiftool: path to exiftool, if not specified will look in path
            overwrite: if True, will overwrite image file without creating backup, default=False
            flags: optional list of exiftool flags to prepend to exiftool command when writing metadata (e.g. -m or -F)

        Returns:
            ExifTool instance
        """
        self.file = filepath
        self.overwrite = overwrite
        self.flags = flags or []
        self.data = {}
        self.warning = None
        self.error = None
        # if running as a context manager, self._context_mgr will be True
        self._context_mgr = False
        self._exiftoolproc = _ExifToolProc(exiftool=exiftool)
        self._process = self._exiftoolproc.process
        self._read_exif()

    def setvalue(self, tag, value):
        """Set tag to value(s); if value is None, will delete tag

        Args:
            tag: str; name of tag to set
            value: str; value to set tag to

        Returns:
            True if success otherwise False

            If error generated by exiftool, returns False and sets self.error to error string
            If warning generated by exiftool, returns True (unless there was also an error) and sets self.warning to warning string
            If called in context manager, returns True (execution is delayed until exiting context manager)
        """

        if value is None:
            value = ""
        command = [f"-{tag}={value}"]
        if self.overwrite and not self._context_mgr:
            command.append("-overwrite_original")

        # avoid "Warning: Some character(s) could not be encoded in Latin" warning
        command.append("-iptc:codedcharacterset=utf8")

        if self._context_mgr:
            self._commands.extend(command)
            return True
        else:
            _, _, error = self.run_commands(*command)
            return error is None

    def addvalues(self, tag, *values):
        """Add one or more value(s) to tag
            If more than one value is passed, each value will be added to the tag

        Args:
            tag: str; tag to set
            *values: str; one or more values to set

        Returns:
            True if success otherwise False

            If error generated by exiftool, returns False and sets self.error to error string
            If warning generated by exiftool, returns True (unless there was also an error) and sets self.warning to warning string
            If called in context manager, returns True (execution is delayed until exiting context manager)

        Notes: exiftool may add duplicate values for some tags so the caller must ensure
               the values being added are not already in the EXIF data
               For some tags, such as IPTC:Keywords, this will add a new value to the list of keywords,
               but for others, such as EXIF:ISO, this will literally add a value to the existing value.
               It's up to the caller to know what exiftool will do for each tag
               If setvalue called before addvalues, exiftool does not appear to add duplicates,
               but if addvalues called without first calling setvalue, exiftool will add duplicate values
        """
        if not values:
            raise ValueError("Must pass at least one value")

        command = []
        for value in values:
            if value is None:
                raise ValueError("Can't add None value to tag")
            command.append(f"-{tag}+={value}")

        if self.overwrite and not self._context_mgr:
            command.append("-overwrite_original")

        if self._context_mgr:
            self._commands.extend(command)
            return True
        else:
            _, _, error = self.run_commands(*command)
            return error is None

    def run_commands(self, *commands, no_file=False):
        """Run commands in the exiftool process and return result.

        Args:
                *commands: exiftool commands to run
                no_file: (bool) do not pass the filename to exiftool (default=False)
                        by default, all commands will be run against self.file
                        use no_file=True to run a command without passing the filename
        Returns:
            (output, warning, errror)
            output: bytes is containing output of exiftool commands
            warning: if exiftool generated warnings, string containing warning otherwise empty string
            error: if exiftool generated errors, string containing otherwise empty string

        Note: Also sets self.warning and self.error if warning or error generated.
        """
        if not (hasattr(self, "_process") and self._process):
            raise ValueError("exiftool process is not running")

        if not commands:
            raise TypeError("must provide one or more command to run")

        if self._context_mgr and self.overwrite:
            commands = list(commands)
            commands.append("-overwrite_original")

        filename = os.fsencode(self.file) if not no_file else b""

        if self.flags:
            # need to split flags, e.g. so "--ext AVI" becomes ["--ext", "AVI"]
            flags = []
            for f in self.flags:
                flags.extend(f.split())
            command_str = b"\n".join([f.encode("utf-8") for f in flags])
            command_str += b"\n"
        else:
            command_str = b""

        command_str += (
            b"\n".join([c.encode("utf-8") for c in commands])
            + b"\n"
            + filename
            + b"\n"
            + b"-execute\n"
        )

        # send the command
        self._process.stdin.write(command_str)
        self._process.stdin.flush()

        # read the output
        output = b""
        warning = b""
        error = b""
        while EXIFTOOL_STAYOPEN_EOF not in str(output):
            line = self._process.stdout.readline()
            if line.startswith(b"Warning"):
                warning += line.strip()
            elif line.startswith(b"Error"):
                error += line.strip()
            else:
                output += line.strip()
        warning = "" if warning == b"" else warning.decode("utf-8")
        error = "" if error == b"" else error.decode("utf-8")
        self.warning = warning
        self.error = error
        return output[:-EXIFTOOL_STAYOPEN_EOF_LEN], warning, error

    @property
    def pid(self):
        """ return process id (PID) of the exiftool process """
        return self._process.pid

    @property
    def version(self):
        """ returns exiftool version """
        ver, _, _ = self.run_commands("-ver", no_file=True)
        return ver.decode("utf-8")

    def asdict(self, tag_groups=True):
        """return dictionary of all EXIF tags and values from exiftool
        returns empty dict if no tags

        Args:
            tag_groups: if True (default), dict keys have tag groups, e.g. "IPTC:Keywords"; if False, drops groups from keys, e.g. "Keywords"
        """
        json_str, _, _ = self.run_commands("-json")
        if not json_str:
            return dict()

        try:
            exifdict = json.loads(json_str)
        except Exception as e:
            # will fail with some commands, e.g --ext AVI which produces
            # 'No file with specified extension' instead of json
            return dict()

        exifdict = exifdict[0]
        if not tag_groups:
            # strip tag groups
            exif_new = {}
            for k, v in exifdict.items():
                k = re.sub(r".*:", "", k)
                exif_new[k] = v
            exifdict = exif_new
        return exifdict

    def json(self):
        """ returns JSON string containing all EXIF tags and values from exiftool """
        json, _, _ = self.run_commands("-json")
        return json

    def _read_exif(self):
        """ read exif data from file """
        data = self.asdict()
        self.data = {k: v for k, v in data.items()}

    def __str__(self):
        return f"file: {self.file}\nexiftool: {self._exiftoolproc._exiftool}"

    def __enter__(self):
        self._context_mgr = True
        self._commands = []
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            return False
        elif self._commands:
            # run_commands sets self.warning and self.error as needed
            self.run_commands(*self._commands)
