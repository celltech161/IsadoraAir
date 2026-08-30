"""Static contract coverage for the two Road Conditions/KanDrive deploy
units -- deliberately NOT installed, enabled, or started by anything
here (see this project's own side-effect-boundary conventions); this
only reads the checked-in template files, matching the established
precedent in webrequests/tests/test_ingest_integration.py's
test_execution_is_bounded_in_http_and_systemd()."""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SERVICE_PATH = Path(settings.BASE_DIR) / "deploy/isadoraair-generate-road-condition-audio.service"
TIMER_PATH = Path(settings.BASE_DIR) / "deploy/isadoraair-generate-road-condition-audio.timer"


class GenerationServiceContractTests(SimpleTestCase):
    def setUp(self):
        self.content = SERVICE_PATH.read_text()

    def test_invokes_exactly_generate_road_condition_audio_with_no_flags(self):
        exec_lines = [
            line.strip() for line in self.content.splitlines()
            if line.strip().startswith("ExecStart=")
        ]
        self.assertEqual(len(exec_lines), 1)
        self.assertTrue(
            exec_lines[0].endswith("manage.py generate_road_condition_audio"),
            f"ExecStart must invoke the plain command with no flags, got: {exec_lines[0]!r}",
        )

    def test_does_not_contain_force_flag(self):
        self.assertNotIn("--force", self.content)

    def test_does_not_contain_regenerate_flag(self):
        self.assertNotIn("--regenerate", self.content)

    def test_does_not_contain_voice_override_flag(self):
        self.assertNotIn("--voice", self.content)

    def test_uses_isa_root_render_token(self):
        self.assertIn("@@ISA_ROOT@@", self.content)

    def test_uses_isa_user_render_token(self):
        self.assertIn("@@ISA_USER@@", self.content)

    def test_is_a_oneshot_service(self):
        self.assertIn("Type=oneshot", self.content)

    def test_has_deliberate_unbounded_start_timeout_not_a_short_default(self):
        # Not systemd's ordinary 90s default, and not an arbitrary short
        # value like 90s/120s -- the application's own internal bounds
        # (per-call Kokoro/shared-TTS timeouts, ffmpeg/ffprobe timeouts,
        # the advisory lock) govern instead. See this unit's own comment
        # for the full reasoning and the 165.2s real-production data point.
        self.assertIn("TimeoutStartSec=0", self.content)

    def test_uses_environment_file(self):
        self.assertIn("EnvironmentFile=@@ISA_ROOT@@/.env", self.content)

    def test_reads_config_from_isa_root_working_directory(self):
        self.assertIn("WorkingDirectory=@@ISA_ROOT@@", self.content)


class GenerationTimerContractTests(SimpleTestCase):
    def setUp(self):
        self.content = TIMER_PATH.read_text()

    def test_installed_via_timers_target(self):
        self.assertIn("WantedBy=timers.target", self.content)

    def test_five_minute_recurrence(self):
        self.assertIn("OnUnitActiveSec=5min", self.content)

    def test_sensible_boot_delay(self):
        self.assertIn("OnBootSec=3min", self.content)

    def test_not_persistent(self):
        # Persistent=true would immediately fire a catch-up run on boot
        # for every boot the timer missed while the host was down --
        # never desired for an ordinary generation check.
        self.assertIn("Persistent=false", self.content)

    def test_no_systemd_dependency_on_the_sync_unit(self):
        # The two timers are deliberately decoupled -- separate
        # advisory locks, separate schedules (see this file's own
        # explanatory comment, which legitimately NAMES the sync timer
        # for documentation). What must NOT exist is an actual systemd
        # dependency/ordering directive wiring this timer's firing to
        # the sync unit.
        dependency_lines = [
            line.strip() for line in self.content.splitlines()
            if line.strip().split("=")[0] in {"After", "Requires", "BindsTo", "PartOf", "Wants"}
        ]
        for line in dependency_lines:
            self.assertNotIn("sync-road-conditions", line)


class BothUnitsRenderCleanlyTests(SimpleTestCase):
    """Confirms the same @@TOKEN@@ convention every other rendered unit
    in this repository uses -- no stray/unknown tokens, no literal
    station-specific paths baked in (matching deploy/README.md's own
    six-variable render contract)."""

    def test_service_contains_no_unrendered_unknown_tokens(self):
        content = SERVICE_PATH.read_text()
        # Every @@...@@ token actually present must be one of the two
        # this unit legitimately uses.
        import re
        tokens = set(re.findall(r"@@[A-Z_]+@@", content))
        self.assertEqual(tokens, {"@@ISA_ROOT@@", "@@ISA_USER@@"})

    def test_timer_contains_no_tokens_at_all(self):
        # Timer units in this repo never render tokens -- only the
        # paired .service file does.
        import re
        content = TIMER_PATH.read_text()
        self.assertEqual(re.findall(r"@@[A-Z_]+@@", content), [])
