"""Static contract coverage for the Road Conditions/KanDrive generation
deploy units -- deliberately NOT installed, enabled, or started by anything
here (see this project's own side-effect-boundary conventions); this
only reads the checked-in template files, matching the established
precedent in webrequests/tests/test_ingest_integration.py's
test_execution_is_bounded_in_http_and_systemd()."""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

DEFAULT_TRIGGER_PATH = "/run/isadoraair/road_generation.trigger"
SERVICE_PATH = Path(settings.BASE_DIR) / "deploy/isadoraair-generate-road-condition-audio.service"
TIMER_PATH = Path(settings.BASE_DIR) / "deploy/isadoraair-generate-road-condition-audio.timer"
PATH_UNIT_PATH = Path(settings.BASE_DIR) / "deploy/isadoraair-generate-road-condition-audio.path"
DEPLOY_README = Path(settings.BASE_DIR) / "deploy/README.md"
RESTORE_PATH = Path(settings.BASE_DIR) / "deploy/restore/90-system-config.sh"


def directives(content):
    return [line.strip() for line in content.splitlines()
            if line.strip() and not line.strip().startswith(("#", "["))]


class GenerationServiceContractTests(SimpleTestCase):
    def setUp(self):
        self.content = SERVICE_PATH.read_text()

    def test_invokes_only_the_dispatched_generator(self):
        exec_lines = [
            line.strip() for line in self.content.splitlines()
            if line.strip().startswith("ExecStart=")
        ]
        self.assertEqual(len(exec_lines), 1)
        self.assertTrue(
            exec_lines[0].endswith("manage.py generate_road_condition_audio --dispatched"),
            f"ExecStart must invoke the supervised dispatch command, got: {exec_lines[0]!r}",
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


class GenerationTriggerContractTests(SimpleTestCase):
    """1.24-B: the .path unit is the only ordinary automatic trigger."""

    def setUp(self):
        self.content = PATH_UNIT_PATH.read_text()

    def test_there_is_no_generation_timer_any_more(self):
        self.assertFalse(TIMER_PATH.exists())

    def test_watches_exactly_the_file_the_sync_command_replaces(self):
        from road_conditions import trigger
        self.assertEqual(
            [d for d in directives(self.content) if d.startswith("Path")],
            [f"PathChanged={DEFAULT_TRIGGER_PATH}"])
        self.assertEqual(str(trigger.TRIGGER_PATH), DEFAULT_TRIGGER_PATH)

    def test_starts_the_existing_generator_service(self):
        self.assertIn(f"Unit={SERVICE_PATH.name}", directives(self.content))
        self.assertIn("WantedBy=paths.target", directives(self.content))

    def test_is_edge_triggered_only(self):
        # PathExists= and friends are level-triggered: a failing generator
        # would be restarted forever.
        for forbidden in ("PathExists", "PathExistsGlob", "DirectoryNotEmpty", "PathModified"):
            self.assertFalse([d for d in directives(self.content) if d.startswith(forbidden + "=")])

    def test_service_cannot_hit_the_start_rate_limit(self):
        # Hitting it leaves the path unit failed: no more generation.
        service = SERVICE_PATH.read_text()
        unit_section = service.split("[Service]")[0]
        self.assertIn("StartLimitIntervalSec=0", directives(unit_section))

    def test_restore_installs_path_units(self):
        script = RESTORE_PATH.read_text()
        self.assertIn('"$REPO_ROOT"/deploy/*.path', script)
        self.assertIn("*.service|*.timer|*.path)", script)

    def test_fresh_install_creates_the_runtime_directory_before_enabling_the_watcher(self):
        guide = DEPLOY_README.read_text()
        self.assertIn("isadoraair-updater.service|isadoraair-tmpfiles.conf) continue", guide)
        create = guide.index("systemd-tmpfiles --create /etc/tmpfiles.d/isadoraair.conf")
        watcher = guide.index("enable --now isadoraair-generate-road-condition-audio.path")
        timer = guide.index("enable --now isadoraair-sync-road-conditions.timer")
        self.assertLess(create, watcher)
        self.assertLess(watcher, timer)

    def test_sync_unit_does_not_start_the_generator_itself(self):
        # OnSuccess= would also fire after a skipped ("not due") run.
        sync = (SERVICE_PATH.parent / "isadoraair-sync-road-conditions.service").read_text()
        for line in directives(sync):
            self.assertNotIn("generate-road-condition", line)
            self.assertFalse(line.startswith(("OnSuccess=", "ExecStartPost=")))


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

    def test_path_unit_contains_no_tokens_at_all(self):
        # Trigger units in this repo never render tokens -- only the
        # paired .service file does.
        import re
        content = PATH_UNIT_PATH.read_text()
        self.assertEqual(re.findall(r"@@[A-Z_]+@@", content), [])
