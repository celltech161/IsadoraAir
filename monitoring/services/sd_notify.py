"""Minimal, dependency-free systemd `sd_notify` client -- P1 1.11.

Just enough of the protocol (see `sd_notify(3)`/`systemd.exec(5)`'s
"Debugging and Testing" and "Process Exit Codes" sections, and
`sd_watchdog_enabled(3)`) to support `WATCHDOG=1` keepalives for a
long-running service supervised by systemd's `WatchdogSec=`. No
dependency on the `python-systemd` or `sdnotify` PyPI packages -- the
wire protocol is a small, stable, newline-separated `KEY=VALUE`
datagram sent over an `AF_UNIX SOCK_DGRAM` socket named by
`$NOTIFY_SOCKET`, unchanged since systemd v183 (2012); nothing here
depends on a systemd feature newer than that.

Deliberately narrow: only the calls this project actually needs
(`READY=1` is exposed for completeness/tests but is NOT required by
`deploy/isadoraair-monitoring.service`, which stays `Type=simple` --
see that file's own comment for why `Type=notify` was NOT adopted).

Running outside systemd (`$NOTIFY_SOCKET` unset -- a bare
`manage.py run_monitoring` from a developer's terminal, CI, a plain
`docker run`) is a harmless, silent no-op everywhere in this module:
every public function checks for the environment first and returns
`False`/`None` rather than raising or printing. A process that IS
running under systemd but whose datagram can't be delivered (socket
gone, permission error, systemd itself wedged) also just gets `False`
back -- see `notify()`'s own docstring for why swallowing that failure
is the CORRECT behavior here, not a gap."""
import os
import socket

# WATCHDOG_USEC/WATCHDOG_PID and NOTIFY_SOCKET are systemd's own fixed
# environment-variable names (systemd.exec(5)) -- not configurable, not
# guessed.
_ENV_NOTIFY_SOCKET = "NOTIFY_SOCKET"
_ENV_WATCHDOG_USEC = "WATCHDOG_USEC"
_ENV_WATCHDOG_PID = "WATCHDOG_PID"

_SEND_TIMEOUT_SECONDS = 1.0


def _notify_socket_address():
    """Resolve $NOTIFY_SOCKET into a connectable AF_UNIX address, or
    None if this process was not started with systemd notification
    access (NotifyAccess=none, the default, or no systemd at all).

    A leading '@' is systemd's own convention for the Linux ABSTRACT
    namespace (see `unix(7)`) -- translated here to the NUL-prefixed
    form Python's `socket` module expects for an abstract AF_UNIX
    address. A path NOT starting with '@' is an ordinary filesystem
    socket path, used as-is."""
    addr = os.environ.get(_ENV_NOTIFY_SOCKET)
    if not addr:
        return None
    if addr.startswith("@"):
        return "\0" + addr[1:]
    return addr


def notify(*, ready=False, watchdog=False, stopping=False, status=None):
    """Best-effort sd_notify datagram. Returns True only if a message
    was actually handed to the kernel socket successfully; False if
    there was nothing to send (no NOTIFY_SOCKET -- the expected,
    unremarkable case outside systemd) or the caller asked for
    nothing at all.

    Never raises. A send failure (ENOENT if the socket path vanished,
    ECONNREFUSED if nothing is listening, a permission error, ...) is
    caught and reported as a plain `False` return -- deliberately NOT
    surfaced as an exception. Rationale (see also this module's own
    docstring and monitor.py's call site): the one notification this
    project actually depends on is WATCHDOG=1, and its entire purpose
    is to tell systemd "I am still healthy." If we can't reliably
    DELIVER that claim, the conservative, fail-safe outcome is for
    systemd's own watchdog timer to elapse and treat the process as
    unhealthy (triggering Restart=on-failure) -- exactly as if the
    keepalive had never been sent. Raising here instead would risk
    crashing an otherwise-healthy monitoring cycle over a pure
    notification-transport hiccup, which would make the SYMPTOM look
    like the poller itself failed rather than just its watchdog ping --
    a strictly worse outcome for the "poller must stay running and
    keep writing real check results" goal this whole feature protects.
    """
    address = _notify_socket_address()
    if address is None:
        return False

    fields = []
    if ready:
        fields.append("READY=1")
    if watchdog:
        fields.append("WATCHDOG=1")
    if stopping:
        fields.append("STOPPING=1")
    if status is not None:
        # STATUS is a single free-form status line in systemd's own
        # protocol -- strip embedded newlines so a caller-supplied
        # string can never smuggle in a second, unintended field.
        fields.append("STATUS=" + str(status).replace("\n", " ").replace("\r", " "))
    if not fields:
        return False

    message = "\n".join(fields).encode("utf-8")
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.settimeout(_SEND_TIMEOUT_SECONDS)
        sock.connect(address)
        sock.send(message)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def watchdog_enabled():
    """True only if systemd has asked THIS EXACT PROCESS to send
    WATCHDOG=1 keepalives (i.e. `WatchdogSec=` is configured on the
    unit that started us, AND we are the process systemd is actually
    watching).

    Mirrors `sd_watchdog_enabled(3)`'s own documented contract:
    `WATCHDOG_PID`, when present, must match our own pid. A forked
    child that happens to inherit the parent's environment (e.g. a
    management-command subprocess, a test runner's worker) must NOT
    conclude it is being watched and start sending keepalives on the
    supervised parent's behalf -- systemd is watching the PID it
    started, not any process that happens to see the same env vars."""
    usec = os.environ.get(_ENV_WATCHDOG_USEC)
    if not usec or not usec.isdigit() or int(usec) <= 0:
        return False
    watched_pid = os.environ.get(_ENV_WATCHDOG_PID)
    if watched_pid and watched_pid.isdigit() and int(watched_pid) != os.getpid():
        return False
    return True


def watchdog_interval_seconds():
    """The WatchdogSec= interval (in seconds) systemd told this process
    about via $WATCHDOG_USEC, or None if watchdog supervision is not
    active for this exact process (see watchdog_enabled()).

    Not currently required by monitor.py's own keepalive cadence (its
    fixed POLL_SECONDS loop already paces WATCHDOG=1 sends comfortably
    inside the configured interval -- see deploy/isadoraair-
    monitoring.service's own WatchdogSec= comment for the arithmetic).
    Exposed for tests and any future caller that wants to self-check
    its own margin rather than trusting the deploy-time constant alone."""
    if not watchdog_enabled():
        return None
    try:
        return int(os.environ[_ENV_WATCHDOG_USEC]) / 1_000_000.0
    except (KeyError, ValueError):
        return None
