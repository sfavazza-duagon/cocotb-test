import subprocess
import os
import sys
import tempfile
import re
import cocotb            # type: ignore
import logging
import shutil
from xml.etree import cElementTree as ET
import signal
from typing import List, Union, Optional, Any
from pathlib import Path

from distutils.spawn import find_executable
from distutils.sysconfig import get_config_var

_magic_re = re.compile(r"([\\{}])")
_space_re = re.compile(r"([\s])", re.ASCII)


# ==============================================================================================================
# common functions
def as_tcl_value(value: str):
    r"""Utility to escape all `\` and space characters.

    :param value: string with character to escape.
    """
    # add '\' before special characters and spaces
    value = _magic_re.sub(r"\\\1", value)
    value = value.replace("\n", r"\n")
    value = _space_re.sub(r"\\\1", value)
    if value[0] == '"':
        value = "\\" + value

    return value


def add_args(list_args: List[Union[str, List[str]]]) -> str:
    """Concatenate the given arguments and turn any string list in a space-separated string.

    :param list_args: list of arguments for the output command.
    """

    out_cmd = ''
    for arg in list_args:
        if isinstance(arg, str):
            out_cmd += arg + ' '
        else:
            for inner_arg in arg:
                out_cmd += inner_arg + ' '
    # remove any trailing space character
    return out_cmd.strip()


def get_abs_paths(paths: Optional[List[Union[str, Path]]]) -> List[Union[str, Path]]:
    """Extract the absolute path from the given sources (if any).

    :param paths: list of source paths, if empty this functions does nothing.
    """

    if paths is None:
        return []

    paths_abs = []
    for path in paths:
        paths_abs.append(as_tcl_value(str(Path(path).absolute())))

    return paths_abs


# ==============================================================================================================
# simulators
class Simulator():
    """Simulator abstraction base class.

    :param toplevel:
    :param module:
    :param work_dir:
    :param python_search: list of paths to add to the ``PYTHONPATH`` OS variable.
    :param toplevel_lang:
    :param verilog_sources: list of file-paths to the Verilog/SystemVerilog sources
    :param vhdl_sources: list of file-paths to the VHDL sources.
    :param includes:
    :param defines:
    :param compile_args: additional compilation arguments.
    :param simulation_args: additional simulation arguments right after the default simulation arguments.
    :param extra_args: common additional arguments for compilation and simulation.
    :param plus_args: additional arguments appended at the end of the simulation arguments.
    :param force_compile: force source re-compilation regardless of the file modification timestamp.
    :param testcase:
    :param sim_build:
    :param seed:
    :param extra_env:
    :param compile_only:
    :param gui:

    This class is intended to be extended to define the simulator-specific commands. A child class shall
    implemented the :py:meth:`build_command`, :py:meth:`get_include_commands`, :py:meth:`get_define_commands`
    methods.
    """

    def __init__(self,
                 toplevel: str,
                 module: str,
                 work_dir: Optional[Union[Path, str]] = None,
                 python_search: Optional[List[Union[str, Path]]] = None,
                 toplevel_lang: str = "verilog",
                 verilog_sources: Optional[List[Union[str, Path]]] = None,
                 vhdl_sources: Optional[List[Union[str, Path]]] = None,
                 includes: Optional[List[Union[str, Path]]] = None,
                 defines: Optional[List[str]] = None,
                 compile_args: Optional[List[str]] = None,
                 simulation_args: Optional[List[str]] = None,
                 extra_args: Optional[List[str]] = None,
                 plus_args: Optional[List[str]] = None,
                 force_compile: bool = False,
                 testcase=None,
                 sim_build: str = "sim_build",
                 seed: Optional[Any] = None,
                 extra_env=None,
                 compile_only=False,
                 gui=False,
                 **kwargs):

        # create the simulation folder
        sim_dir = Path.cwd() / sim_build
        sim_dir.mkdir(exist_ok=True)
        self.sim_dir = str(sim_dir)

        self.logger = logging.getLogger("cocotb")
        self.logger.setLevel(logging.INFO)
        logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")

        self.lib_dir = str(Path(cocotb.__file__).parent / "libs")

        self.lib_ext = "dll" if os.name == "nt" else "so"

        self.module = module  # TODO: Auto discovery, try introspect ?

        self.work_dir = self.sim_dir
        if work_dir is not None:
            work_dir = Path(work_dir).absolute()
            if work_dir.is_dir():
                self.work_dir = str(work_dir)

        self.python_search = [] if python_search is None else python_search

        self.toplevel = toplevel
        self.toplevel_lang = toplevel_lang

        self.verilog_sources = [] if verilog_sources is None else get_abs_paths(verilog_sources)
        self.vhdl_sources = [] if vhdl_sources is None else get_abs_paths(vhdl_sources)

        self.includes = [] if includes is None else get_abs_paths(includes)
        self.defines = [] if defines is None else defines

        if extra_args is None:
            extra_args = []

        self.compile_args = [] if compile_args is None else compile_args
        self.compile_args += extra_args
        self.simulation_args = [] if simulation_args is None else simulation_args
        self.simulation_args += extra_args

        self.plus_args = [] if plus_args is None else plus_args
        self.force_compile = force_compile
        self.compile_only = compile_only

        # register the rest of the arguments
        for arg in kwargs:
            setattr(self, arg, kwargs[arg])

        self.env = extra_env if extra_env is not None else {}

        if testcase is not None:
            self.env["TESTCASE"] = testcase

        if seed is not None:
            self.env["RANDOM_SEED"] = str(seed)

        self.gui = gui

        # Catch SIGINT and SIGTERM
        self.old_sigint_h = signal.getsignal(signal.SIGINT)
        self.old_sigterm_h = signal.getsignal(signal.SIGTERM)

        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def set_env(self):

        for e in os.environ:
            self.env[e] = os.environ[e]

        self.env["PATH"] += os.pathsep + self.lib_dir

        self.env["PYTHONPATH"] = os.pathsep.join(sys.path)
        for path in self.python_search:
            self.env["PYTHONPATH"] += os.pathsep + str(path)

        self.env["PYTHONHOME"] = get_config_var("prefix")

        self.env["TOPLEVEL"] = self.toplevel
        self.env["MODULE"] = self.module

    def build_command(self):
        raise NotImplementedError()

    def run(self):
        """Run the simulation and analyze the results.
        """

        sys.tracebacklimit = 0  # remove not needed traceback from assert

        # use temporary results file
        if not os.getenv("COCOTB_RESULTS_FILE"):
            tmp_results_file = tempfile.NamedTemporaryFile(suffix="_results.xml", dir=self.sim_dir)
            results_xml_file = tmp_results_file.name
            tmp_results_file.close()
            self.env["COCOTB_RESULTS_FILE"] = results_xml_file
        else:
            results_xml_file = os.getenv("COCOTB_RESULTS_FILE")

        cmds = self.build_command()
        self.set_env()
        self.execute(cmds)

        if not self.compile_only:
            results_file_exist = os.path.isfile(results_xml_file)
            assert results_file_exist, "Simulation terminated abnormally. Results file not found."

            tree = ET.parse(results_xml_file)
            for ts in tree.iter("testsuite"):
                for tc in ts.iter("testcase"):
                    for failure in tc.iter("failure"):
                        assert False, '{} class="{}" test="{}" error={}'.format(
                            failure.get("message"), tc.get("classname"), tc.get("name"), failure.get("stdout"))

        print("Results file: %s" % results_xml_file)
        return results_xml_file

    def get_include_commands(self, includes):
        raise NotImplementedError()

    def get_define_commands(self, defines):
        raise NotImplementedError()

    def execute(self, cmds: List[List[str]]) -> None:
        """Set up the environment and execute the list of given arguments.

        :param cmds: list of commands to run the simulation
        """

        self.set_env()
        for cmd in cmds:
            self.logger.info("Running command: " + " ".join(cmd))

            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.work_dir, env=self.env)

            while True:
                out = self.process.stdout.readline()

                if not out and self.process.poll() is not None:
                    break

                log_out = out.decode("utf-8").rstrip()
                if log_out != "":
                    self.logger.info(log_out)

            if self.process.returncode:
                self.logger.error("Command terminated with error %d" % self.process.returncode)

    def outdated(self, output, dependencies):

        if not os.path.isfile(output):
            return True

        output_mtime = os.path.getmtime(output)

        dep_mtime = 0
        for dependency in dependencies:
            mtime = os.path.getmtime(dependency)
            if mtime > dep_mtime:
                dep_mtime = mtime

        if dep_mtime > output_mtime:
            return True

        return False

    def exit_gracefully(self, signum, frame):
        pid = None
        if self.process is not None:
            pid = self.process.pid
            self.process.stdout.flush()
            self.process.kill()
            self.process.wait()
        # Restore previous handlers
        signal.signal(signal.SIGINT, self.old_sigint_h)
        signal.signal(signal.SIGTERM, self.old_sigterm_h)
        assert False, f"Exiting pid: {pid} with signum: {signum}"


class Icarus(Simulator):
    def __init__(self, *argv, **kwargs):
        super(Icarus, self).__init__(*argv, **kwargs)

        if self.vhdl_sources:
            raise ValueError("This simulator does not support VHDL")

        self.sim_file = os.path.join(self.sim_dir, self.toplevel + ".vvp")

    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("-I")
            include_cmd.append(dir)

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("-D")
            defines_cmd.append(define)

        return defines_cmd

    def compile_command(self):

        cmd_compile = (
            ["iverilog", "-o", self.sim_file, "-D", "COCOTB_SIM=1", "-s", self.toplevel, "-g2012"]
            + self.get_define_commands(self.defines)
            + self.get_include_commands(self.includes)
            + self.compile_args
            + self.verilog_sources
        )

        return cmd_compile

    def run_command(self):
        return (
            ["vvp", "-M", self.lib_dir, "-m", "libcocotbvpi_icarus"]
            + self.simulation_args
            + [self.sim_file]
            + self.plus_args
        )

    def build_command(self):
        cmd = []
        if self.outdated(self.sim_file, self.verilog_sources) or self.force_compile:
            cmd.append(self.compile_command())
        else:
            self.logger.warning("Skipping compilation:" + self.sim_file)

        # TODO: check dependency?
        if not self.compile_only:
            cmd.append(self.run_command())

        return cmd


class Questa(Simulator):
    def get_include_commands(self, includes):
        include_cmd = []
        for incdir in includes:
            include_cmd.append("+incdir+" + as_tcl_value(incdir))

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("+define+" + as_tcl_value(define))

        return defines_cmd

    def build_command(self):
        """Generate the compilation and simulation Modelsim commands.

        These are exported to scripts to ease the interaction with the simulator while working in GUI mode.
        """

        self.rtl_library = self.toplevel

        cmds = []

        out_file = os.path.join(self.sim_dir, self.toplevel, "_info")

        # ------------------------------------------------------------------------------------------------------
        # compilation phase
        if self.outdated(out_file, self.verilog_sources + self.vhdl_sources) or self.force_compile:

            if (Path(self.sim_dir) / self.rtl_library).is_dir():
                cmds.append([f"vdel -lib {self.rtl_library} -all; quit;"])
            cmds.append([f"vlib {self.rtl_library}; quit;"])

            compile_cmds = []
            if self.vhdl_sources:
                compile_cmds.append(add_args(["vcom -mixedsvvh -work",
                                              self.rtl_library,
                                              "-mfcu",
                                              self.compile_args,
                                              self.vhdl_sources]))

            if self.verilog_sources:
                compile_cmds.append(add_args(["vlog -mixedsvvh -work",
                                              self.rtl_library,
                                              "+define+COCOTB_SIM -sv -mfcu",
                                              self.get_define_commands(self.defines),
                                              self.get_include_commands(self.includes),
                                              self.compile_args,
                                              self.verilog_sources]))

            # export the commands to compile the sources to ease user/simulator interaction
            compile_filename = Path(self.sim_dir) / 'compile.do'
            with open(compile_filename, 'w') as fscript:
                for compile_cmd in compile_cmds:
                    fscript.write(compile_cmd + '\n')
                cmds.append([f"source {compile_filename.as_posix()}; quit;"])

            # add the command prefix to all commands
            for idx, cmd in enumerate(cmds):
                cmds[idx] = ["vsim", "-c", "-do"] + cmd
        else:
            self.logger.warning("Skipping compilation:" + out_file)

        # ------------------------------------------------------------------------------------------------------
        # simulation phase
        if not self.compile_only:

            # select python-to-hdl interface according to the top-level language
            if self.toplevel_lang == "vhdl":
                ext_name = "-foreign cocotb_init " \
                    + as_tcl_value(str(Path(self.lib_dir) / f"libcocotbfli_modelsim.{self.lib_ext}"))
                if self.verilog_sources:
                    self.env["GPI_EXTRA"] = "cocotbvpi_modelsim:cocotbvpi_entry_point"
            else:
                ext_name = "-pli " \
                    + as_tcl_value(str(Path(self.lib_dir) / f"libcocotbvpi_modelsim.{self.lib_ext}"))
                if self.vhdl_sources:
                    self.env["GPI_EXTRA"] = "cocotbfli_modelsim:cocotbfli_entry_point"

            # compose the script
            do_script = add_args([f"vsim -onfinish {'stop' if self.gui else 'exit'}",
                                  ext_name,
                                  self.simulation_args,
                                  self.rtl_library + '.' + self.toplevel,
                                  self.plus_args])

            if not self.gui:
                do_script += "run -all; quit"

            # export the commands to run the simulation to ease user/simulator interaction
            run_filename = Path(self.sim_dir) / 'runsim.do'
            with open(run_filename, 'w') as fscript:
                fscript.write(do_script)

            cmds.append(["vsim"] + (["-gui"] if self.gui else ["-c"]) + ["-do"] \
                        + [f"source {run_filename.as_posix()}"])

        return cmds


class Ius(Simulator):
    def __init__(self, *argv, **kwargs):
        super(Ius, self).__init__(*argv, **kwargs)

        self.env["GPI_EXTRA"] = "cocotbvhpi_ius:cocotbvhpi_entry_point"

    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("-incdir")
            include_cmd.append(dir)

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("-define")
            defines_cmd.append(define)

        return defines_cmd

    def build_command(self):

        out_file = os.path.join(self.sim_dir, "INCA_libs", "history")

        cmd = []

        if self.outdated(out_file, self.verilog_sources + self.vhdl_sources) or self.force_compile:
            cmd_elab = (
                [
                    "irun",
                    "-64",
                    "-elaborate",
                    "-v93",
                    "-define",
                    "COCOTB_SIM=1",
                    "-loadvpi",
                    os.path.join(self.lib_dir, "libcocotbvpi_ius." + self.lib_ext) + ":vlog_startup_routines_bootstrap",
                    "-plinowarn",
                    "-access",
                    "+rwc",
                    "-top",
                    self.toplevel,
                ]
                + self.get_define_commands(self.defines)
                + self.get_include_commands(self.includes)
                + self.compile_args
                + self.verilog_sources
                + self.vhdl_sources
            )
            cmd.append(cmd_elab)

        else:
            self.logger.warning("Skipping compilation:" + out_file)

        if not self.compile_only:
            cmd_run = ["irun", "-64", "-R", ("-gui" if self.gui else "")] + self.simulation_args + self.plus_args
            cmd.append(cmd_run)

        return cmd


class Xcelium(Simulator):
    def __init__(self, *argv, **kwargs):
        super(Xcelium, self).__init__(*argv, **kwargs)

        self.env["GPI_EXTRA"] = "cocotbvhpi_ius:cocotbvhpi_entry_point"

    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("-incdir")
            include_cmd.append(dir)

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("-define")
            defines_cmd.append(define)

        return defines_cmd

    def build_command(self):

        out_file = os.path.join(self.sim_dir, "INCA_libs", "history")

        cmd = []

        if self.outdated(out_file, self.verilog_sources + self.vhdl_sources) or self.force_compile:
            cmd_elab = (
                [
                    "xrun",
                    "-64",
                    "-elaborate",
                    "-v93",
                    "-define",
                    "COCOTB_SIM=1",
                    "-loadvpi",
                    os.path.join(self.lib_dir, "libcocotbvpi_ius." + self.lib_ext) + ":vlog_startup_routines_bootstrap",
                    "-plinowarn",
                    "-access",
                    "+rwc",
                    "-top",
                    self.toplevel,
                ]
                + self.get_define_commands(self.defines)
                + self.get_include_commands(self.includes)
                + self.compile_args
                + self.verilog_sources
                + self.vhdl_sources
            )
            cmd.append(cmd_elab)

        else:
            self.logger.warning("Skipping compilation:" + out_file)

        if not self.compile_only:
            cmd_run = ["xrun", "-64", "-R", ("-gui" if self.gui else "")] + self.simulation_args + self.plus_args
            cmd.append(cmd_run)

        return cmd


class Vcs(Simulator):
    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("+incdir+" + dir)

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("+define+" + define)

        return defines_cmd

    def build_command(self):

        pli_cmd = "acc+=rw,wn:*"

        cmd = []

        do_file_path = os.path.join(self.sim_dir, "pli.tab")
        with open(do_file_path, "w") as pli_file:
            pli_file.write(pli_cmd)

        cmd_build = (
            [
                "vcs",
                "-full64",
                "-debug",
                "+vpi",
                "-P",
                "pli.tab",
                "-sverilog",
                "+define+COCOTB_SIM=1",
                "-load",
                os.path.join(self.lib_dir, "libcocotbvpi_vcs." + self.lib_ext),
            ]
            + self.get_define_commands(self.defines)
            + self.get_include_commands(self.includes)
            + self.compile_args
            + self.verilog_sources
        )
        cmd.append(cmd_build)

        if not self.compile_only:
            cmd_run = [os.path.join(self.sim_dir, "simv"), "+define+COCOTB_SIM=1"] + self.simulation_args
            cmd.append(cmd_run)

        if self.gui:
            cmd.append("-gui")  # not tested!

        return cmd


class Ghdl(Simulator):
    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("-I")
            include_cmd.append(dir)

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("-D")
            defines_cmd.append(define)

    def build_command(self):

        cmd = []

        for source_file in self.vhdl_sources:
            cmd.append(["ghdl"] + self.compile_args + ["-i", source_file])

        cmd_elaborate = ["ghdl"] + self.compile_args + ["-m", self.toplevel]
        cmd.append(cmd_elaborate)

        cmd_run = [
            "ghdl",
            "-r",
            self.toplevel,
            "--vpi=" + os.path.join(self.lib_dir, "libcocotbvpi_ghdl." + self.lib_ext),
        ] + self.simulation_args

        if not self.compile_only:
            cmd.append(cmd_run)

        return cmd


class Riviera(Simulator):
    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("+incdir+" + as_tcl_value(dir))

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("+define+" + as_tcl_value(define))

        return defines_cmd

    def build_command(self):

        self.rtl_library = self.toplevel

        do_script = "\nonerror {\n quit -code 1 \n} \n"

        out_file = os.path.join(self.sim_dir, self.rtl_library, self.rtl_library + ".lib")

        if self.outdated(out_file, self.verilog_sources + self.vhdl_sources) or self.force_compile:

            do_script += "alib {RTL_LIBRARY} \n".format(RTL_LIBRARY=as_tcl_value(self.rtl_library))

            if self.vhdl_sources:
                do_script += "acom -work {RTL_LIBRARY} {EXTRA_ARGS} {VHDL_SOURCES}\n".format(
                    RTL_LIBRARY=as_tcl_value(self.rtl_library),
                    VHDL_SOURCES=" ".join(as_tcl_value(v) for v in self.vhdl_sources),
                    EXTRA_ARGS=" ".join(as_tcl_value(v) for v in self.compile_args),
                )

            if self.verilog_sources:
                do_script += "alog -work {RTL_LIBRARY} +define+COCOTB_SIM -sv {DEFINES} {INCDIR} {EXTRA_ARGS} {VERILOG_SOURCES} \n".format(
                    RTL_LIBRARY=as_tcl_value(self.rtl_library),
                    VERILOG_SOURCES=" ".join(as_tcl_value(v) for v in self.verilog_sources),
                    DEFINES=" ".join(self.get_define_commands(self.defines)),
                    INCDIR=" ".join(self.get_include_commands(self.includes)),
                    EXTRA_ARGS=" ".join(as_tcl_value(v) for v in self.compile_args),
                )
        else:
            self.logger.warning("Skipping compilation:" + out_file)

        if not self.compile_only:
            if self.toplevel_lang == "vhdl":
                do_script += "asim +access +w -interceptcoutput -O2 -loadvhpi {EXT_NAME} {EXTRA_ARGS} {RTL_LIBRARY}.{TOPLEVEL} \n".format(
                    RTL_LIBRARY=as_tcl_value(self.rtl_library),
                    TOPLEVEL=as_tcl_value(self.toplevel),
                    EXT_NAME=as_tcl_value(os.path.join(self.lib_dir, "libcocotbvhpi_aldec")),
                    EXTRA_ARGS=" ".join(as_tcl_value(v) for v in self.simulation_args),
                )
                if self.verilog_sources:
                    self.env["GPI_EXTRA"] = "cocotbvpi_aldec:cocotbvpi_entry_point"
            else:
                do_script += "asim +access +w -interceptcoutput -O2 -pli {EXT_NAME} {EXTRA_ARGS} {RTL_LIBRARY}.{TOPLEVEL} {PLUS_ARGS} \n".format(
                    RTL_LIBRARY=as_tcl_value(self.rtl_library),
                    TOPLEVEL=as_tcl_value(self.toplevel),
                    EXT_NAME=as_tcl_value(os.path.join(self.lib_dir, "libcocotbvpi_aldec")),
                    EXTRA_ARGS=" ".join(as_tcl_value(v) for v in self.simulation_args),
                    PLUS_ARGS=" ".join(as_tcl_value(v) for v in self.plus_args),
                )
                if self.vhdl_sources:
                    self.env["GPI_EXTRA"] = "cocotbvhpi_aldec:cocotbvhpi_entry_point"

            do_script += "run -all \nexit"

        do_file = tempfile.NamedTemporaryFile(delete=False)
        do_file.write(do_script.encode())
        do_file.close()
        # print(do_script)

        return [["vsimsa"] + ["-do"] + ["do"] + [do_file.name]]


class Verilator(Simulator):
    def __init__(self, *argv, **kwargs):
        super(Verilator, self).__init__(*argv, **kwargs)

        if self.vhdl_sources:
            raise ValueError("This simulator does not support VHDL")

    def get_include_commands(self, includes):
        include_cmd = []
        for dir in includes:
            include_cmd.append("-I" + dir)

        return include_cmd

    def get_define_commands(self, defines):
        defines_cmd = []
        for define in defines:
            defines_cmd.append("-D" + define)

        return defines_cmd

    def build_command(self):

        cmd = []

        out_file = os.path.join(self.sim_dir, self.toplevel)
        verilator_cpp = os.path.join(os.path.dirname(os.path.dirname(self.lib_dir)), "share", "verilator.cpp")
        verilator_cpp = os.path.join(os.path.dirname(cocotb.__file__), "share", "lib", "verilator", "verilator.cpp")

        verilator_exec = find_executable("verilator")
        if verilator_exec is None:
            raise ValueError("Verilator executable not found.")

        cmd.append(
            [
                "perl",
                verilator_exec,
                "-cc",
                "--exe",
                "-Mdir",
                self.sim_dir,
                "-DCOCOTB_SIM=1",
                "--top-module",
                self.toplevel,
                "--vpi",
                "--public-flat-rw",
                "--prefix",
                "Vtop",
                "-o",
                self.toplevel,
                "-LDFLAGS",
                "-Wl,-rpath,{LIB_DIR} -L{LIB_DIR} -lcocotbvpi_verilator".format(LIB_DIR=self.lib_dir),
            ]
            + self.compile_args
            + self.get_define_commands(self.defines)
            + self.get_include_commands(self.includes)
            + [verilator_cpp]
            + self.verilog_sources
        )

        self.env["CPPFLAGS"] = "-std=c++11"
        cmd.append(["make", "-C", self.sim_dir, "-f", "Vtop.mk"])

        if not self.compile_only:
            cmd.append([out_file])

        return cmd


def run(**kwargs):

    sim_env = os.getenv("SIM", "icarus")

    supported_sim = ["icarus", "questa", "ius", "xcelium", "vcs", "ghdl", "riviera", "verilator"]
    if sim_env not in supported_sim:
        raise NotImplementedError("Set SIM variable. Supported: " + ", ".join(supported_sim))

    if sim_env == "icarus":
        sim = Icarus(**kwargs)
    elif sim_env == "questa":
        sim = Questa(**kwargs)
    elif sim_env == "ius":
        sim = Ius(**kwargs)
    elif sim_env == "xcelium":
        sim = Xcelium(**kwargs)
    elif sim_env == "vcs":
        sim = Vcs(**kwargs)
    elif sim_env == "ghdl":
        sim = Ghdl(**kwargs)
    elif sim_env == "riviera":
        sim = Riviera(**kwargs)
    elif sim_env == "verilator":
        sim = Verilator(**kwargs)

    return sim.run()


def clean(recursive=False):
    dir = os.getcwd()

    def rm_clean():
        sim_build_dir = os.path.join(dir, "sim_build")
        if os.path.isdir(sim_build_dir):
            print("Removing:", sim_build_dir)
            shutil.rmtree(sim_build_dir, ignore_errors=True)

    rm_clean()

    if recursive:
        for dir, _, _ in os.walk(dir):
            rm_clean()
