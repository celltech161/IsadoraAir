"""Kernel-enforced resource confinement for ONE media-tool process.

Usage (only ever invoked by production.services.confinement):

    python -I -S confined_exec.py --memory BYTES --cpu SECONDS --fsize BYTES
        --nofile N -- /absolute/tool arg ...

Applies setrlimit() limits to itself and then exec()s the tool, so the limits
are inherited by the tool and by every process it creates:

    RLIMIT_AS      virtual address space  -> allocations beyond it fail
    RLIMIT_CPU     CPU seconds            -> SIGXCPU at the soft limit,
                                             SIGKILL 5 s later (hard limit)
    RLIMIT_FSIZE   largest file it may write -> SIGXFSZ beyond it
    RLIMIT_NOFILE  open descriptors
    RLIMIT_CORE    0 (no core dumps of untrusted-input crashes)

A requested limit higher than the current hard limit is clamped DOWN to it
(still confined, only stricter). If any limit cannot be applied or read back,
the tool is NOT run: exit 125 with a ``confined-exec:`` marker on stderr --
callers treat that as "confinement unavailable" and fail closed. A failed
exec exits 126 with the same marker.

Standard library only and nothing imported from Django or the application:
this file runs with ``-I -S`` before the untrusted work begins, and it is the
unprivileged child of the web process -- never a privilege boundary.
"""
import os
import resource
import sys

MARKER = "confined-exec:"
EXIT_LIMITS = 125
EXIT_EXEC = 126
CPU_HARD_GRACE_SECONDS = 5


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
    names = {"--memory", "--cpu", "--fsize", "--nofile"}
    it = iter(options)
    for name in it:
        if name not in names:
            _fail(EXIT_LIMITS, f"usage: unknown option {name!r}")
        try:
            value = int(next(it))
        except (StopIteration, ValueError):
            _fail(EXIT_LIMITS, f"usage: {name} needs an integer")
        if value <= 0:
            _fail(EXIT_LIMITS, f"usage: {name} must be positive")
        values[name] = value
    if set(values) != names:
        _fail(EXIT_LIMITS, "usage: --memory, --cpu, --fsize and --nofile are all required")
    return values, command


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
        _apply(resource.RLIMIT_CORE, 0, 0)
        _apply(resource.RLIMIT_AS, values["--memory"], values["--memory"])
        _apply(resource.RLIMIT_CPU, values["--cpu"], values["--cpu"] + CPU_HARD_GRACE_SECONDS)
        _apply(resource.RLIMIT_FSIZE, values["--fsize"], values["--fsize"])
        _apply(resource.RLIMIT_NOFILE, values["--nofile"], values["--nofile"])
    except (OSError, ValueError) as exc:
        _fail(EXIT_LIMITS, f"cannot apply resource limits: {exc}")
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
