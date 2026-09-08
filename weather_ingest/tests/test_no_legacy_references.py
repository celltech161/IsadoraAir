"""Part 9's static legacy-dependency audit, made permanent as a test:
zero executable references to the retired provider dictionary anywhere
in this project's own tracked source. wx_alert_beep.py/lib/ipaws.py/
lib/delivery.py/lib/notify.py are included for completeness even
though they were never TTS-related -- a clean audit should say so
explicitly, not just skip them."""
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The exact tracked-source set (`git ls-files '*.py'` at the time this
# test was written) -- deliberately a fixed list, not a live git
# subprocess call, so this test has no dependency on git being
# available/the working tree being a real checkout at test time.
TRACKED_PY_FILES = (
    "amber_alert.py", "amber_poll.py", "current_temp.py",
    "lib/delivery.py", "lib/ipaws.py", "lib/notify.py", "lib/voices.py",
    "lib/wxconfig.py", "update_local_wx_data.py", "wx_alert.py",
    "wx_alert_beep.py", "wx_forecast.py",
)

FORBIDDEN_MARKERS = (
    "/home/jreed/kokoro",
    "af_jessica",
    "am_liam",
    "kokoro_synth",
)

# "piper"/"Piper" checked separately (case-insensitive, since it's a
# common-enough substring to want a clear allow-list) -- these are the
# only files that legitimately still mention it, purely as historical/
# migration-note prose or an unchanged function name kept for caller
# compatibility, never as an executable path or CLI argument:
#   - lib/voices.py, requirements.txt, README.md: explain what was
#     retired and why, for operators/future readers.
#   - wx_alert.py, wx_forecast.py: unchanged ffmpeg lead-in-silence
#     rationale comments (real behavior preserved verbatim, comment
#     wording untouched -- out of this migration's narrow scope).
#   - current_temp.py, wx_forecast.py: generate_wav_with_piper() kept
#     as the historical function NAME for caller-signature stability
#     (Part 4's own explicit instruction) -- it no longer does
#     anything Piper-specific internally.
#   - update_local_wx_data.py: its own doc comments about historical
#     "Piper TTS audio" describing what wx_alert.py used to do --
#     unchanged, out of this migration's narrow scope.
FILES_WITH_ALLOWED_HISTORICAL_PIPER_MENTIONS = frozenset({
    "lib/voices.py", "wx_alert.py", "wx_forecast.py", "requirements.txt",
    "README.md", "current_temp.py", "update_local_wx_data.py",
})


class NoLegacyProviderReferencesTests(unittest.TestCase):
    def test_no_kokoro_path_af_jessica_am_liam_or_kokoro_synth_anywhere(self):
        for relative in TRACKED_PY_FILES:
            path = PROJECT_ROOT / relative
            content = path.read_text()
            for marker in FORBIDDEN_MARKERS:
                with self.subTest(file=relative, marker=marker):
                    self.assertNotIn(marker, content, f"{relative} unexpectedly contains {marker!r}")

    def test_no_piper_executable_or_model_path_anywhere(self):
        """Piper the WORD may still appear in historical/migration-note
        prose in a small allow-listed set of files; an actual
        EXECUTABLE PATH or CLI flag referencing it must never appear
        anywhere, including in those files."""
        piper_execution_markers = ("venv/bin/piper", "piper-tts", "PIPER_BINARY", "--model-path")
        for relative in TRACKED_PY_FILES:
            path = PROJECT_ROOT / relative
            content = path.read_text()
            for marker in piper_execution_markers:
                with self.subTest(file=relative, marker=marker):
                    self.assertNotIn(marker, content, f"{relative} unexpectedly contains {marker!r}")

    def test_lib_voices_contains_zero_provider_references_of_any_kind(self):
        """The central file -- the strictest possible check, no
        allow-list at all: not even a documentation mention of a
        provider-native id/path is expected here anymore."""
        content = (PROJECT_ROOT / "lib" / "voices.py").read_text()
        for marker in (*FORBIDDEN_MARKERS, "PIPER_BINARY", "KOKORO_BINARY"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, content)

    def test_requirements_txt_has_no_provider_package_dependency_line(self):
        """The COMMENT explaining piper-tts's retirement legitimately
        mentions the word (see FILES_WITH_ALLOWED_HISTORICAL_PIPER_
        MENTIONS above) -- what must never exist is an actual pinned
        dependency line for it."""
        for line in (PROJECT_ROOT / "requirements.txt").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                stripped.lower().startswith("piper"),
                f"requirements.txt has an active piper dependency line: {line!r}",
            )

    def test_only_allow_listed_files_mention_piper_at_all(self):
        for relative in TRACKED_PY_FILES:
            path = PROJECT_ROOT / relative
            content = path.read_text()
            if "piper" in content.lower():
                with self.subTest(file=relative):
                    self.assertIn(
                        relative, FILES_WITH_ALLOWED_HISTORICAL_PIPER_MENTIONS,
                        f"{relative} mentions 'piper' but is not on the allow-list -- classify it",
                    )


if __name__ == "__main__":
    unittest.main()
