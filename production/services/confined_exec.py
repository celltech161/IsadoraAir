"""Kernel-enforced confinement for ONE media-tool process tree.

Usage (only ever invoked by production.services.confinement):

    python -I -S confined_exec.py --cgroup /sys/fs/cgroup/.../run-X
        --memory BYTES --cpu SECONDS --fsize BYTES --nofile N -- /abs/tool arg ...

Before the untrusted tool runs, this launcher (trusted, standard library only)
establishes, in this order, and then exec()s the tool:

1. **cgroup** -- it moves ITSELF into the per-run leaf cgroup ``--cgroup``
   (prepared and limited by the parent: aggregate memory, task count, CPU) and
   verifies the move. Cgroup membership is inherited by every descendant and
   is NOT changed by setsid(), setpgid(), double forking or a parent exiting,
   so the parent can kill the whole tree with ``cgroup.kill`` and the kernel
   accounts all of it against the leaf's limits.
2. **no_new_privs** -- nothing the tool runs can gain privileges (setuid,
   file capabilities); required for 3 and 4.
3. **Landlock** -- the tool tree may READ and EXECUTE files but may not write,
   create, remove, rename or truncate anything on any filesystem (the only
   exception is opening /dev/null), so it cannot write ``cgroup.procs`` /
   ``cgroup.threads`` to migrate out of its leaf. Landlock also confines
   ptrace to the sandbox, and -- where the kernel supports it -- denies TCP
   bind/connect and scopes signals and abstract unix sockets to the sandbox.
4. **seccomp** -- ``clone3`` fails with ENOSYS (libc falls back to clone), so
   ``CLONE_INTO_CGROUP`` -- the one way to start a process in another cgroup
   without writing ``cgroup.procs`` -- is unavailable; syscalls of a foreign
   ABI (x32 / 32-bit compat) kill the process.
5. **rlimits** -- per-process backstops inside the aggregate limits:
   RLIMIT_AS, RLIMIT_CPU (SIGXCPU, SIGKILL 5 s later), RLIMIT_FSIZE,
   RLIMIT_NOFILE, RLIMIT_CORE 0.

If ANY step cannot be established or verified, the tool is NOT run: exit 125
with a ``confined-exec:`` marker on stderr, which the caller reports as
"confinement unavailable" (fail closed). A failed exec exits 126.

Nothing here is privileged: the leaf belongs to the service's own delegated
cgroup subtree, Landlock and seccomp are unprivileged kernel facilities.
"""
import ctypes
import os
import platform
import resource
import struct
import sys

MARKER = "confined-exec:"
EXIT_LIMITS = 125
EXIT_EXEC = 126
CPU_HARD_GRACE_SECONDS = 5
CGROUP_MOUNT = "/sys/fs/cgroup"

# -- Landlock (include/uapi/linux/landlock.h) ---------------------------------
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
_FS_ABI1 = (1 << 13) - 1            # EXECUTE .. MAKE_SYM
_FS_REFER = 1 << 13                 # ABI 2
_FS_TRUNCATE = 1 << 14              # ABI 3
_FS_IOCTL_DEV = 1 << 15             # ABI 5
_NET_BIND_TCP = 1 << 0              # ABI 4
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1

# -- seccomp ------------------------------------------------------------------
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_ENOSYS = 38
_NR_CLONE3 = 435                    # same number on every 64-bit Linux ABI
_X32_SYSCALL_BIT = 0x40000000
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}


class _Refused(Exception):
    pass


def _fail(code, message):
    sys.stderr.write(f"{MARKER} {message}\n")
    sys.stderr.flush()
    os._exit(code)


def _parse(argv):
    if "--" not in argv:
        _fail(EXIT_LIMITS, "usage: missing -- before the command")
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    if not command or not os.path.isabs(command[0]):
        _fail(EXIT_LIMITS, "usage: the command must be an absolute path")
    values = {}
    numeric = {"--memory", "--cpu", "--fsize", "--nofile"}
    it = iter(options)
    for name in it:
        try:
            raw = next(it)
        except StopIteration:
            _fail(EXIT_LIMITS, f"usage: {name} needs a value")
        if name == "--cgroup":
            values[name] = raw
            continue
        if name not in numeric:
            _fail(EXIT_LIMITS, f"usage: unknown option {name!r}")
        try:
            value = int(raw)
        except ValueError:
            _fail(EXIT_LIMITS, f"usage: {name} needs an integer")
        if value <= 0:
            _fail(EXIT_LIMITS, f"usage: {name} must be positive")
        values[name] = value
    if set(values) != numeric | {"--cgroup"}:
        _fail(EXIT_LIMITS, "usage: --cgroup, --memory, --cpu, --fsize and --nofile are all required")
    return values, command


# -- 1. cgroup ----------------------------------------------------------------

def own_cgroup():
    with open("/proc/self/cgroup", encoding="ascii") as handle:
        for line in handle:
            if line.startswith("0::"):
                return line[3:].strip()
    raise _Refused("no cgroup v2 membership")


def _join_cgroup(leaf):
    real = os.path.realpath(leaf)
    if real != leaf or not real.startswith(CGROUP_MOUNT + "/"):
        raise _Refused(f"cgroup {leaf!r} is not a canonical path under {CGROUP_MOUNT}")
    fd = os.open(os.path.join(leaf, "cgroup.procs"), os.O_WRONLY | os.O_CLOEXEC)
    try:
        os.write(fd, b"0")
    finally:
        os.close(fd)
    if own_cgroup() != leaf[len(CGROUP_MOUNT):]:
        raise _Refused("cgroup move did not take effect")


# -- 2./3. no_new_privs + Landlock --------------------------------------------

def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    return libc


def _check(result, what):
    if result < 0:
        raise _Refused(f"{what}: {os.strerror(ctypes.get_errno())}")
    return result


def _no_new_privs(libc):
    _check(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
    if libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise _Refused("no_new_privs did not take effect")


def landlock_abi(libc=None):
    libc = libc or _libc()
    return libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))


def _landlock(libc):
    abi = landlock_abi(libc)
    if abi < 1:
        raise _Refused("Landlock is not available")
    handled_fs = _FS_ABI1
    if abi >= 2:
        handled_fs |= _FS_REFER
    if abi >= 3:
        handled_fs |= _FS_TRUNCATE
    if abi >= 5:
        handled_fs |= _FS_IOCTL_DEV
    handled_net = (_NET_BIND_TCP | _NET_CONNECT_TCP) if abi >= 4 else 0
    scoped = (_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if abi >= 6 else 0
    attr = struct.pack("=QQQ", handled_fs, handled_net, scoped)
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = _check(libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, buf, ctypes.c_size_t(size), ctypes.c_uint32(0)),
                     "landlock_create_ruleset")
    try:
        for path, allowed in (("/", FS_EXECUTE | FS_READ_FILE | FS_READ_DIR),
                              ("/dev/null", FS_READ_FILE | FS_WRITE_FILE)):
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = ctypes.create_string_buffer(struct.pack("=Qi", allowed, fd), 12)
                _check(libc.syscall(_SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset),
                                    ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH), rule, ctypes.c_uint32(0)),
                       f"landlock_add_rule({path})")
            finally:
                os.close(fd)
        _check(libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)),
               "landlock_restrict_self")
    finally:
        os.close(ruleset)
    return abi


# -- 4. seccomp ---------------------------------------------------------------

class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _seccomp(libc):
    arch = _AUDIT_ARCH.get(platform.machine())
    if arch is None:
        raise _Refused(f"no seccomp policy for architecture {platform.machine()!r}")
    ld_w_abs, jeq_k, jge_k, ret_k = 0x20, 0x15, 0x35, 0x06
    program = [
        (ld_w_abs, 0, 0, 4),                            # 0: A = seccomp_data.arch
        (jeq_k, 1, 0, arch),                            # 1: native ABI? -> 3
        (ret_k, 0, 0, _SECCOMP_RET_KILL_PROCESS),       # 2: foreign ABI: kill
        (ld_w_abs, 0, 0, 0),                            # 3: A = seccomp_data.nr
        (jge_k, 3, 0, _X32_SYSCALL_BIT),                # 4: x32 syscall? -> 8 (kill)
        (jeq_k, 0, 1, _NR_CLONE3),                      # 5: clone3? -> 6 else 7
        (ret_k, 0, 0, _SECCOMP_RET_ERRNO | _ENOSYS),    # 6: clone3 -> ENOSYS
        (ret_k, 0, 0, _SECCOMP_RET_ALLOW),              # 7: everything else
        (ret_k, 0, 0, _SECCOMP_RET_KILL_PROCESS),       # 8
    ]
    filters = (_SockFilter * len(program))(*[_SockFilter(*insn) for insn in program])
    fprog = _SockFprog(len(program), filters)
    _check(libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0), "seccomp")


# -- 5. rlimits ---------------------------------------------------------------

def _apply(which, soft, hard):
    _cur_soft, cur_hard = resource.getrlimit(which)
    if cur_hard != resource.RLIM_INFINITY:
        hard = min(hard, cur_hard)
        soft = min(soft, hard)
    resource.setrlimit(which, (soft, hard))
    if resource.getrlimit(which) != (soft, hard):
        raise OSError(f"limit {which} did not apply")


def main(argv):
    values, command = _parse(argv)
    try:
        _join_cgroup(values["--cgroup"])
        libc = _libc()
        _no_new_privs(libc)
        _landlock(libc)
        _seccomp(libc)
        _apply(resource.RLIMIT_CORE, 0, 0)
        _apply(resource.RLIMIT_AS, values["--memory"], values["--memory"])
        _apply(resource.RLIMIT_CPU, values["--cpu"], values["--cpu"] + CPU_HARD_GRACE_SECONDS)
        _apply(resource.RLIMIT_FSIZE, values["--fsize"], values["--fsize"])
        _apply(resource.RLIMIT_NOFILE, values["--nofile"], values["--nofile"])
    except (_Refused, OSError, ValueError) as exc:
        _fail(EXIT_LIMITS, f"cannot establish confinement: {exc}")
    try:
        os.nice(10)
    except OSError:
        pass            # a lower priority is a courtesy, not a safety property
    try:
        os.execv(command[0], command)
    except OSError as exc:
        _fail(EXIT_EXEC, f"cannot execute {command[0]}: {exc.strerror}")


if __name__ == "__main__":
    main(sys.argv[1:])
