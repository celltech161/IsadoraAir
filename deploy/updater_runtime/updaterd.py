#!/usr/bin/python3
"""Installed entry point; production runs this file only from protected storage."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

# ``-I`` deliberately removes the script directory from sys.path.  Re-add only
# this entry point's own canonical directory; production verifies below that it
# is the exact root-owned installation directory, never an application path.
_ENTRY_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ENTRY_ROOT))


_PROTECTED_INSTALL_ROOT = Path("/usr/local/libexec/isadoraair-updater")


def _assert_protected_install():
    if _ENTRY_ROOT != _PROTECTED_INSTALL_ROOT:
        raise RuntimeError("updater daemon refuses to execute outside its protected installation path")
    current = Path("/")
    for part in _ENTRY_ROOT.parts[1:]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"protected updater parent path is unsafe: {current}")
    checked = [_ENTRY_ROOT, _ENTRY_ROOT / "updaterd.py", _ENTRY_ROOT / "isadoraair_updater"]
    checked.extend((_ENTRY_ROOT / "isadoraair_updater").glob("*.py"))
    for path in checked:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"protected updater installation ownership/mode is unsafe: {path.name}")
        if path.suffix == ".py" and not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"protected updater module is not a regular file: {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", default="/etc/isadoraair/station.json")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("the protected updater daemon must run as root")
    _assert_protected_install()
    # Import privileged implementation only after the installation path and
    # every package file have been proven root-owned and application-unwritable.
    from isadoraair_updater.config import load_config
    from isadoraair_updater.daemon import UpdaterDaemon

    config = load_config(Path(arguments.config), enforce_protection=True)
    daemon = UpdaterDaemon(config)
    try:
        daemon.serve_forever()
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
