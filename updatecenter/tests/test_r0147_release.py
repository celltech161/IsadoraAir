"""r0147 immutable release contract (first-audio stall recovery, roadmap 1.21-B1).

Published r0146 (bef4717) plus the nine accepted engineering commits of ``feature/1.21-b-first-audio-recovery``
(``28c98b3`` .. ``5dbbfa5``), then R:
  28c98b3 .. 5dbbfa5 -- the accepted engineering commits themselves, linear on r0146, NOT cherry-picked, squashed,
                        rebased or rewritten: library/services/engine.py plus three existing test modules;
  R                  -- metadata-only child of 5dbbfa5 adding this contract, the r0147 manifest and two release
                        documents.

Because the accepted commits are the published commits, every provenance claim is provable from the published
history alone: there are no unpublished source objects and nothing here skips in a public clone.

r0147 declares no migration and restarts the engine only. The one changed production module is
library.services.engine. Every changed or added member of it belongs to ``Deck`` or ``PlaybackEngine`` (plus two new
module constants), only the engine service's ``run_engine`` command constructs a ``PlaybackEngine`` and only
``engine.py`` constructs a ``Deck``, and no other production module references any changed or added name, so no other
long-running service executes the changed code (Gunicorn imports the module, as in every release, but never builds
or runs a PlaybackEngine). r0147 does not touch the deck mixer, deck bus or pipeline construction, so the r0146 Gate A
/ Gate B GStreamer preflight is not a r0147 installation gate for a station already on accepted r0146; the runbook
says so explicitly and keeps the r0146 gates mandatory for a station below r0146 (asserted below).
"""
from __future__ import annotations

import ast
import io
import json
import re
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
R0145_RELEASE = "d6679c80f162ba573a50b0ed4c30400b77ce7907"
R0146_RELEASE = "bef47174cd8cc7e0064102890b396fb0b93d83b0"
R0146_TREE = "4987527b59ec8e268a08e5a081e942f6ccd0ba93"
#: (commit, tree) in first-parent order on r0146: the independently accepted 1.21-B1 engineering commits.
ENGINEERING_CHAIN = (
    ("28c98b3ef85b451b990106278e0fe265a592a131", "9bb82cc619b07c706a1d706a995e1da84d861448"),
    ("15bdc42333819860930b5f9423e3bd2f14378e94", "96fe6e6e6d5112b63b429e825e4ef90040ea84a3"),
    ("ce9c7334e3135cc6a47cdb43374d530c84d0c085", "bcb844a40e91da3567fec113f910cb9d70c7e4d0"),
    ("d5a368ea79defd8d1a2c96a14f6978d3b68e9212", "6e14750cd558c8b2ff07614563d1ab07a09b92ca"),
    ("d12a2f730c495d1ecffa64fe26d9a91aa74e47a9", "e639a85adf336d9e3c19a0e81c3232a50b3efff4"),
    ("9d4f8e047ff2ee77c9bd95b76edb1780d73c6ac1", "2fe3214b00ca467a50442dd263830ad4f9f7150b"),
    ("96318b0cfa84898c9480036ceb2e66882c27855a", "a1246f5275e0a294b5c3d078eaa08d2f15588e04"),
    ("6bbaf9d36cd615a546a5cf8cfe3eaff00e611dca", "2d11ed528a3c6dc1554d32532c1c8900d11d69ec"),
    ("5dbbfa5ffc377bb4c615bffee60c6ee41deed71b", "dfae26d2be2bff365df6c83bcb19128dec2646ef"),
)
ENGINEERING = ENGINEERING_CHAIN[-1][0]
ENGINEERING_TREE = ENGINEERING_CHAIN[-1][1]
CHANGE_FILES = {
    "library/services/engine.py",
    "library/tests/test_engine_deck_lifecycle.py",
    "library/tests/test_engine_playback_accounting_semantics.py",
    "library/tests/test_engine_playback_boundary_evidence.py",
}
PRODUCT_FILES = {"library/services/engine.py"}
PRODUCTION_MODULES = {"library.services.engine"}
#: Names newly defined in the engine module (top-level constants and PlaybackEngine methods).
ADDED_ENGINE_NAMES = {
    ("", "FIRST_AUDIO_ELIGIBLE_ORIGINS"), ("", "ORDINARY_FIRST_AUDIO_GRACE_S"),
    *(("PlaybackEngine", name) for name in (
        "_first_audio_arm", "_first_audio_cursor_urgent", "_first_audio_deadline", "_first_audio_fresh_generation",
        "_first_audio_on_open", "_first_audio_owns", "_first_audio_queue_retry", "_first_audio_report_exhausted",
        "_first_audio_resolve_retry", "_first_audio_retry_discard", "_first_audio_retry_not_opened",
        "_first_audio_retry_refusal", "_first_audio_retry_take", "_first_audio_spend",
        "_first_audio_start_unattended", "_first_audio_unattended", "_release_claim_contention_attribution")),
}
#: Existing engine members whose body changed (nothing was removed).
CHANGED_ENGINE_MEMBERS = {
    ("Deck", "__init__"),
    *(("PlaybackEngine", name) for name in (
        "_deck_teardown_tick", "_defer_contended_occurrence", "_ensure_upcoming_logs", "_install_built_hour",
        "_install_live_fill_checked", "_next_queue_item", "_occurrence_handed_out", "_reload_and_restart_current_log",
        "_retry_contended_occurrence", "_schedule_occurrence_air_start_from_probe", "_start_next_track",
        "_try_load_next_hour", "start")),
}
#: Changed names too generic to search for by name; covered by the single-constructor assertions instead.
GENERIC_CHANGED_NAMES = {"__init__", "start"}
MANIFEST_PATH = "deploy/releases/r0147.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0147.md",
    "docs/releases/r0147_installation.md",
    "updatecenter/tests/test_r0147_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-engine",)
CATCH_UP_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = CATCH_UP_RESTARTS + ("isadoraair-active-message-worker",)
LAST = 147  # the release number of this contract
#: installed release -> (installed commit, releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0146": (R0146_RELEASE, ("r0147",), (), None, RESTARTS),
    "r0145": (R0145_RELEASE, ("r0146", "r0147"), (), None, RESTARTS),
    "r0144": (R0144_RELEASE, ("r0145", "r0146", "r0147"), (), None, CATCH_UP_RESTARTS),
    "r0143": (R0143_RELEASE, ("r0144", "r0145", "r0146", "r0147"), (), None, CATCH_UP_RESTARTS),
    "r0142": (R0142_RELEASE, tuple(f"r{n:04d}" for n in range(143, LAST + 1)), (), None, CATCH_UP_RESTARTS),
    "r0141": (R0141_RELEASE, tuple(f"r{n:04d}" for n in range(142, LAST + 1)), (), None, CATCH_UP_RESTARTS),
    "r0140": (R0140_RELEASE, tuple(f"r{n:04d}" for n in range(141, LAST + 1)), (), None, CATCH_UP_RESTARTS),
    "r0139": (R0139_RELEASE, tuple(f"r{n:04d}" for n in range(140, LAST + 1)), (R0140_MIGRATION,), "additive",
              CATCH_UP_RESTARTS),
    "r0138": (R0138_RELEASE, tuple(f"r{n:04d}" for n in range(139, LAST + 1)), (R0139_MIGRATION, R0140_MIGRATION),
              "additive", CATCH_UP_RESTARTS),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, LAST + 1)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_RESTARTS),
    "r0132": (R0132_RELEASE, tuple(f"r{n:04d}" for n in range(133, LAST + 1)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_RESTARTS),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, LAST + 1)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_RESTARTS),
    "r0124": (R0124_RELEASE, tuple(f"r{n:04d}" for n in range(125, LAST + 1)),
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
RUNBOOK = "docs/releases/r0147_installation.md"
NOTES = "docs/releases/r0147.md"


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True, capture_output=True,
    ).stdout


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    options, paths = list(filters), []
    if "--" in options:
        cut = options.index("--")
        options, paths = options[:cut], options[cut:]
    return set(_git("diff", "--name-only", "--no-renames", *options, base, commit, *paths).split())


def _introducing(path: str = MANIFEST_PATH) -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--", path,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing()
    if len(commits) > 1:
        raise AssertionError(f"r0147 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0145_RELEASE, POLICY_PATH)), label="r0145 protected policy")


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


def _text(path: str) -> str:
    return _artifact(path).decode()


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(markdown: str, heading: str) -> str:
    start = markdown.index(heading)
    following = re.search(r"^## ", markdown[start + len(heading):], re.M)
    return markdown[start: start + len(heading) + following.start()] if following else markdown[start:]


def _engine_names(source: str) -> set[tuple[str, str]]:
    names: set[tuple[str, str]] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            names.add((node.name, ""))
            names.update((node.name, member.name) for member in node.body
                         if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(("", node.name))
        elif isinstance(node, ast.Assign):
            names.update(("", target.id) for target in node.targets if isinstance(target, ast.Name))
    return names


def _engine_members(source: str) -> dict[tuple[str, str], str]:
    members: dict[tuple[str, str], str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members[(node.name, member.name)] = ast.dump(member)
    return members




class R0147ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_engine_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0147.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0147", "r0146", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0146 = json.loads(_show(R0146_RELEASE, "deploy/releases/r0146.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0146["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0146["schema_version"])
        self.assertEqual(data["minimum_updater_protocol_version"], r0146["minimum_updater_protocol_version"])
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
        self.assertEqual(data["services_requiring_restart"], ["isadoraair-engine"])
        self.assertEqual(list(data), list(r0146))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("First-audio stall recovery", "1.21-B1", "1.0 s", "same unaired occurrence once",
                         "one critical event and advances", "Not a general silence detector",
                         "operator starts are unchanged", "Restart the engine only", "no migration",
                         "protected-runtime change"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0147")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0147", "r0146"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_the_r0144_engine_restart_safeguard_gates_this_installation(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        self.assertTrue(engine_restart_safety.plan_restarts_engine(
            SimpleNamespace(services_requiring_restart=data["services_requiring_restart"])))

    def test_release_notes_document_the_change_its_boundaries_and_limitations(self):
        notes = _flat(_text(NOTES))
        for required in (
            "Independent Codex review: ACCEPT — ready for release qualification",
            "findings B1-R1, B1-R2 and B1-R3 resolved", "the accepted commits themselves; not cherry-picked, squashed or rewritten",
            "Only `library/services/engine.py` and three existing test modules changed",
            "**The inherited gap.**", "**The correction.**", "within **1.0 second**",
            "**reopens the same scheduled occurrence once**", "the original claim identity and timestamp are kept",
            "records **one critical engine event**", "**advances playout**",
            "behind forced items, urgent items and the Weather/AMBER alerts already pending",
            "is **not** retried", "exactly one of them wins", "**Playback accounting is preserved.**",
            "A Play event is still recorded only when real audio is first delivered",
            "**Retry identity lasts for the engine process.**", "for the lifetime of the engine process",
            "Only an engine restart resets it", "## What it is not",
            "**It is first-audio startup recovery, not a generalized silence detector.**",
            "r0147 does not eliminate dead air", "**Operator-controlled starts and other excluded playback modes keep their existing behaviour:**",
            "Start Next, Eject, Manual mode", "adds no station configuration",
            "47 inherited Active Messages `MediaRejected` fixture errors", "identical on unmodified r0146",
            "does **not** change the deck mixer, deck bus or pipeline construction",
            "does **not** have to be repeated for a station already running accepted r0146",
            "the r0146 preflight (`r0146_installation.md`) remains mandatory for it",
            "## Limitations (accepted; not remediated in r0147)", "**One retry only.**",
            "The Aircheck `airtap` Admin and session-attribution correction is a separate change and is not in r0147",
            "`migrations_required` is empty", "`migration_compatibility` is `null`", "`protected_runtime` is `null`",
            "1. `isadoraair-engine`", "**interrupts program audio**", "**engine restart safeguard**",
            "Do not override it to install", "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        limitations = _section(_text(NOTES), "## Limitations (accepted; not remediated in r0147)")
        items = [_flat(item) for item in re.split(r"^\s*\d+\. ", limitations, flags=re.M)[1:]]
        self.assertEqual(len(items), 9)
        self.assertRegex(items[0], r"A start that delivers some audio and then stalls")
        self.assertRegex(items[0], r"not detected or recovered")
        for forbidden in ("2. `isadoraair-", "Restart Gunicorn", "eliminates all dead air", "all dead air is",
                          "fully protected", "generalized silence detector is", "guarantees"):
            self.assertNotIn(forbidden, notes)
        # Nothing excluded is described as done in this release.
        self.assertNotRegex(notes, r"(?i)1\.21-[CDE][^.]*\b(is|are|was|were) (now )?(implemented|resolved|fixed|included)")

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = _flat(_text(RUNBOOK))
        for required in (
            R0146_RELEASE, R0146_TREE, ENGINEERING, ENGINEERING_TREE,
            "Publication does not authorize deployment", "separate station-specific", "There is **no migration**",
            "Nothing else.", "no Gunicorn, Monitoring, encoder, RBDS or worker restart", "stop and investigate",
            "a station on r0145 also restarts the engine for r0146 (still the engine only)",
            "a station on r0144 also restarts Gunicorn and Monitoring for r0145",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`", "Do not manually run `migrate`",
            "**approved operator maintenance window**", "**Do not override the safeguard to install.**",
            "predates the safeguard and will not check", "Manual mode, Start Next or Eject in progress",
            "return to Auto first", "not within minutes of a scheduled transition",
            "## Common read-only preflight", "ordinary unattended playout", "Engine and encoder health",
            "No scheduled transition is imminent, no urgent or Weather/AMBER alert is active",
            "Do **not** key a microphone on air", "**Do not inject a stalled deck, corrupt or missing media or "
            "deliberate silence into an operating station**", "exactly one Play event per item",
            "expected play-count increment", "The journal and Monitoring show no `FIRST-AUDIO DEADLINE` line",
            "If a recovery does occur naturally", "do not intervene", "additional operational evidence",
            "## Post-install Monitoring observation", "## KOGR acceptance", "## WRJE acceptance",
            "do not reuse KOGR's results",
            "**A successful installation at one station is not authorization to install at the other.**",
            "Stop and preserve", "Do not use manual Git resets", "not rollback authority",
        ):
            self.assertIn(required, runbook)

    def test_runbook_states_whether_the_r0146_gstreamer_preflight_still_applies(self):
        runbook = _text(RUNBOOK)
        heading = "## GStreamer preflight: applicability to r0147"
        self.assertIn(heading, runbook)
        section = _flat(_section(runbook, heading))
        for required in (
            "r0147 does not touch that code", "changes no audiomixer, capsfilter, pipeline construction or element property",
            "installs no package and does not change the system GStreamer runtime",
            "A station directly on accepted r0146 does not need to repeat Gate A and Gate B for r0147",
            "`Deck mixer: audiomixer force-live=True` is present", "`force-live=False`",
            "do not install r0147 until it is understood",
            "A station that is not on r0146", "Gate A and Gate B in `r0146_installation.md` remain **mandatory**",
            "KOGR's results never qualify WRJE", "GStreamer runtime was changed after its r0146 acceptance",
        ):
            self.assertIn(required, section)
        self.assertLess(runbook.index(heading), runbook.index("## Common read-only preflight"))
        self.assertNotRegex(section, r"(?i)\boptional(ly)?\b")
        # The claim is true: the engine diff touches no deck-mixer or pipeline-construction code.
        engine_diff = _git("diff", R0146_RELEASE, ENGINEERING, "--", "library/services/engine.py")
        for untouched in ("make_deck_mixer", "make_deck_bus_caps", "DECK_BUS_FORMAT", "_build_main_pipeline",
                          "force-live", "audiomixer", "capsfilter"):
            self.assertNotIn(untouched, engine_diff)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = _flat(_text(NOTES))
        runbook = _flat(_text(RUNBOOK))
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap, configuration or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0147 release commit R. 2. Restart "
                      "`isadoraair-engine`. No migration", runbook)
        self.assertIn("A direct r0146 → r0147 plan advances source to R, then restarts `isadoraair-engine`. "
                      "Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


class R0147HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_is_the_exact_linear_accepted_chain_on_r0146(self):
        """Published provenance only: runs in every clone, with nothing skipped."""
        self.assertEqual(_git("rev-parse", f"{R0146_RELEASE}^{{tree}}"), R0146_TREE)
        commits = [commit for commit, _tree in ENGINEERING_CHAIN]
        self.assertEqual(len(commits), 9)
        self.assertEqual(len(set(commits)), 9)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0146_RELEASE}..{ENGINEERING}").split(), commits)
        parent = R0146_RELEASE
        for commit, tree in ENGINEERING_CHAIN:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            self.assertNotEqual(_changed(f"{commit}^", commit), set(), commit)
            self.assertNotIn("cherry picked from commit", _git("log", "-1", "--format=%B", commit), commit)
            parent = commit
        self.assertEqual(_git("rev-list", "--merges", f"{R0146_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_introducing("deploy/releases/r0146.json"), [R0146_RELEASE])
        self.assertEqual(_git("rev-parse", ENGINEERING + "^{tree}"), ENGINEERING_TREE)

    def test_change_inventory_is_exact(self):
        self.assertEqual(_changed(R0146_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0146_RELEASE, ENGINEERING, "--diff-filter=M"), CHANGE_FILES)
        self.assertEqual(_changed(R0146_RELEASE, ENGINEERING, "--diff-filter=ADRTC"), set())
        production = set()
        for commit, _tree in ENGINEERING_CHAIN:
            files = _changed(f"{commit}^", commit)
            self.assertLessEqual(files, CHANGE_FILES, commit)
            production |= {path for path in files if "/tests/" not in path}
        self.assertEqual(production, PRODUCT_FILES)
        # Only the engine module is production code; everything else is an existing test module.
        self.assertTrue(all("/tests/" in path for path in CHANGE_FILES - PRODUCT_FILES))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0146_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0146_RELEASE}..{release}").split(),
                         [commit for commit, _tree in ENGINEERING_CHAIN] + [release])
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0146_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0146_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0146_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0146_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0146_RELEASE, release)
                          if "/static/" in path or "/templates/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0146_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0146_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        touched = _changed(R0146_RELEASE, release)
        self.assertEqual({path for path in touched if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        self.assertEqual(touched - RELEASE_FILES, CHANGE_FILES)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0147.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0146_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_production_change_is_exactly_the_engine_module(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0146_RELEASE, release)
        modules = {path[:-3].replace("/", ".") for path in changed if path.endswith(".py") and "/tests/" not in path}
        self.assertEqual(modules, PRODUCTION_MODULES)
        self.assertEqual({path for path in changed if not path.endswith(".py") and not path.startswith("docs/")
                          and path != MANIFEST_PATH}, set())

    def test_the_engine_diff_is_additive_and_bounded_to_the_declared_members(self):
        before = _show(R0146_RELEASE, "library/services/engine.py").decode()
        after = _show(ENGINEERING, "library/services/engine.py").decode()
        self.assertEqual(_engine_names(before) - _engine_names(after), set(), "nothing was removed")
        self.assertEqual(_engine_names(after) - _engine_names(before), ADDED_ENGINE_NAMES)
        old, new = _engine_members(before), _engine_members(after)
        changed = {key for key, dumped in new.items() if key in old and old[key] != dumped}
        self.assertEqual(changed, CHANGED_ENGINE_MEMBERS)
        # Module-level code other than the two new constants is byte-for-byte the r0146 code.
        old_tree, new_tree = ast.parse(before), ast.parse(after)
        def top_level(tree):
            return {(type(node).__name__, getattr(node, "name", None) or ast.dump(node)): ast.dump(node)
                    for node in tree.body if not isinstance(node, ast.ClassDef)}
        added_constants = {("", name) for _scope, name in ADDED_ENGINE_NAMES if _scope == ""}
        old_top, new_top = top_level(old_tree), top_level(new_tree)
        self.assertEqual({key for key in old_top if key not in new_top or old_top[key] != new_top[key]}, set())
        self.assertEqual(len([key for key in new_top if key not in old_top]), len(added_constants))

    def test_declared_restart_is_exactly_the_engine(self):
        """Every changed or added name lives in Deck / PlaybackEngine (or is one of the two new constants). Only the
        engine service's run_engine command constructs a PlaybackEngine, only engine.py constructs a Deck, and no
        other production module references any changed or added name: so no other long-running service executes
        the changed code. Gunicorn's over-approximating import graph reaches library.services.engine, but no Gunicorn
        code path constructs or runs a PlaybackEngine."""
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        reach = {service: graph.service_reach(service) for service in LONG_RUNNING_ENTRIES}
        self.assertIn("library.services.engine", reach["isadoraair-engine"])
        searched = {name for _scope, name in ADDED_ENGINE_NAMES | CHANGED_ENGINE_MEMBERS if name} - GENERIC_CHANGED_NAMES
        self.assertTrue({"_first_audio_deadline", "ORDINARY_FIRST_AUDIO_GRACE_S", "_start_next_track"} <= searched)
        production = [path for path in graph.files if "/tests/" not in path]
        for path in production:
            if path == "library/services/engine.py":
                continue
            tree = ast.parse(_show(release, path).decode("utf-8"))
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
            own = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            with self.subTest(path=path):
                # A module's OWN same-named method (library/services/vt_audio.py has an unrelated
                # ``_first_audio_deadline`` of its own) is not a reference to the engine's; such a module must not
                # import the engine at all.
                self.assertEqual((names - own) & searched, set())
                if own & searched:
                    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
                    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                                 for alias in node.names}
                    self.assertFalse({name for name in imported if name and "services.engine" in name},
                                     "a module defining a same-named method must not import the engine")
                for constructed in ("PlaybackEngine", "Deck"):
                    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                             and getattr(node.func, "id", getattr(node.func, "attr", None)) == constructed]
                    if constructed == "PlaybackEngine" and path == "library/management/commands/run_engine.py":
                        continue
                    self.assertEqual(calls, [], f"only the engine service constructs a {constructed}")
        run_engine = ast.parse(_show(release, "library/management/commands/run_engine.py").decode("utf-8"))
        self.assertTrue([node for node in ast.walk(run_engine) if isinstance(node, ast.Call)
                         and getattr(node.func, "id", None) == "PlaybackEngine"])
        self.assertEqual(LONG_RUNNING_ENTRIES["isadoraair-engine"], "library.management.commands.run_engine")
        engine_tree = ast.parse(_show(release, "library/services/engine.py").decode("utf-8"))
        deck_builders = [fn.name for cls in engine_tree.body if isinstance(cls, ast.ClassDef) and cls.name == "PlaybackEngine"
                         for fn in cls.body if isinstance(fn, ast.FunctionDef)
                         for node in ast.walk(fn) if isinstance(node, ast.Call)
                         and getattr(node.func, "id", None) == "Deck"]
        self.assertEqual(len(deck_builders), 1, "Deck is built by exactly one PlaybackEngine method")


class R0147MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0146_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0147.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0147", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0147"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0147ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0147_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0146_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 146)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0146_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0146_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0147_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, LAST + 1)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0144"], commits["r0145"], commits["r0146"], commits["r0147"]),
                         (R0132_RELEASE, R0144_RELEASE, R0145_RELEASE, R0146_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0146 -> r0147", out.getvalue())


class R0147TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0147", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0147", release))
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

    def test_direct_plan_is_migration_free_engine_only_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0146", R0146_RELEASE), ("r0147", release)])
        self._assert_plan("r0146")

    def test_catch_up_from_r0145_stays_engine_only(self):
        self._assert_plan("r0145")

    def test_catch_up_from_r0144_keeps_the_r0145_restarts(self):
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

    def test_catch_up_from_r0132_keeps_three_migrations(self):
        self._assert_plan("r0132")

    def test_catch_up_from_r0130_keeps_all_four_migrations(self):
        self._assert_plan("r0130")

    def test_catch_up_from_r0124_preserves_all_intervening_actions(self):
        self._assert_plan("r0124")

    def test_restart_rehearsal_touches_only_the_engine(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[2] for call in runner.calls if call[1] == "restart"], ["isadoraair-engine.service"])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in ("isadoraair-gunicorn", "isadoraair-monitoring", "isadoraair-encoders", "isadoraair-rbds",
                          "isadoraair-active-message-worker", "isadoraair-spoken-render-worker", "isadoraair-render",
                          "isadoraair-validation"):
            self.assertNotIn(untouched, touched)


class R0147ReachabilityTests(SimpleTestCase):
    def test_every_published_object_is_reachable_from_r_alone(self):
        """A bare repository holding only R's history (what a fresh clone of R gets) has every object this contract
        relies on: r0146, the nine accepted commits with their trees, R and every release file."""
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            for commit, tree in ENGINEERING_CHAIN:
                self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}", cwd=bare), tree)
            self.assertEqual(_git("rev-parse", f"{R0146_RELEASE}^{{tree}}", cwd=bare), R0146_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            isolated = derive_plan(_repository(bare), release, R0146_RELEASE, "r0147", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0146_RELEASE, "r0147", known_units=_known_units())
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


class R0147PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0147",
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
        self._assert_parity("r0146")

    def test_catch_up_from_r0145_application_and_protected_planners_match(self):
        self._assert_parity("r0145")

    def test_catch_up_from_r0144_application_and_protected_planners_match(self):
        self._assert_parity("r0144")

    def test_catch_up_from_r0132_application_and_protected_planners_match(self):
        self._assert_parity("r0132")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
