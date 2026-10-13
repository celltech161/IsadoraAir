"""Read-only host preflight for what r0107's iPortal work requires of a station.

Run by the protected updater BEFORE any application mutation -- before its
database checkpoint, any migration, the checkout advance, unit installation or
any service start -- through the staged TARGET code's migration preflight for
``production.0001_initial`` (updatecenter/management/commands/
updatecenter_migration_preflight.py). That migration is pending exactly when a
station first takes on ProductionMedia (r0106 -> r0107), and the preflight runs
as the application account, which is also the account the validation service
and the runtime use. A station that cannot provide the boundary is refused
there (MIGRATION_PREFLIGHT_BLOCKED) instead of installing r0107 and then
discovering that ``isadoraair-validation`` can never become ready.

Nothing here writes, creates, starts or reconfigures anything. Two checks:

* ``validation_host_capability`` -- what deploy/isadoraair-validation.service
  and production.services.confinement / confined_exec need (docs/IPORTAL.md,
  "Required kernel facilities"): a unified cgroup v2 hierarchy; the cpu,
  memory and pids controllers available for delegation; the per-leaf
  interface files the executor requires (cgroup.kill, memory.swap.max,
  memory.oom.group, pids.max, cpu.max); systemd >= 254 (DelegateSubgroup=);
  and a FUNCTIONAL sandbox self-test -- a throwaway child applies the
  launcher's own no_new_privs, Landlock and seccomp routines and proves they
  bite (a write is refused, clone3 returns ENOSYS).
* ``media_root_establishable`` -- PRODUCTION_MEDIA_ROOT passes
  production.root_policy, and either already is a usable dedicated directory
  of this account or can be created by it on first use (the runtime creates
  the root and media/, incoming/, work/, locks/ itself, 0750, through
  production.services.layout.ensure_layout -- no operator step).

The validation service keeps failing closed on its own whatever this says;
this preflight only moves the refusal ahead of every mutation.

r0109 adds ``render_host_capability`` for ``production.0002_referenceaudio``
(pending exactly when a station first takes on Phase C spoken content):
deploy/isadoraair-render.service needs the same delegated cgroup facilities,
and every render step runs under the launcher's narrower ``render`` profile.
Its FUNCTIONAL self-test applies exactly that profile in a throwaway child and
proves it bites -- a write is refused, a file outside the render read roots
cannot be read, socket(2) fails, clone3 returns ENOSYS -- and that the very
ffmpeg the render service will use (render_service.configured_ffmpeg) still
runs and reports its version under it. The real system manager's delegation
of the unit's own subtree remains a post-install acceptance check.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED_CONTROLLERS = ("cpu", "memory", "pids")
# The files production.services.confinement._prepare_leaf requires in every
# run leaf, keyed by the controller that provides them ("" = cgroup core).
REQUIRED_INTERFACE_FILES = {
    "cgroup.kill": "",
    "memory.max": "memory",
    "memory.swap.max": "memory",
    "memory.oom.group": "memory",
    "pids.max": "pids",
    "cpu.max": "cpu",
}
MINIMUM_SYSTEMD = 254                      # DelegateSubgroup=
SYSTEMCTL_CANDIDATES = ("/usr/bin/systemctl", "/bin/systemctl")
SELFTEST_TIMEOUT_SECONDS = 30
_ENOSYS = 38

# The child: load the launcher by path (stdlib only, isolated interpreter),
# apply exactly its sandbox, then prove it is in force.
_SELFTEST = r"""
import ctypes, importlib.util, os, sys
spec = importlib.util.spec_from_file_location("confined_exec_selftest", sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
probe = sys.argv[2]
try:
    libc = launcher._libc()
    launcher._no_new_privs(libc)
    abi = launcher._landlock(libc)
    launcher._seccomp(libc)
except launcher._Refused as exc:
    print("refused", exc)
    sys.exit(3)
try:
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except PermissionError:
    write = "refused"
else:
    os.close(fd)
    write = "ALLOWED"
result = libc.syscall(435, None, ctypes.c_size_t(0))
print("sandbox", abi, write, result, ctypes.get_errno())
"""


# The render-profile child: everything it needs is imported BEFORE the sandbox
# (afterwards it may read only the render roots), then it proves each refusal
# and runs the toolchain the way a render step does (a plain fork/exec, no
# shell), printing one parseable line.
_RENDER_SELFTEST = r"""
import ctypes, errno, importlib.util, os, socket, subprocess, sys
spec = importlib.util.spec_from_file_location("confined_exec_render_selftest", sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
probe, outside, ffmpeg = sys.argv[2], sys.argv[3], sys.argv[4]
try:
    libc = launcher._libc()
    launcher._no_new_privs(libc)
    abi = launcher._landlock(libc, "render")
    launcher._seccomp(libc, deny_sockets=True)
except launcher._Refused as exc:
    print("refused", exc)
    sys.exit(3)
def attempt(action):
    try:
        action()
    except OSError as exc:
        return "refused" if exc.errno in (errno.EACCES, errno.EPERM) else f"errno-{exc.errno}"
    return "ALLOWED"
write = attempt(lambda: os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)))
read = attempt(lambda: os.close(os.open(outside, os.O_RDONLY)))
sock = attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
clone3 = libc.syscall(435, None, ctypes.c_size_t(0))
clone3_errno = ctypes.get_errno()
try:
    tool = subprocess.run([ffmpeg, "-hide_banner", "-version"], stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=20, check=False)
    returncode, first = tool.returncode, (tool.stdout.decode("ascii", "replace").splitlines() or [""])[0]
except (OSError, subprocess.TimeoutExpired) as exc:
    returncode, first = -1, type(exc).__name__
print("render", abi, write, read, sock, clone3, clone3_errno, returncode, first.strip()[:100])
"""


def _facility(ok: bool, detail: str) -> dict:
    return {"ok": bool(ok), "detail": detail[:300]}


def _read(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def cgroup_v2_unified(*, proc: Path = Path("/proc"), cgroup_mount: Path = Path("/sys/fs/cgroup")) -> dict:
    try:
        lines = (proc / "self" / "mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return _facility(False, f"cannot read mountinfo: {exc.strerror}")
    types = []
    for line in lines:
        fields, _, tail = line.partition(" - ")
        parts = fields.split()
        if len(parts) >= 5 and parts[4] == str(cgroup_mount):
            types.append(tail.split()[0] if tail else "")
    if not types:
        return _facility(False, f"nothing is mounted at {cgroup_mount}")
    if types[-1] != "cgroup2":
        return _facility(False, f"{cgroup_mount} is {types[-1]!r}, not a unified cgroup2 hierarchy")
    return _facility(True, f"{cgroup_mount} is cgroup2")


def controllers_available(*, cgroup_mount: Path = Path("/sys/fs/cgroup")) -> dict:
    try:
        available = set(_read(cgroup_mount / "cgroup.controllers").split())
    except OSError as exc:
        return _facility(False, f"cannot read the root cgroup.controllers: {exc.strerror}")
    missing = [name for name in REQUIRED_CONTROLLERS if name not in available]
    if missing:
        return _facility(False, f"controller(s) not available for delegation: {', '.join(missing)}")
    return _facility(True, f"{', '.join(REQUIRED_CONTROLLERS)} available")


def _candidate_cgroups(proc: Path, cgroup_mount: Path) -> list[Path]:
    """Non-root cgroups to inspect: this process's own ancestry, then the
    root's direct children (system.slice, user.slice, ...)."""
    candidates = []
    try:
        for line in (proc / "self" / "cgroup").read_text(encoding="ascii").splitlines():
            if line.startswith("0::"):
                path = cgroup_mount / line[3:].lstrip("/")
                while path != cgroup_mount and cgroup_mount in path.parents:
                    candidates.append(path)
                    path = path.parent
    except OSError:
        pass
    try:
        candidates += sorted(entry for entry in cgroup_mount.iterdir()
                             if entry.is_dir() and not entry.is_symlink())
    except OSError:
        pass
    return list(dict.fromkeys(candidates))


def leaf_interfaces(*, proc: Path = Path("/proc"), cgroup_mount: Path = Path("/sys/fs/cgroup")) -> dict:
    """Each interface file a run leaf needs must exist in SOME non-root cgroup
    where its controller is enabled -- proof the kernel provides it (e.g.
    memory.swap.max is absent when swap accounting is unavailable)."""
    candidates = _candidate_cgroups(proc, cgroup_mount)
    missing = []
    for name, controller in REQUIRED_INTERFACE_FILES.items():
        found = False
        for cgroup in candidates:
            try:
                if controller and controller not in _read(cgroup / "cgroup.controllers").split():
                    continue
                info = os.lstat(cgroup / name)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found = True
                break
        if not found:
            missing.append(name)
    if missing:
        return _facility(False, f"kernel does not provide: {', '.join(missing)} "
                                f"(checked {len(candidates)} cgroup(s))")
    return _facility(True, ", ".join(REQUIRED_INTERFACE_FILES))


def systemd_version(*, systemctl_candidates=SYSTEMCTL_CANDIDATES) -> dict:
    executable = next((path for path in systemctl_candidates if os.path.isfile(path)), None)
    if executable is None:
        return _facility(False, "systemctl not found")
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _facility(False, f"systemctl --version failed: {exc}")
    match = re.match(rb"systemd ([0-9]{1,5})\b", result.stdout)
    if result.returncode != 0 or match is None:
        return _facility(False, "cannot determine the systemd version")
    version = int(match.group(1))
    if version < MINIMUM_SYSTEMD:
        return _facility(False, f"systemd {version} lacks DelegateSubgroup= (needs >= {MINIMUM_SYSTEMD})")
    return _facility(True, f"systemd {version}")


def sandbox_selftest(*, launcher: Path | None = None, python: str = sys.executable) -> dict:
    """The launcher's own no_new_privs + Landlock + seccomp, applied in a
    throwaway child that must then be unable to create a file and must see
    clone3 fail with ENOSYS."""
    if launcher is None:
        launcher = Path(__file__).with_name("confined_exec.py")
    with tempfile.TemporaryDirectory(prefix="isadoraair-sandbox-selftest-") as scratch:
        probe = Path(scratch) / "must-not-exist"
        try:
            result = subprocess.run(
                [python, "-I", "-S", "-c", _SELFTEST, str(launcher), str(probe)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=SELFTEST_TIMEOUT_SECONDS,
                check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _facility(False, f"sandbox self-test could not run: {exc}")
        escaped = probe.exists()
    output = result.stdout.decode("ascii", "replace").strip()
    match = re.fullmatch(r"sandbox (-?\d+) (\S+) (-?\d+) (\d+)", output)
    if result.returncode != 0 or match is None:
        detail = output or result.stderr.decode("utf-8", "replace").strip()
        return _facility(False, f"sandbox self-test failed: {detail}")
    abi, write, clone3, errno_value = int(match.group(1)), match.group(2), int(match.group(3)), int(match.group(4))
    if abi < 1:
        return _facility(False, "Landlock is not available")
    if write != "refused" or escaped:
        return _facility(False, "Landlock did not refuse a file write")
    if clone3 != -1 or errno_value != _ENOSYS:
        return _facility(False, f"seccomp did not refuse clone3 (result {clone3}, errno {errno_value})")
    return _facility(True, f"no_new_privs, Landlock ABI {abi}, seccomp (clone3 -> ENOSYS)")


def render_sandbox_selftest(*, launcher: Path | None = None, python: str = sys.executable,
                            ffmpeg: str | None = None) -> dict:
    """The launcher's render profile (Landlock render read roots + seccomp
    with socket(2) denied), applied in a throwaway child: no write, no read
    outside the render roots, no socket, no clone3 -- and the render ffmpeg
    still runs under it."""
    if launcher is None:
        launcher = Path(__file__).with_name("confined_exec.py")
    if ffmpeg is None:
        from production.services import render_service
        try:
            ffmpeg = render_service.configured_ffmpeg()
        except render_service.NotReady as exc:
            return _facility(False, f"no render toolchain: {exc}")
    with tempfile.TemporaryDirectory(prefix="isadoraair-render-selftest-") as scratch:
        probe, outside = Path(scratch) / "must-not-exist", Path(scratch) / "outside-the-render-roots"
        outside.write_bytes(b"not for a renderer\n")
        try:
            result = subprocess.run(
                [python, "-I", "-S", "-c", _RENDER_SELFTEST, str(launcher), str(probe), str(outside), str(ffmpeg)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=SELFTEST_TIMEOUT_SECONDS,
                check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _facility(False, f"render sandbox self-test could not run: {exc}")
        escaped = probe.exists()
    output = result.stdout.decode("ascii", "replace").strip()
    match = re.fullmatch(r"render (-?\d+) (\S+) (\S+) (\S+) (-?\d+) (\d+) (-?\d+) ?(.*)", output)
    if result.returncode != 0 or match is None:
        detail = output or result.stderr.decode("utf-8", "replace").strip()
        return _facility(False, f"render sandbox self-test failed: {detail}")
    abi, write, read, sock = int(match.group(1)), match.group(2), match.group(3), match.group(4)
    clone3, errno_value, tool_rc, version = int(match.group(5)), int(match.group(6)), int(match.group(7)), match.group(8)
    if abi < 1:
        return _facility(False, "Landlock is not available")
    if write != "refused" or escaped:
        return _facility(False, "the render profile did not refuse a file write")
    if read != "refused":
        return _facility(False, "the render profile did not refuse a read outside the render roots")
    if sock != "refused":
        return _facility(False, "the render profile did not refuse socket(2)")
    if clone3 != -1 or errno_value != _ENOSYS:
        return _facility(False, f"seccomp did not refuse clone3 (result {clone3}, errno {errno_value})")
    if tool_rc != 0 or not version.startswith("ffmpeg version"):
        return _facility(False, f"{ffmpeg} does not run under the render profile (exit {tool_rc}: {version!r})")
    return _facility(True, f"render profile: Landlock ABI {abi}, no write/outside read/socket, clone3 -> ENOSYS; "
                           f"{version}")


def _host_facilities(overrides) -> dict:
    return {
        "cgroup_v2_unified": cgroup_v2_unified(**{k: v for k, v in overrides.items()
                                                 if k in ("proc", "cgroup_mount")}),
        "controllers": controllers_available(**{k: v for k, v in overrides.items() if k == "cgroup_mount"}),
        "leaf_interface_files": leaf_interfaces(**{k: v for k, v in overrides.items()
                                                    if k in ("proc", "cgroup_mount")}),
        "systemd_delegate_subgroup": systemd_version(**{k: v for k, v in overrides.items()
                                                         if k == "systemctl_candidates"}),
    }


def check_render_host(**overrides) -> dict:
    """r0109 evidence for the updater: what deploy/isadoraair-render.service
    and the render profile need (``ok`` is popped by the preflight runner)."""
    facilities = _host_facilities(overrides)
    facilities["render_sandbox"] = render_sandbox_selftest(
        **{k: v for k, v in overrides.items() if k in ("launcher", "python", "ffmpeg")})
    return {"ok": all(item["ok"] for item in facilities.values()), "facilities": facilities}


def check_validation_host(**overrides) -> dict:
    """Evidence for the updater (``ok`` is popped by the preflight runner)."""
    facilities = {
        "cgroup_v2_unified": cgroup_v2_unified(**{k: v for k, v in overrides.items()
                                                 if k in ("proc", "cgroup_mount")}),
        "controllers": controllers_available(**{k: v for k, v in overrides.items() if k == "cgroup_mount"}),
        "leaf_interface_files": leaf_interfaces(**{k: v for k, v in overrides.items()
                                                    if k in ("proc", "cgroup_mount")}),
        "systemd_delegate_subgroup": systemd_version(**{k: v for k, v in overrides.items()
                                                         if k == "systemctl_candidates"}),
        "sandbox": sandbox_selftest(**{k: v for k, v in overrides.items() if k in ("launcher", "python")}),
    }
    return {"ok": all(item["ok"] for item in facilities.values()), "facilities": facilities}


def _account_can_write(info: os.stat_result) -> bool:
    """Write+search permission for THIS account from ownership and mode bits.
    Deliberately not os.access(): the protected updater runs this preflight
    inside its own ProtectSystem=strict mount namespace, where /srv is
    read-only for the updater even though it is writable for the services
    (Gunicorn creates the root on first use) -- os.access would refuse a good
    station. ACLs are not consulted (a conservative refusal at worst)."""
    if os.geteuid() == 0:
        return True
    if info.st_uid == os.geteuid():
        return info.st_mode & 0o300 == 0o300
    if info.st_gid == os.getegid() or info.st_gid in os.getgroups():
        return info.st_mode & 0o030 == 0o030
    return info.st_mode & 0o003 == 0o003


def check_media_root(*, raw=None) -> dict:
    """PRODUCTION_MEDIA_ROOT is safe (production.root_policy) and this account
    either already has it as a real dedicated directory or can create it."""
    from django.conf import settings

    from production import root_policy
    from production.services import layout

    value = raw if raw is not None else getattr(settings, "PRODUCTION_MEDIA_ROOT", "")
    try:
        accepted = Path(root_policy.check_root(value, protected=layout._protected_paths(), dedicated_path=value))
    except root_policy.RootPolicyError as exc:
        return {"ok": False, "root": str(value)[:200], "detail": f"{exc.code}: {exc.message}"[:300]}
    if os.path.lexists(accepted):
        info = os.lstat(accepted)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return {"ok": False, "root": str(accepted), "detail": "exists but is not a real directory"}
        if info.st_uid != os.geteuid() or not _account_can_write(info):
            return {"ok": False, "root": str(accepted), "detail": "exists but is not owned and writable by this account"}
        return {"ok": True, "root": str(accepted), "detail": "exists; usable"}
    ancestor = accepted.parent
    while not os.path.lexists(ancestor):
        ancestor = ancestor.parent
    info = os.lstat(ancestor)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or not _account_can_write(info):
        return {"ok": False, "root": str(accepted),
                "detail": f"absent, and {ancestor} is not writable by this account to create it"}
    return {"ok": True, "root": str(accepted), "detail": f"absent; created on first use under {ancestor}"}
