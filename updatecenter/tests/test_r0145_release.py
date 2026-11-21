"""r0145 immutable release contract (evergreen Voice Track transitions, Library CSV export, NWS fetch visibility).

Published r0144 (e4003aa) plus 25 linear engineering commits, each cherry-picked with -x from an independently accepted
candidate (equal patch IDs), then R:
  ca1fc40 .. d0a1af4 -- A: roadmap 2.25 (22 commits from 6ad5222 .. 213993e); the 22nd reproduces the accepted tree
                        09377e28 and the 21st the accepted C10 remediation tree 255d8add;
  62649c5            -- B: Library filtered CSV export (from 8f84f26);
  50cb6c8, 724eed0   -- C: NWS alert-fetch failure visibility (from 35e9830, 654b7ee);
  R                  -- metadata-only child of 724eed0 adding this contract, the r0145 manifest and two release
                        documents.
The three workstreams touch disjoint files; every changed file in the engineering tree is byte-identical to its accepted
source tip.

r0145 declares no migration and restarts Gunicorn, the engine and Monitoring: those are exactly the long-running
services that load (or, for Monitoring, run) the changed production code; the changed NWS poller is a scheduled one-shot
process. Every object named by these tests is reachable from R alone (asserted against an isolated repository that holds
nothing but R's history).
"""
from __future__ import annotations

import ast
import io
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401  (protected runtime on sys.path)
from .test_phase_b_systemd_daemon import FakeSystemRunner
from .test_r0137_release import LONG_RUNNING_ENTRIES, _ImportGraph
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.migration_authorization import validate_release_companion
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    TrustedRepository,
    derive_plan,
    load_chain,
    manual_blockers,
    parse_manifest,
    resolve_known_managed_units,
)
from isadoraair_updater.systemd import SystemdManager
from library.services import engine_restart_safety
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import cross_check, manifest as manifest_mod, migration_authorization, planner, release_chain


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0124_RELEASE = "69d53bc4f40e96a47359dce817375aa415cd068f"
R0130_RELEASE = "58e67cb247bd75ed489213dfc7e0bd6800596d3a"
R0132_RELEASE = "653c5bfd09972858fc529d00a28deee7890607a3"
R0136_RELEASE = "7c1df8f8d30c396fd01cfb6ded7d2240ecfa8fa3"
R0138_RELEASE = "167a806ef97dd224d6f1182d2492ba3d908a5da5"
R0139_RELEASE = "52e764ed0a8dab63a308214b0e85bc3918c8bf07"
R0140_RELEASE = "64e8620ee5e90ffcd31bfca00369bd5cfb80fe3b"
R0141_RELEASE = "529b84b79e154a0d370c9a87c78484716d4e6518"
R0142_RELEASE = "6b8d4b844de385e4f149b90cf30079d790d19fc1"
R0143_RELEASE = "538ed7e1c4a9a178e5d8186dc40ff27510dc0ce5"
R0144_RELEASE = "e4003aa7f075b940ec5680bf6864f08530ceed05"
R0144_TREE = "ca941ed15e937cbf0508f2b03aa6545fb39f8c01"
#: (commit, tree, accepted source commit it was cherry-picked from) in first-parent order on r0144.
ENGINEERING_CHAIN = (
    ("ca1fc401cdf64c9d26d9d1008885f29936ffa0e9", "161abf42a78d83aa98a859d77b86ba3f2a6b7ac0",
     "6ad5222f283747ff30d42d23d6aaf2e3623de3e0"),
    ("c9eb12f1fb523f92db62d769d00fd1465d40b839", "9957dfad19c5ef2c1f35c3fb90ba43c7366d51e6",
     "ced54ed747ed4431c5bdc2aecb169faac1a2d4d0"),
    ("de146b5a4874f48cd2749c8a78d4f71a48772cb6", "acf588c8767672f80dad0348ee6ae68732147f4b",
     "6f66222e3098dbd0dba363a9b2c7fb851acfa7f3"),
    ("3ee8cd558daae05682b908590624e15ca25a1df3", "8f646e56e538bdde12121a032d564abf2bd80765",
     "f55febebfe9474719d6aacc660ba57dd72348b81"),
    ("c9d4600270b3f552f81f651c1f3ac59ada0f28eb", "c7f9273f53de39b2809ab045fdecb8e14a0c9e3a",
     "2e5da2f04f104c4b11a7e099f038a5817aa07a75"),
    ("32449e98c9266d6b32cdf878ecb96ae4acdbed48", "e2d89ff87162637a237450285e9245f1a269ac2f",
     "d279cb8af1e3fbda1204e18c6a83d778a5740d5f"),
    ("3bf3a0a399e6ec7305ff0580b584c88863700193", "e43a175ac3275c3d2acab5c0183e0a9f5db502bc",
     "2dc58eb6e0fcae272ec80fc600f713bc3101a481"),
    ("b40c30311f7de685bc8b756d20ef51f97aa6c608", "bfa3f2d623c3ac41d594c02b33bc106e5461889d",
     "c380671b0caa33cab351347ae91f7a10153e8b4e"),
    ("a27c4e954e464156dc0419b8a9065bf75752f446", "c3cbb5f049a8129f540996c0a5a0e2d42a57d11f",
     "f2f7f332e3fa53eabe344bbeefca2a4281576852"),
    ("653eb626ce353ffe0781368e5bf72724150aabdd", "ae0c8c06889aaf2c3bd149a2edae1a43c5bec467",
     "57f9956725c4954a3169795f27ccc1a16d229c32"),
    ("e7016473920caacb102b307cc43956bbfecd2770", "19bb9753f4a0cfa817dc5d33e111b021cc90db19",
     "41454f691b91118514b096845e0db08f65b76bba"),
    ("c5454a6fcbeb87dd976963038696232e995e3286", "884c794dd2e35a25dfc2ab301090bf0a49c22dc5",
     "ff762e1697cc17145e3bc18ae9e9d71f5e9d4c69"),
    ("bfdd298ede35a75d6433329caa5993c48f354fd7", "0b24862b82dc9742205c307c8b3be0e923d8e037",
     "00ac626198106dd533e74392ba950aca1e3cd95c"),
    ("5ed6978d355fa5f242cbba652c64c83eca8a8cd5", "cb8339caa5ad603e96d51f990151954459f71646",
     "ce96c4ec8ae60c3b8f27b8905eebb6cdcfadd4bd"),
    ("0cce217491620892e4d1cc9d372078e90a413b7b", "4a51901d4a371bbf5bcba0557df0ffad1ec494e2",
     "abe4e983d68486b0541961b71d7940a866364366"),
    ("9b6957fb80e4f749613379d930ad508b189d757e", "5375b84117ff8416ca1d1419a86d1f3505e599bc",
     "05dbcc724f04c0369dcfc7a7b6932b97c68c99bd"),
    ("7acf0b12f4a138430ae7499162f197f74a5e9e45", "c66f54df5da70dec29234b2f0680a8cf88b14805",
     "aa931013db4202ff59d9c51e1007c99ba25dbd8e"),
    ("c9def2b137d504fd5a76baa7857b5323a962f137", "5c1ae60d31e835bd0188ef9203b011c0411cac5a",
     "f83a2973c44b22faebc58f69598e9d20a69f0307"),
    ("45e2b4e555bd529affc88be4217651c09267fb63", "0a89a222ae367e3339505b27aba0e30dc7422a99",
     "788f28596dc8c572c972d100e79aaa898afdd2e5"),
    ("b616baf21d7d94a786fbfd92ff999d0a16f604f7", "89f3d115104faa45c596cfafa98965ce21b062b0",
     "345e5b5c50b1610710083bc31fe91472ed153683"),
    ("dfe14f102fbb70eb8a7906514f66461c9bec4668", "255d8add168413d8974c223cd3db1cf2e554bbd9",
     "d45b328a7e274f6854b1283f2032a345f5441108"),
    ("d0a1af4a5cc1c0e179ccbb1f74c5a75899d15016", "09377e28748f909670405b81383bce3056cd873d",
     "213993eb968aa5fd001f547e49c86ff1ecd6c9e4"),
    ("62649c5b3b507aca1b4ce0689f3c914c54b6f09d", "5c1dde46c2fe0649d943aee7977605d8b80b757b",
     "8f84f26a16135da2a3c2dffa2b1b1f2a392ccf43"),
    ("50cb6c8b4abc526a1e0dbd51c375d6eeff618494", "f6bef26152b129f42dca34b3a74b1ed7cbbf396f",
     "35e98305054aac4402fba776eead6e7b8a3098be"),
    ("724eed0882e58ed38aad40e8a76923e094673407", "dd09ecf17ea149cd6cb7fb86c105b7ed53dd31ce",
     "654b7ee9d2fac175167ef00b9ddb11ba8bc19fd0"),
)
#: Accepted source tips (commit, tree) and the integrated commit that completes each workstream.
ACCEPTED_VT = ("213993eb968aa5fd001f547e49c86ff1ecd6c9e4", "09377e28748f909670405b81383bce3056cd873d")
ACCEPTED_C10 = ("d45b328a7e274f6854b1283f2032a345f5441108", "255d8add168413d8974c223cd3db1cf2e554bbd9")
ACCEPTED_CSV = ("8f84f26a16135da2a3c2dffa2b1b1f2a392ccf43", "ba5cdad1b3e1110a2c5632268effdae6be6ff9d7")
ACCEPTED_NWS_IMPLEMENTATION = ("35e98305054aac4402fba776eead6e7b8a3098be", "48f0b54641579b394290f1542f7114d6e8bfe3ae")
ACCEPTED_NWS = ("654b7ee9d2fac175167ef00b9ddb11ba8bc19fd0", "c1a8403ff0a82c3f8c7f7670a368d69ad6a20621")
ENGINEERING = ENGINEERING_CHAIN[-1][0]
ENGINEERING_TREE = ENGINEERING_CHAIN[-1][1]
ADDED_FILES = {
    "docs/LIBRARY_CSV_EXPORT.md",
    "docs/roadmap/2_25_PHASE_A_VT_TRANSITION_CONTRACT.md",
    "docs/roadmap/2_25_PHASE_B0_TIMING_FEASIBILITY.md",
    "docs/roadmap/2_25_PHASE_B1_B2_INFRASTRUCTURE.md",
    "docs/roadmap/2_25_PHASE_B3_B4_INTEGRATION.md",
    "docs/roadmap/2_25_PHASE_B5_B6_OPERATOR_AND_RETIREMENT.md",
    "docs/roadmap/2_25_PHASE_B7_QUALIFICATION.md",
    "library/services/track_csv_export.py",
    "library/services/vt_audio.py",
    "library/services/vt_lookup.py",
    "library/services/vt_transition.py",
    "library/tests/_vt_b0_lab.py",
    "library/tests/_vt_b7_webrtc_churn.py",
    "library/tests/_vt_b7s_remote_lab.py",
    "library/tests/_vt_integration_lab.py",
    "library/tests/measure_vt_b7_readmission.py",
    "library/tests/measure_vt_b7s_admission_boundary.py",
    "library/tests/memsoak_vt_b7s.py",
    "library/tests/render_library_csv_export_artifact.py",
    "library/tests/soak_vt_b7.py",
    "library/tests/soak_vt_b7s_livefill.py",
    "library/tests/test_library_csv_export.py",
    "library/tests/test_vt_b0_timing_feasibility.py",
    "library/tests/test_vt_b2_infrastructure.py",
    "library/tests/test_vt_b3_outro_integration.py",
    "library/tests/test_vt_b4_intro_integration.py",
    "library/tests/test_vt_b5_operator_integration.py",
    "library/tests/test_vt_b6_retirement_state_lookup.py",
    "library/tests/test_vt_b7_qualification.py",
    "library/tests/test_vt_b7s_lifecycle.py",
    "library/tests/test_vt_b7s_remote_dj_media.py",
    "library/tests/test_vt_b7s_supplemental.py",
    "library/tests/test_vt_c10_teardown_ownership.py",
    "library/tests/test_vt_transition_2_25_phase_a.py",
    "library/tests/test_vt_transition_planner.py",
    "weather/tests/test_nws_fetch_visibility.py",
    "weather_ingest/tests/test_nws_alert_fetch_status.py",
}
MODIFIED_FILES = {
    "active_messages/tests/d2b_support.py",
    "docs/WEATHER_ALERT_COMBINED.md",
    "library/services/audio_recovery.py",
    "library/services/engine.py",
    "library/services/hour_refill_causality.py",
    "library/services/hour_refill_projection.py",
    "library/templates/library/library.html",
    "library/tests/_transport_harness.py",
    "library/tests/test_engine_deck_lifecycle.py",
    "library/tests/test_fx_fire_completion.py",
    "library/tests/test_fx_fires_state.py",
    "library/tests/test_hour_refill_automatic_failure_2_27.py",
    "library/tests/test_hour_refill_eject_worker_2_27.py",
    "library/tests/test_hour_refill_proposal_2_27.py",
    "library/tests/test_hour_refill_runner_2_27.py",
    "library/tests/test_hour_refill_start_next_2_27.py",
    "library/tests/test_start_next_engine_2_23.py",
    "library/tests/test_transport_safety_2_23.py",
    "library/tests/test_voicetrack_production_media.py",
    "library/urls.py",
    "library/views.py",
    "monitoring/services/probes.py",
    "weather/diagnostics.py",
    "weather_ingest/update_local_wx_data.py",
    "webrequests/tests/test_dedication_intros.py",
}
CHANGE_FILES = ADDED_FILES | MODIFIED_FILES
#: Production (non-test) Python modules loaded by long-running services.
PRODUCTION_MODULES = {
    "library.services.audio_recovery", "library.services.engine", "library.services.hour_refill_causality",
    "library.services.hour_refill_projection", "library.services.track_csv_export", "library.services.vt_audio",
    "library.services.vt_lookup", "library.services.vt_transition", "library.urls", "library.views",
    "monitoring.services.probes", "weather.diagnostics",
}
#: Changed scripts run only as scheduled one-shot processes (they load the new source on their next invocation).
ONE_SHOT_MODULES = {"weather_ingest.update_local_wx_data"}
#: The engine loads exactly these changed modules.
ENGINE_MODULES = {
    "library.services.audio_recovery", "library.services.engine", "library.services.hour_refill_causality",
    "library.services.hour_refill_projection", "library.services.vt_audio", "library.services.vt_lookup",
    "library.services.vt_transition",
}
#: Changed template -> the only (Gunicorn) view module that renders it.
TEMPLATES = {"library/templates/library/library.html": ("library/library.html", "library/views.py")}
MANIFEST_PATH = "deploy/releases/r0145.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0145.md",
    "docs/releases/r0145_installation.md",
    "updatecenter/tests/test_r0145_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = RESTARTS + ("isadoraair-active-message-worker",)
#: installed release -> (installed commit, releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0144": (R0144_RELEASE, ("r0145",), (), None, RESTARTS),
    "r0143": (R0143_RELEASE, ("r0144", "r0145"), (), None, RESTARTS),
    "r0142": (R0142_RELEASE, ("r0143", "r0144", "r0145"), (), None, RESTARTS),
    "r0141": (R0141_RELEASE, tuple(f"r{n:04d}" for n in range(142, 146)), (), None, RESTARTS),
    "r0140": (R0140_RELEASE, tuple(f"r{n:04d}" for n in range(141, 146)), (), None, RESTARTS),
    "r0139": (R0139_RELEASE, tuple(f"r{n:04d}" for n in range(140, 146)), (R0140_MIGRATION,), "additive", RESTARTS),
    "r0138": (R0138_RELEASE, tuple(f"r{n:04d}" for n in range(139, 146)), (R0139_MIGRATION, R0140_MIGRATION),
              "additive", RESTARTS),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, 146)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", RESTARTS),
    "r0132": (R0132_RELEASE, tuple(f"r{n:04d}" for n in range(133, 146)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", RESTARTS),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, 146)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", RESTARTS),
    "r0124": (R0124_RELEASE, tuple(f"r{n:04d}" for n in range(125, 146)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_FROM_R0124_RESTARTS),
}
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
    "deploy/migration_authorizations",
)


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True, capture_output=True,
    ).stdout


def _patch_id(commit: str) -> str:
    patch = subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", commit], check=True, capture_output=True).stdout
    out = subprocess.run(["git", "-C", str(PROJECT_ROOT), "patch-id", "--stable"], input=patch, check=True,
                         capture_output=True).stdout.decode().split()
    return out[0]


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    options, paths = list(filters), []
    if "--" in options:
        cut = options.index("--")
        options, paths = options[:cut], options[cut:]
    return set(_git("diff", "--name-only", "--no-renames", *options, base, commit, *paths).split())


def _blob(commit: str, path: str) -> str | None:
    result = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "-q", "--verify", f"{commit}:{path}"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _introducing(path: str = MANIFEST_PATH) -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--", path,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing()
    if len(commits) > 1:
        raise AssertionError(f"r0145 manifest introduced more than once: {commits!r}")
    return commits[0] if commits else None


def _require_release(test) -> str:
    release = _release_commit()
    if release is None:
        test.skipTest("R has not been committed yet")
    return release


def _artifact(path: str) -> bytes:
    release = _release_commit()
    return _show(release, path) if release else (PROJECT_ROOT / path).read_bytes()


def _repository(git_dir: Path | None = None) -> TrustedRepository:
    git_dir = git_dir or Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    return TrustedRepository(git_dir, "unused", "main", CommandRunner())


def _policy():
    return parse_policy_dict(json.loads(_show(R0144_RELEASE, POLICY_PATH)), label="r0144 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _isolated_repository(root: Path, release: str) -> Path:
    """A bare repository holding only R's history (no alternates, no other refs): what a fresh clone of R gets."""
    bare = root / "isolated.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git("fetch", "-q", "--no-tags", str(PROJECT_ROOT), f"{release}:refs/heads/main", cwd=bare)
    return bare


def _py_files(commit: str, prefix: str) -> list[str]:
    return [path for path in _git("ls-tree", "-r", "--name-only", commit, prefix).split()
            if path.endswith(".py") and "/tests/" not in path]


class R0145ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_engine_monitoring_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0145.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0145", "r0144", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0144 = json.loads(_show(R0144_RELEASE, "deploy/releases/r0144.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0144["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0144["schema_version"])
        for field in (
            "apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
            "systemd_units_new_optional", "systemd_units_removed_or_renamed", "migrations_required",
        ):
            self.assertEqual(data[field], [], field)
        self.assertEqual(
            (data["manual_bootstrap_required"], data["python_requirements_changed"], data["requirements_sha256"],
             data["nginx_changed"], data["runtime_components_changed"], data["migration_compatibility"],
             data["collectstatic_required"], data["protected_runtime"]),
            (False, False, None, False, False, None, False, None),
        )
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))
        self.assertEqual(list(data), list(r0144))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Evergreen Voice Tracks", "two-sided intro/outro transitions", "actual-audio admission",
                         "Remote DJ and urgent-alert precedence", "failure quarantine and VT suspension",
                         "Export to .csv", "why an NWS watch/warning snapshot is stale", "No migration",
                         "protected-runtime change", "restart Gunicorn, then the engine, then Monitoring"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0145")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0145", "r0144"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_the_r0144_engine_restart_safeguard_gates_this_installation(self):
        """The declared plan restarts the engine, so the r0144 Update Center assesses engine restart safety first."""
        data = json.loads(_artifact(MANIFEST_PATH))
        self.assertTrue(engine_restart_safety.plan_restarts_engine(
            SimpleNamespace(services_requiring_restart=data["services_requiring_restart"])))

    def test_release_notes_document_changes_reviews_and_limitations(self):
        notes = " ".join(_artifact("docs/releases/r0145.md").decode().split())
        for required in (
            ACCEPTED_VT[1], "Codex ACCEPTED (C10 remediation verified)", "3,030 of 3,030",
            "Codex ACCEPTED (after the `654b7ee` remediation)", "every patch is identical to its accepted source",
            "byte-identical to its accepted source tip", "**outro VT**", "**intro VT**", "**150 ms**",
            "Start Next, manual deck starts and a song started by an operator Eject bypass intro VTs",
            "evergreen VTs never attach to them", "deck-mixer evidence", "never on the audio thread",
            "**quarantined**", "**suspended**", "`program_playout_interrupted=False`", "An engine restart clears the "
            "suspension", "adds `vt_transition`", "`Artist,Title,Album,Category,Duration`", "UTF-8 **with a byte-order mark**",
            "one leading apostrophe", "**no Library record, audio file, embedded tag or Bulk Edit behavior changes.**",
            "`WEATHER_DATA_DIR/nws_alert_fetch_status.json`", "`upstream`", "`local`", "`degraded_details`", "**Recovery:**",
            "It never inserts, replays or cancels an alert", "The AMBER poll is unchanged",
            "**A teardown failure suspends Voice Tracks until the next engine restart.**",
            "deck-mixer idle freeze (pre-existing in r0144", "roadmap 3.11) is **not** part of r0145",
            "Hour Refill arming is unchanged", "`migrations_required` is empty", "`migration_compatibility` is `null`",
            "`protected_runtime` is `null`", "1. `isadoraair-gunicorn` 2. `isadoraair-engine` 3. "
            "`isadoraair-monitoring`", "**interrupts program audio**", "**engine restart safeguard**",
            "Do not override it to install", "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("TESTING_PLACEHOLDER", "fully passing", "Restart Gunicorn only",
                          "embedded metadata synchronization is included"):
            self.assertNotIn(forbidden, notes)

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = " ".join(_artifact("docs/releases/r0145_installation.md").decode().split())
        for required in (
            R0144_RELEASE, ENGINEERING, ENGINEERING_TREE, ACCEPTED_VT[1],
            "Publication does not authorize deployment", "separate station-specific", "There is **no migration**",
            "Nothing else.", "no encoder, RBDS or worker restart", "stop and investigate",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`", "Do not manually run `migrate`",
            "**approved operator maintenance window**", "**Do not override the safeguard to install.**",
            "predates the safeguard and will not check", "**Do not assume the station is on r0144.**",
            "WRJE was last independently documented on r0132", "complete chain", "action fingerprint",
            "Do **not** key a microphone on air", "`vt_transition`", "**Ordinary playout.**", "**Voice Track audio",
            "Do not add VTs or alter the log", "`engine|vt2-teardown|…`", "**NWS Weather Health.**",
            "Do not block the network or edit the file", "**Library CSV export.**", "**Export to .csv**",
            "## Post-install Monitoring observation", "## KOGR acceptance", "## WRJE acceptance",
            "Do not reuse KOGR's results",
            "**A successful installation at one station is not authorization to install at the other.**",
            "Record KOGR and WRJE evidence independently", "Stop and preserve", "Do not use manual Git resets",
            "not rollback authority",
        ):
            self.assertIn(required, runbook)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = " ".join(_artifact("docs/releases/r0145.md").decode().split())
        runbook = " ".join(_artifact("docs/releases/r0145_installation.md").decode().split())
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0145 release commit R. 2. Restart "
                      "`isadoraair-gunicorn`. 3. Restart `isadoraair-engine`. 4. Restart `isadoraair-monitoring`. "
                      "No migration", runbook)
        self.assertIn("A direct r0144 → r0145 plan advances source to R, then restarts `isadoraair-gunicorn`, "
                      "`isadoraair-engine` and `isadoraair-monitoring`, in that order. Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


class R0145HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_is_the_exact_linear_chain_on_r0144(self):
        self.assertEqual(_git("rev-parse", f"{R0144_RELEASE}^{{tree}}"), R0144_TREE)
        commits = [commit for commit, _tree, _origin in ENGINEERING_CHAIN]
        self.assertEqual(len(commits), 25)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0144_RELEASE}..{ENGINEERING}").split(), commits)
        parent = R0144_RELEASE
        for commit, tree, _origin in ENGINEERING_CHAIN:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            parent = commit
        self.assertEqual(_git("rev-list", "--merges", f"{R0144_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_introducing("deploy/releases/r0144.json"), [R0144_RELEASE])

    def test_every_commit_is_a_patch_identical_cherry_pick_of_its_accepted_source_in_order(self):
        origins = [origin for _commit, _tree, origin in ENGINEERING_CHAIN]
        self.assertEqual(origins[:22], _git("rev-list", "--reverse", f"{R0144_RELEASE}..{ACCEPTED_VT[0]}").split())
        self.assertEqual(origins[22:], [ACCEPTED_CSV[0], ACCEPTED_NWS_IMPLEMENTATION[0], ACCEPTED_NWS[0]])
        self.assertEqual(_git("rev-list", "--merges", f"{R0144_RELEASE}..{ACCEPTED_VT[0]}"), "")
        for commit, _tree, origin in ENGINEERING_CHAIN:
            with self.subTest(commit=commit):
                self.assertIn(f"(cherry picked from commit {origin})", _git("log", "-1", "--format=%B", commit))
                self.assertEqual(_patch_id(commit), _patch_id(origin))
                self.assertNotEqual(_changed(f"{commit}^", commit), set())

    def test_accepted_sources_and_trees_are_reproduced(self):
        for commit, tree in (ACCEPTED_VT, ACCEPTED_C10, ACCEPTED_CSV, ACCEPTED_NWS_IMPLEMENTATION, ACCEPTED_NWS):
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
        self.assertEqual(_git("rev-parse", f"{ACCEPTED_CSV[0]}^"), R0144_RELEASE)
        self.assertEqual(_git("rev-parse", f"{ACCEPTED_NWS_IMPLEMENTATION[0]}^"), R0144_RELEASE)
        self.assertEqual(_git("rev-parse", f"{ACCEPTED_NWS[0]}^"), ACCEPTED_NWS_IMPLEMENTATION[0])
        self.assertEqual(ENGINEERING_CHAIN[21][1], ACCEPTED_VT[1])
        self.assertEqual(ENGINEERING_CHAIN[20][1], ACCEPTED_C10[1])

    def test_workstreams_are_disjoint_and_every_changed_file_equals_its_accepted_source(self):
        sources = {name: _changed(R0144_RELEASE, tip) for name, tip in
                   (("vt", ACCEPTED_VT[0]), ("csv", ACCEPTED_CSV[0]), ("nws", ACCEPTED_NWS[0]))}
        self.assertEqual(sources["vt"] & sources["csv"], set())
        self.assertEqual(sources["vt"] & sources["nws"], set())
        self.assertEqual(sources["csv"] & sources["nws"], set())
        self.assertEqual(sources["vt"] | sources["csv"] | sources["nws"], CHANGE_FILES)
        self.assertEqual((len(sources["vt"]), len(sources["csv"]), len(sources["nws"])), (49, 7, 6))
        for name, tip in (("vt", ACCEPTED_VT[0]), ("csv", ACCEPTED_CSV[0]), ("nws", ACCEPTED_NWS[0])):
            for path in sorted(sources[name]):
                with self.subTest(path=path):
                    self.assertEqual(_blob(ENGINEERING, path), _blob(tip, path))

    def test_change_inventory_is_exact(self):
        self.assertEqual(_changed(R0144_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0144_RELEASE, ENGINEERING, "--diff-filter=A"), ADDED_FILES)
        self.assertEqual(_changed(R0144_RELEASE, ENGINEERING, "--diff-filter=M"), MODIFIED_FILES)
        self.assertEqual(_changed(R0144_RELEASE, ENGINEERING, "--diff-filter=DRTC"), set())
        self.assertIn("62 files changed, 19963 insertions(+), 763 deletions(-)",
                      _git("diff", "--shortstat", "--no-renames", R0144_RELEASE, ENGINEERING))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0144_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0144_RELEASE}..{release}").split(),
                         [commit for commit, _tree, _origin in ENGINEERING_CHAIN] + [release])
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0144_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0144_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0144_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0144_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0144_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0144_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0144_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        touched = _changed(R0144_RELEASE, release)
        self.assertEqual({path for path in touched if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        self.assertEqual(touched - RELEASE_FILES, CHANGE_FILES)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0145.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0144_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_production_change_is_exactly_these_modules_and_templates(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0144_RELEASE, release)
        modules = {path[:-3].replace("/", ".") for path in changed
                   if path.endswith(".py") and "/tests/" not in path}
        self.assertEqual(modules, PRODUCTION_MODULES | ONE_SHOT_MODULES)
        production_other = {path for path in changed if not path.endswith(".py")
                            and not path.startswith("docs/") and path != MANIFEST_PATH}
        self.assertEqual(production_other, set(TEMPLATES))

    def test_declared_restarts_are_exactly_the_long_running_services_that_execute_the_changed_code(self):
        """Gunicorn loads every changed production module, the engine loads the Voice Track, teardown and Hour Refill
        modules, and Monitoring runs the changed Weather probe. No other long-running service executes them even under
        an over-approximating import graph (lazy imports included); the changed NWS poller is a one-shot process."""
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        for entry in LONG_RUNNING_ENTRIES.values():
            self.assertIsNotNone(graph.path(entry), entry)
        for module in PRODUCTION_MODULES | ONE_SHOT_MODULES:
            self.assertIsNotNone(graph.path(module), module)
        reach = {service: graph.service_reach(service) for service in LONG_RUNNING_ENTRIES}
        for service, modules in reach.items():
            self.assertEqual(modules & ONE_SHOT_MODULES, set(), service)
        loading = {service for service, modules in reach.items() if modules & PRODUCTION_MODULES}
        # Over-approximation: the encoders appear only through a lazy import chain (next test).
        self.assertEqual(loading, set(RESTARTS) | {"isadoraair-encoders"})
        self.assertEqual(reach["isadoraair-gunicorn"] & PRODUCTION_MODULES, PRODUCTION_MODULES)
        self.assertEqual(reach["isadoraair-engine"] & PRODUCTION_MODULES, ENGINE_MODULES)
        self.assertEqual(reach["isadoraair-monitoring"] & PRODUCTION_MODULES,
                         {"monitoring.services.probes", "weather.diagnostics"})
        for template, (name, renderer) in TEMPLATES.items():
            with self.subTest(template=template):
                users = {path for path in graph.files
                         if "/tests/" not in path and name.encode() in _show(release, path)}
                self.assertEqual(users, {renderer})
                renderer_module = renderer[:-3].replace("/", ".")
                self.assertEqual({service for service, modules in reach.items() if renderer_module in modules},
                                 {"isadoraair-gunicorn"})
        units = _git("ls-tree", "--name-only", release, "deploy/").split()
        long_running = set()
        for unit in units:
            name = Path(unit).name
            if not (name.startswith("isadoraair-") and name.endswith(".service")):
                continue
            text = _show(release, unit).decode()
            if "Type=oneshot" in text or "manage.py" not in text and "gunicorn" not in text:
                continue
            long_running.add(name[: -len(".service")])
        self.assertEqual(long_running, set(LONG_RUNNING_ENTRIES))
        poller = _show(release, "deploy/wx-update-local-data.service").decode()
        self.assertIn("Type=oneshot", poller)
        self.assertIn("@@WEATHER_ROOT@@/update_local_wx_data.py", poller)

    def test_monitoring_runs_the_changed_weather_probe_and_the_encoders_never_do(self):
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        probes = "monitoring/services/probes.py"
        source = _show(release, probes).decode()
        self.assertIn('"weather": probe_weather', source)
        function = next(node for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.FunctionDef) and node.name == "probe_weather")
        self.assertEqual([node.module for node in ast.walk(function) if isinstance(node, ast.ImportFrom)],
                         ["weather.diagnostics"])
        probe_imports = []
        for path in _py_files(release, "encoders/"):
            text = _show(release, path).decode()
            self.assertNotIn("probe_weather", text, path)
            self.assertNotIn("weather.diagnostics", text.replace("monitoring/services/probes.py", ""), path)
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom) and node.module == "monitoring.services.probes":
                    probe_imports.append((path, node.module, tuple(alias.name for alias in node.names)))
                if isinstance(node, ast.ImportFrom) and node.module == "monitoring.services":
                    self.assertNotIn("probes", [alias.name for alias in node.names], path)
                if isinstance(node, ast.Import):
                    self.assertFalse([a.name for a in node.names if a.name.startswith("monitoring.services.probes")],
                                     path)
        self.assertEqual(probe_imports, [("encoders/services/encoder_manager.py", "monitoring.services.probes",
                                          ("evaluate_encoder_group_health",))])
        # Beyond probes itself (whose changed probe_weather they never call), the encoders reach no changed module.
        self.assertEqual(
            graph.service_reach("isadoraair-encoders", opaque={"monitoring.services.probes"}) & PRODUCTION_MODULES,
            {"monitoring.services.probes"})


class R0145MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0144_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0145.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0145", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0145"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0145ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0145_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0144_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 144)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0144_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0144_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0145_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 146)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0143"], commits["r0144"], commits["r0145"]),
                         (R0132_RELEASE, R0143_RELEASE, R0144_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0144 -> r0145", out.getvalue())


class R0145TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0145", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0145", release))
        self.assertEqual(plan.releases_in_plan, releases)
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), (migrations, compatibility))
        self.assertEqual(plan.services_requiring_restart, restarts)
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())
        self.assertTrue(engine_restart_safety.plan_restarts_engine(plan))

    def test_direct_plan_is_migration_free_restarts_three_services_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0144", R0144_RELEASE), ("r0145", release)])
        self._assert_plan("r0144")

    def test_catch_up_from_r0143(self):
        self._assert_plan("r0143")

    def test_catch_up_from_r0142(self):
        self._assert_plan("r0142")

    def test_catch_up_from_r0141(self):
        self._assert_plan("r0141")

    def test_catch_up_from_r0140(self):
        self._assert_plan("r0140")

    def test_catch_up_from_r0139_keeps_the_r0140_migration(self):
        self._assert_plan("r0139")

    def test_catch_up_from_r0138_keeps_the_r0139_and_r0140_migrations(self):
        self._assert_plan("r0138")

    def test_catch_up_from_r0136_keeps_three_migrations(self):
        self._assert_plan("r0136")

    def test_catch_up_from_r0132_wrje_last_documented_release(self):
        self._assert_plan("r0132")

    def test_catch_up_from_r0130_keeps_all_four_migrations(self):
        self._assert_plan("r0130")

    def test_catch_up_from_r0124_preserves_all_intervening_actions(self):
        self._assert_plan("r0124")

    def test_restart_rehearsal_touches_exactly_the_three_services_in_order(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[2] for call in runner.calls if call[1] == "restart"],
                         [f"{unit}.service" for unit in RESTARTS])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in ("isadoraair-encoders", "isadoraair-rbds", "isadoraair-active-message-worker",
                          "isadoraair-spoken-render-worker", "isadoraair-render", "isadoraair-validation"):
            self.assertNotIn(untouched, touched)


class R0145ReachabilityTests(SimpleTestCase):
    def test_every_named_object_is_reachable_from_r_alone(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            for commit, tree, _origin in ENGINEERING_CHAIN:
                self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}", cwd=bare), tree)
            self.assertEqual(_git("rev-parse", f"{R0144_RELEASE}^{{tree}}", cwd=bare), R0144_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            isolated = derive_plan(_repository(bare), release, R0144_RELEASE, "r0145", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0144_RELEASE, "r0145", known_units=_known_units())
            self.assertEqual(isolated.fingerprint, shared.fingerprint)
            self.assertEqual(isolated.services_requiring_restart, RESTARTS)


class _Clone:
    def __init__(self, root: Path, release: str):
        self.origin = root / "origin.git"
        self.work = root / "station"
        subprocess.run(["git", "clone", "--quiet", "--shared", "--bare", str(PROJECT_ROOT), str(self.origin)],
                       check=True, capture_output=True)
        _git("update-ref", "refs/heads/main", release, cwd=self.origin)
        subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(self.origin), str(self.work)],
                       check=True, capture_output=True)
        self.git("config", "user.email", "release-test@invalid")
        self.git("config", "user.name", "release test")

    def git(self, *args: str) -> str:
        return _git(*args, cwd=self.work)

    def at(self, commit: str) -> None:
        self.git("checkout", "-q", "-f", "-B", "main", commit)
        self.git("branch", "-q", "--set-upstream-to=origin/main", "main")
        self.git("clean", "-q", "-fdx")


class R0145PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0145",
                                    known_units=_known_units())
            self.assertEqual(application.safety_status, planner.SafetyStatus.READY_TO_PLAN,
                             application.safety_detail)
            self.assertEqual((application.releases_in_plan, application.target_commit), (releases, release))
            if migrations:
                self.assertEqual(application.migrations.explicitly_required, migrations)
                self.assertEqual(application.migrations.compatibility, compatibility)
            else:
                self.assertIsNone(application.migrations)
            self.assertEqual(application.services_requiring_restart, restarts)
            self.assertEqual(application.fingerprint, protected.fingerprint)

    def test_direct_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity("r0144")

    def test_catch_up_from_r0143_application_and_protected_planners_match(self):
        self._assert_parity("r0143")

    def test_catch_up_from_r0132_application_and_protected_planners_match(self):
        self._assert_parity("r0132")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
