#!/usr/bin/env python3
"""Compatibility entry point for the AMBER/BLU/MEP urgent announcement.

This script used to synthesize an AMBER-only clip and publish it to
WxAlert/wx_alert.flac -- the same file wx_alert.py publishes for NWS
watches/warnings -- so the two producers overwrote each other's pending
announcement. Since r0137 there is ONE producer of that file: the combined
Weather/AMBER coordinator in wx_alert.py, which amber_poll.py runs after
every cycle. Running this script directly simply runs that coordinator
(one combined announcement of every alert currently in effect), so a stale
manual or legacy invocation can never publish an AMBER-only replacement.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wx_alert  # noqa: E402

CATEGORY_CODE = wx_alert.CATEGORY_CODE
DEST_FILENAME = wx_alert.DEST_FILENAME


def main():
    """Returns True on success (including benign no-ops), False on failure."""
    return wx_alert.main()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
