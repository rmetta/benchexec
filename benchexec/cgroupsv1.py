# This file is part of BenchExec, a framework for reliable benchmarking:
# https://github.com/sosy-lab/benchexec
#
# SPDX-FileCopyrightText: 2007-2020 Dirk Beyer <https://www.sosy-lab.org>
#
# SPDX-License-Identifier: Apache-2.0

import errno
import logging
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import time

from benchexec import util
from benchexec.cgroups import Cgroups
from benchexec import systeminfo

from ctypes import cdll

_libc = cdll.LoadLibrary("libc.so.6")
_EFD_CLOEXEC = 0x80000  # from <sys/eventfd.h>: mark eventfd as close-on-exec

_BYTE_FACTOR = 1000  # byte in kilobyte

_CGROUP_FALLBACK_PATH = "system.slice/benchexec-cgroup.service"
"""If we do not have write access to the current cgroup,
attempt to use this cgroup as fallback."""

_CGROUP_NAME_PREFIX = "benchmark_"


def _find_own_cgroups():
    """
    For all subsystems, return the information in which (sub-)cgroup this process is in.
    (Each process is in exactly cgroup in each hierarchy.)
    @return a generator of tuples (subsystem, cgroup)
    """
    try:
        with open("/proc/self/cgroup", "rt") as ownCgroupsFile:
            for cgroup in _parse_proc_pid_cgroup(ownCgroupsFile):
                yield cgroup
    except OSError:
        logging.exception("Cannot read /proc/self/cgroup")


def _parse_proc_pid_cgroup(content):
    """
    Parse a /proc/*/cgroup file into tuples of (subsystem,cgroup).
    @param content: An iterable over the lines of the file.
    @return: a generator of tuples
    """
    for ownCgroup in content:
        # each line is "id:subsystem,subsystem:path"
        ownCgroup = ownCgroup.strip().split(":")
        try:
            path = ownCgroup[2][1:]  # remove leading /
        except IndexError:
            raise IndexError(f"index out of range for {ownCgroup}")
        for subsystem in ownCgroup[1].split(","):
            yield (subsystem, path)


def _kill_all_tasks_in_cgroup(cgroup, ensure_empty=True):
    tasksFile = cgroup / "tasks"

    i = 0
    while True:
        i += 1
        # TODO We can probably remove this loop over signals and just send
        # SIGKILL. We added this loop when killing sub-processes was not reliable
        # and we did not know why, but now it is reliable.
        for sig in [signal.SIGKILL, signal.SIGINT, signal.SIGTERM]:
            task = None
            try:
                with open(tasksFile, "rt") as tasks:
                    for task in tasks:
                        task = task.strip()
                        if i > 1:
                            logging.warning(
                                "Run has left-over process with pid %s "
                                "in cgroup %s, sending signal %s (try %s).",
                                task,
                                cgroup,
                                sig,
                                i,
                            )
                        util.kill_process(int(task), sig)
            except FileNotFoundError:
                logging.warning(
                    "cgroup tasks file %s " "could no longer be found while killing",
                    tasksFile,
                )

            if task is None or not ensure_empty:
                return  # No process was hanging, exit
            # wait for the process to exit, this might take some time
            time.sleep(i * 0.5)


def _register_process_with_cgrulesengd(pid):
    """Tell cgrulesengd daemon to not move the given process into other cgroups,
    if libcgroup is available.
    """
    # Logging/printing from inside preexec_fn would end up in the output file,
    # not in the correct logger, thus it is disabled here.
    from ctypes import cdll

    try:
        libcgroup = cdll.LoadLibrary("libcgroup.so.1")
        failure = libcgroup.cgroup_init()
        if failure:
            pass
        else:
            CGROUP_DAEMON_UNCHANGE_CHILDREN = 0x1
            failure = libcgroup.cgroup_register_unchanged_process(
                pid, CGROUP_DAEMON_UNCHANGE_CHILDREN
            )
            if failure:
                pass
                # print(f'Could not register process to cgrulesndg, error {success}. '
                #      'Probably the daemon will mess up our cgroups.')
    except OSError:
        pass


class CgroupsV1(Cgroups):
    version = 1

    IO = "blkio"
    CPU = "cpuacct"
    CPUSET = "cpuset"
    FREEZE = "freezer"
    MEMORY = "memory"

    known_subsystems = {
        # cgroups for BenchExec
        IO,
        CPU,
        CPUSET,
        FREEZE,
        MEMORY,
        # other cgroups users might want
        "cpu",
        "devices",
        "net_cls",
        "net_prio",
        "hugetlb",
        "perf_event",
        "pids",
    }

    def __init__(self, subsystems):
        assert set(subsystems.keys()) <= self.known_subsystems
        super(CgroupsV1, self).__init__(subsystems)

    @classmethod
    def from_system(cls, cgroup_procinfo=None, fallback=True):
        """
        Return a Cgroup object with the cgroups of the current process.
        Note that it is not guaranteed that all subsystems are available
        in the returned object, as a subsystem may not be mounted.
        Check with "subsystem in <instance>" before using.
        A subsystem may also be present but we do not have the rights to create
        child cgroups, this can be checked with require_subsystem().
        @param cgroup_procinfo: If given, use this instead of reading /proc/self/cgroup.
        @param fallback: Whether to look for a default cgroup as fallback if our cgroup
            is not accessible.
        """
        logging.debug(
            "Analyzing /proc/mounts and /proc/self/cgroup for determining cgroups."
        )
        if cgroup_procinfo is None:
            my_cgroups = dict(_find_own_cgroups())
        else:
            my_cgroups = dict(_parse_proc_pid_cgroup(cgroup_procinfo))

        cgroupsParents = {}
        for subsystem, mount in cls._find_cgroup_mounts():
            # Ignore mount points where we do not have any access,
            # e.g. because a parent directory has insufficient permissions
            # (lxcfs mounts cgroups under /run/lxcfs in such a way).
            if os.access(mount, os.F_OK):
                cgroupPath = mount / my_cgroups[subsystem]
                fallbackPath = mount / _CGROUP_FALLBACK_PATH
                if (
                    fallback
                    and not os.access(cgroupPath, os.W_OK)
                    and os.path.isdir(fallbackPath)
                ):
                    cgroupPath = fallbackPath
                cgroupsParents[subsystem] = cgroupPath

        return cls(cgroupsParents)

    @classmethod
    def _find_cgroup_mounts(cls):
        """
        Return the information which subsystems are mounted where.
        @return a generator of tuples (subsystem, mountpoint)
        """
        try:
            with open("/proc/mounts", "rt") as mountsFile:
                for mount in mountsFile:
                    mount = mount.split(" ")
                    if mount[2] == "cgroup":
                        mountpoint = pathlib.Path(mount[1])
                        options = mount[3]
                        for option in options.split(","):
                            if option in cls.known_subsystems:
                                yield (option, mountpoint)
        except OSError:
            logging.exception("Cannot read /proc/mounts")

    def create_fresh_child_cgroup(self, subsystems):
        """
        Create child cgroups of the current cgroup for at least the given subsystems.
        @return: A Cgroup instance representing the new child cgroup(s).
        """
        assert set(subsystems).issubset(self.subsystems.keys())
        createdCgroupsPerSubsystem = {}
        createdCgroupsPerParent = {}
        for subsystem in subsystems:
            parentCgroup = self.subsystems[subsystem]
            if parentCgroup in createdCgroupsPerParent:
                # reuse already created cgroup
                createdCgroupsPerSubsystem[subsystem] = createdCgroupsPerParent[
                    parentCgroup
                ]
                continue

            cgroup = pathlib.Path(
                tempfile.mkdtemp(prefix=_CGROUP_NAME_PREFIX, dir=parentCgroup)
            )
            createdCgroupsPerSubsystem[subsystem] = cgroup
            createdCgroupsPerParent[parentCgroup] = cgroup

            # add allowed cpus and memory to cgroup if necessary
            # (otherwise we can't add any tasks)
            def copy_parent_to_child(name):
                shutil.copyfile(parentCgroup / name, cgroup / name)  # noqa: B023

            try:
                copy_parent_to_child("cpuset.cpus")
                copy_parent_to_child("cpuset.mems")
            except OSError:
                # expected to fail if cpuset subsystem is not enabled in this hierarchy
                pass

        return CgroupsV1(createdCgroupsPerSubsystem)

    def add_task(self, pid):
        """
        Add a process to the cgroups represented by this instance.
        """
        _register_process_with_cgrulesengd(pid)
        for cgroup in self.paths:
            with open(cgroup / "tasks", "w") as tasksFile:
                tasksFile.write(str(pid))

    def get_all_tasks(self, subsystem):
        """
        Return a generator of all PIDs currently in this cgroup for the given subsystem.
        """
        with open(self.subsystems[subsystem] / "tasks", "r") as tasksFile:
            for line in tasksFile:
                yield int(line)

    def kill_all_tasks(self):
        """
        Kill all tasks in this cgroup and all its children cgroups forcefully.
        Additionally, the children cgroups will be deleted.
        """

        def kill_all_tasks_in_cgroup_recursively(cgroup, delete):
            for dirpath, dirs, _files in os.walk(cgroup, topdown=False):
                for subCgroup in dirs:
                    subCgroup = os.path.join(dirpath, subCgroup)
                    _kill_all_tasks_in_cgroup(subCgroup, ensure_empty=delete)

                    if delete:
                        self._remove_cgroup(subCgroup)

            _kill_all_tasks_in_cgroup(cgroup, ensure_empty=delete)

        # First, we go through all cgroups recursively while they are frozen and kill
        # all processes. This helps against fork bombs and prevents processes from
        # creating new subgroups while we are trying to kill everything.
        # But this is only possible if we have freezer, and all processes will stay
        # until they are thawed (so we cannot check for cgroup emptiness and we cannot
        # delete subgroups).
        if self.FREEZE in self.subsystems:
            cgroup = self.subsystems[self.FREEZE]
            freezer_file = cgroup / "freezer.state"

            util.write_file("FROZEN", freezer_file)
            kill_all_tasks_in_cgroup_recursively(cgroup, delete=False)
            util.write_file("THAWED", freezer_file)

        # Second, we go through all cgroups again, kill what is left,
        # check for emptiness, and remove subgroups.
        # Furthermore, we do this for all hierarchies, not only the one with freezer.
        for cgroup in self.paths:
            kill_all_tasks_in_cgroup_recursively(cgroup, delete=True)

    def read_cputime(self):
        """
        Read the cputime usage of this cgroup. CPUACCT cgroup needs to be available.
        @return cputime usage in seconds
        """
        # convert nano-seconds to seconds
        return float(self.get_value(self.CPU, "usage")) / 1_000_000_000

    def read_max_mem_usage(self):
        # This measurement reads the maximum number of bytes of RAM+Swap the process used.
        # For more details, c.f. the kernel documentation:
        # https://www.kernel.org/doc/Documentation/cgroups/memory.txt
        memUsageFile = "memsw.max_usage_in_bytes"
        if not self.has_value(self.MEMORY, memUsageFile):
            memUsageFile = "max_usage_in_bytes"
        if self.has_value(self.MEMORY, memUsageFile):
            try:
                return int(self.get_value(self.MEMORY, memUsageFile))
            except OSError as e:
                if e.errno == errno.ENOTSUP:
                    # kernel responds with operation unsupported if this is disabled
                    logging.critical(
                        "Kernel does not track swap memory usage, cannot measure memory usage."
                        " Please set swapaccount=1 on your kernel command line."
                    )
                else:
                    raise e

        return None

    def read_mem_pressure(self):
        logging.debug("Pressure metrics not supported in cgroups v1")

    def read_cpu_pressure(self):
        logging.debug("Pressure metrics not supported in cgroups v1")

    def read_io_pressure(self):
        logging.debug("Pressure metrics not supported in cgroups v1")

    def read_usage_per_cpu(self):
        usage = {}
        for (core, coretime) in enumerate(
            self.get_value(self.CPU, "usage_percpu").split(" ")
        ):
            try:
                coretime = int(coretime)
                if coretime != 0:
                    # convert nanoseconds to seconds
                    usage[core] = coretime / 1_000_000_000
            except (OSError, ValueError) as e:
                logging.debug(
                    "Could not read CPU time for core %s from kernel: %s", core, e
                )

        return usage

    def read_available_cpus(self):
        return util.parse_int_list(self.get_value(self.CPUSET, "cpus"))

    def read_available_mems(self):
        return util.parse_int_list(self.get_value(self.CPUSET, "mems"))

    def read_io_stat(self):
        blkio_bytes_file = "throttle.io_service_bytes"
        bytes_read = 0
        bytes_written = 0
        for blkio_line in self.get_file_lines(self.IO, blkio_bytes_file):
            try:
                dev_no, io_type, bytes_amount = blkio_line.split(" ")
                if io_type == "Read":
                    bytes_read += int(bytes_amount)
                elif io_type == "Write":
                    bytes_written += int(bytes_amount)
            except ValueError:
                pass  # There are irrelevant lines in this file with a different structure
        return bytes_read, bytes_written

    def has_tasks(self, path):
        return bool((path / "cgroup.procs").read_bytes().strip())

    def write_memory_limit(self, limit):
        limit_file = "limit_in_bytes"
        self.set_value(self.MEMORY, limit_file, limit)

        swap_limit_file = "memsw.limit_in_bytes"
        # We need swap limit because otherwise the kernel just starts swapping
        # out our process if the limit is reached.
        # Some kernels might not have this feature,
        # which is ok if there is actually no swap.
        if not self.has_value(self.MEMORY, swap_limit_file):
            if systeminfo.has_swap():
                sys.exit(
                    'Kernel misses feature for accounting swap memory, but machine has swap. Please set swapaccount=1 on your kernel command line or disable swap with "sudo swapoff -a".'
                )
        else:
            try:
                self.set_value(self.MEMORY, swap_limit_file, limit)
            except OSError as e:
                if e.errno == errno.ENOTSUP:
                    # kernel responds with operation unsupported if this is disabled
                    sys.exit(
                        'Memory limit specified, but kernel does not allow limiting swap memory. Please set swapaccount=1 on your kernel command line or disable swap with "sudo swapoff -a".'
                    )
                raise e

    def read_memory_limit(self):
        return int(self.get_value(self.MEMORY, "limit_in_bytes"))

    def disable_swap(self):
        # Note that this disables swapping completely according to
        # https://www.kernel.org/doc/Documentation/cgroups/memory.txt
        # (unlike setting the global swappiness to 0).
        # Our process might get killed because of this.
        return self.set_value(self.MEMORY, "swappiness", "0")

    def read_oom_count(self):
        # not supported in v1, see oomhandler and memory_used > memlimit impl
        return None
