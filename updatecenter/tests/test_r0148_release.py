"""r0148 immutable release contract (Aircheck capture source shown truthfully, roadmap 2.0-A).

Published r0147 (3027223) plus one accepted engineering commit of ``feature/2.0-a-aircheck-airtap-admin``
(``be7bc128``), then R:
  be7bc128 -- the accepted engineering commit itself, a direct child of r0147, NOT cherry-picked, squashed, rebased or
              rewritten: ``aircheck/admin.py`` and ``aircheck/services/recorder.py`` (product code),
              ``aircheck/models.py`` (documentation only: a docstring and a comment) and the new test module
              ``aircheck/tests/test_source_attribution.py``;
  R        -- metadata-only child of be7bc128 adding this contract, the r0148 manifest and two release documents.

Because the accepted commit is the published commit, every provenance claim is provable from the published history
alone: there are no unpublished source objects and nothing here skips in a public clone.

r0148 declares no migration and restarts Gunicorn only. Of the changed production modules, ``aircheck.admin`` and
``aircheck.services.recorder`` are imported only by Gunicorn (its URLconf loads the admin and the views, and the views
load the recorder), the Aircheck buffer and recovery services are ``Type=oneshot`` units that load the current code the
next time they are invoked, and ``aircheck.models`` is the only changed module the other long-running services import:
its change is provably documentation-only (the module's AST without docstrings is byte-for-byte the r0147 AST), so none
of them executes different code. The encoder script, ``DEFAULT_INPUT_DEVICE`` and the encoder-group machinery are not
touched.
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
R0147_RELEASE = "3027223c44ae7e7be6446c29ff6a2a74fa18cfd4"
R0147_TREE = "404ce679232b9aa7b532e0f80e1d3cd1aa3f8ab1"
#: The independently accepted 2.0-A engineering commit: the direct child of r0147.
ENGINEERING = "be7bc128d24f63bf7d147828dad3b7a8ea73919e"
ENGINEERING_TREE = "da1c190fdc1bd201fe43b4d3bc29d8199562b7cb"
CHANGE_FILES = {
    "aircheck/admin.py",
    "aircheck/models.py",
    "aircheck/services/recorder.py",
    "aircheck/tests/test_source_attribution.py",
}
PRODUCT_FILES = {"aircheck/admin.py", "aircheck/models.py", "aircheck/services/recorder.py"}
PRODUCTION_MODULES = {"aircheck.admin", "aircheck.models", "aircheck.services.recorder"}
#: Changed modules that only Gunicorn can load (everything else that loads a changed module loads aircheck.models only).
GUNICORN_ONLY_MODULES = {"aircheck.admin", "aircheck.services.recorder"}
#: Oneshot Aircheck commands: they import the recorder but are started by timers, never by a long-running service.
ONE_SHOT_COMMANDS = {
    "aircheck.management.commands.maintain_aircheck_buffer",
    "aircheck.management.commands.maintain_aircheck_recovery",
}
MANIFEST_PATH = "deploy/releases/r0148.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0148.md",
    "docs/releases/r0148_installation.md",
    "updatecenter/tests/test_r0148_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn",)
CATCH_UP_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_WITH_MONITORING = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = CATCH_UP_WITH_MONITORING + ("isadoraair-active-message-worker",)
LAST = 148  # the release number of this contract
#: installed release -> (installed commit, releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0147": (R0147_RELEASE, ("r0148",), (), None, RESTARTS),
    "r0146": (R0146_RELEASE, ("r0147", "r0148"), (), None, CATCH_UP_RESTARTS),
    "r0145": (R0145_RELEASE, ("r0146", "r0147", "r0148"), (), None, CATCH_UP_RESTARTS),
    "r0144": (R0144_RELEASE, ("r0145", "r0146", "r0147", "r0148"), (), None, CATCH_UP_WITH_MONITORING),
    "r0143": (R0143_RELEASE, tuple(f"r{n:04d}" for n in range(144, LAST + 1)), (), None, CATCH_UP_WITH_MONITORING),
    "r0142": (R0142_RELEASE, tuple(f"r{n:04d}" for n in range(143, LAST + 1)), (), None, CATCH_UP_WITH_MONITORING),
    "r0141": (R0141_RELEASE, tuple(f"r{n:04d}" for n in range(142, LAST + 1)), (), None, CATCH_UP_WITH_MONITORING),
    "r0140": (R0140_RELEASE, tuple(f"r{n:04d}" for n in range(141, LAST + 1)), (), None, CATCH_UP_WITH_MONITORING),
    "r0139": (R0139_RELEASE, tuple(f"r{n:04d}" for n in range(140, LAST + 1)), (R0140_MIGRATION,), "additive",
              CATCH_UP_WITH_MONITORING),
    "r0138": (R0138_RELEASE, tuple(f"r{n:04d}" for n in range(139, LAST + 1)), (R0139_MIGRATION, R0140_MIGRATION),
              "additive", CATCH_UP_WITH_MONITORING),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, LAST + 1)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_WITH_MONITORING),
    "r0132": (R0132_RELEASE, tuple(f"r{n:04d}" for n in range(133, LAST + 1)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_WITH_MONITORING),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, LAST + 1)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_WITH_MONITORING),
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
RUNBOOK = "docs/releases/r0148_installation.md"
NOTES = "docs/releases/r0148.md"


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
        raise AssertionError(f"r0148 manifest introduced more than once: {commits!r}")
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



def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _code_dump(source: str) -> str:
    """AST of ``source`` without docstrings. Comments are not part of an AST, so two sources whose code dumps are
    equal differ only in comments, docstrings and layout."""
    return ast.dump(_strip_docstrings(ast.parse(source)))


class _RevertRecorderChange(ast.NodeTransformer):
    """Undo exactly the two code changes 2.0-A makes to the recorder, so the result must equal r0147's code."""

    def visit_ImportFrom(self, node):
        if node.module == "encoders.services.encoder_manager":
            node.names = [alias for alias in node.names if alias.name != "DEFAULT_INPUT_DEVICE"]
        return node

    def visit_keyword(self, node):
        if node.arg == "source_device" and isinstance(node.value, ast.Name) and node.value.id == "DEFAULT_INPUT_DEVICE":
            node.value = ast.Attribute(value=ast.Name(id="cfg", ctx=ast.Load()), attr="source_device", ctx=ast.Load())
        return node



class R0148ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0148.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0148", "r0147", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0147 = json.loads(_show(R0147_RELEASE, "deploy/releases/r0147.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0147["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0147["schema_version"])
        self.assertEqual(data["minimum_updater_protocol_version"], r0147["minimum_updater_protocol_version"])
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
        self.assertEqual(data["services_requiring_restart"], ["isadoraair-gunicorn"])
        self.assertEqual(list(data), list(r0147))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Aircheck source truthfulness", "2.0-A", "no longer offers an ineffective editable",
                         "airtap", "read-only", "newly created Aircheck sessions record airtap",
                         "Existing sessions and the legacy column are unchanged", "Restart Gunicorn only",
                         "no migration, engine, encoder, Monitoring, static, unit, package or protected-runtime change"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0148")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0148", "r0147"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_the_engine_restart_safeguard_does_not_gate_a_gunicorn_only_plan(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        self.assertFalse(engine_restart_safety.plan_restarts_engine(
            SimpleNamespace(services_requiring_restart=data["services_requiring_restart"])))

    def test_release_notes_document_the_change_its_boundaries_and_limitations(self):
        notes = _flat(_text(NOTES))
        for required in (
            "Independent Codex review: ACCEPT — ready for release qualification",
            "the accepted commit itself, not cherry-picked, squashed or rewritten",
            "where the change is **documentation only**", "**The inherited problem.**", "**The correction.**",
            "no longer offers an ineffective editable source-device field",
            "`airtap — shared default encoder-group source`",
            "there is no independent capture device selection",
            "Aircheck continues to record the existing shared program feed",
            "Recording routing, format, bitrate, output directory, filename template, cutting, locking, file handling, "
            "finalization and error behavior are unchanged",
            "**Newly created** Aircheck sessions use the authoritative `airtap` identifier",
            "A stale or arbitrary value in the legacy configuration column is ignored for new sessions",
            "**Historical and already-active sessions keep their original metadata.**",
            "Nothing is backfilled, normalized or reinterpreted",
            "The legacy configuration column and its stored value are preserved",
            "cannot modify it", "**No database migration is necessary.**",
            "**What the display means.**", "establishes the source **identity**",
            "It is not a probe", "does not show that the encoder group is running",
            "opens no device and contacts no encoder or Liquidsoap control socket",
            "17 of 17", "690 tests run, 689 passed, 1 skipped, 0 failures, 0 errors",
            "673 run on unmodified r0147 (672 passed, the same 1 skipped)", "the difference is exactly the 17 new tests",
            "`ISADORAAIR_FDKAAC_SOURCE_DIR`", "r0058 HE-AAC capability gating, r0082 Aircheck containment and recovery",
            "against unmodified r0147 nine of the new tests fail", "fails three, and reverting only the Admin change fails six",
            "No physical Liquidsoap or ALSA experiment was run, and none is claimed",
            "The three separately tracked 1.22 test deficiencies are outside this release",
            "## Unchanged behavior", "## What it is not",
            "a rolling archive or retrospective recording", "a configurable working-buffer size",
            "an archive capacity calculator", "retention or storage management",
            "runtime reconciliation of the Aircheck format or bitrate with the running encoder",
            "scheduled Aircheck or the Talent Skimmer", "any audio-routing or encoder-topology change",
            "## Limitations (accepted; not remediated in r0148)", "**Identity, not health.**",
            "it is not evidence of the encoder's current state at any station",
            "`migrations_required` is empty", "`migration_compatibility` is `null`", "`protected_runtime` is `null`",
            "1. `isadoraair-gunicorn`", "Only Gunicorn loads the changed Admin and recorder code",
            "oneshot units on timers and load the current code the next time they are invoked",
            "load `aircheck.models` only, which changed in documentation alone",
            "**the audio engine and encoders remain running**", "program audio is not interrupted",
            "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        limitations = _section(_text(NOTES), "## Limitations (accepted; not remediated in r0148)")
        items = [_flat(item) for item in re.split(r"^\s*\d+\. ", limitations, flags=re.M)[1:]]
        self.assertEqual(len(items), 5)
        self.assertRegex(items[0], r"does not measure the running encoder")
        for forbidden in ("2. `isadoraair-", "Restart the engine", "rolling archive is included", "guarantees",
                          "proves the encoder", "fully protected", "eliminates"):
            self.assertNotIn(forbidden, notes)
        # Nothing excluded is described as done in this release.
        self.assertNotRegex(notes, r"(?i)(rolling archive|skimmer|capacity calculator)[^.]*\b(is|are|was|were) (now )?"
                                   r"(implemented|resolved|fixed|included)")

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = _flat(_text(RUNBOOK))
        for required in (
            R0147_RELEASE, R0147_TREE, ENGINEERING, ENGINEERING_TREE,
            "Publication does not authorize deployment", "separate station-specific", "There is **no migration**",
            "Nothing else.", "no engine, encoder, Monitoring, RBDS or worker restart", "stop and investigate",
            "a station on r0146 also restarts the engine for r0147 (interrupting program audio)",
            "a station on r0144 also restarts Gunicorn and Monitoring for r0145",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`", "Do not manually run `migrate`",
            "Do not assume a catch-up installation is Gunicorn-only because the direct transition is",
            "## Common read-only preflight", "**Do not start or stop a recording to prepare for or to prove this release.**",
            "A Gunicorn restart does not stop Liquidsoap's recorder",
            "a recording already running keeps its original session row",
            "briefly interrupts the web console and its requests",
            "The engine restart safeguard does not apply to a Gunicorn-only plan",
            "that milestone is not evidence that anything was applied or restarted",
            "confirm the effective restart set against the plan",
            "Do not start or stop an Aircheck recording, key a microphone, alter a log or change a station setting only to "
            "demonstrate the correction, and do not interrupt a recording that is running",
            "`airtap — shared default encoder-group source`", "there is **no** editable source-device field",
            "**Format**, **Bitrate**, **Output directory**, **Filename template**",
            "Do not save the page merely to prove it works", "older sessions may show a value other than `airtap`",
            "that is historical and is not corrected", "any running session is still running",
            "**New-session attribution**", "only when the operator separately authorizes a recording",
            "It is not required for acceptance and must not be forced",
            "## KOGR acceptance", "## WRJE acceptance", "do not reuse KOGR's results",
            "it is not evidence of WRJE's actual encoder state", "do so read-only and record what is found for WRJE alone",
            "KOGR's observations do not establish it",
            "**A successful installation at one station is not authorization to install at the other.**",
            "Stop and preserve", "Do not use manual Git resets", "not rollback authority",
        ):
            self.assertIn(required, runbook)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = _flat(_text(NOTES))
        runbook = _flat(_text(RUNBOOK))
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap, Aircheck-configuration or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0148 release commit R. 2. Restart "
                      "`isadoraair-gunicorn`. No migration", runbook)
        self.assertIn("A direct r0147 → r0148 plan advances source to R, then restarts `isadoraair-gunicorn`. "
                      "Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


class R0148HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_is_the_exact_accepted_commit_on_r0147(self):
        """Published provenance only: runs in every clone, with nothing skipped."""
        self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}^{{tree}}"), R0147_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0147_RELEASE}..{ENGINEERING}").split(), [ENGINEERING])
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", ENGINEERING).split()[1:], [R0147_RELEASE])
        self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}"), ENGINEERING_TREE)
        self.assertNotEqual(_changed(f"{ENGINEERING}^", ENGINEERING), set())
        self.assertNotIn("cherry picked from commit", _git("log", "-1", "--format=%B", ENGINEERING))
        self.assertEqual(_git("rev-list", "--merges", f"{R0147_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_introducing("deploy/releases/r0147.json"), [R0147_RELEASE])

    def test_change_inventory_is_exact(self):
        self.assertEqual(_changed(R0147_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0147_RELEASE, ENGINEERING, "--diff-filter=M"), PRODUCT_FILES)
        self.assertEqual(_changed(R0147_RELEASE, ENGINEERING, "--diff-filter=A"),
                         {"aircheck/tests/test_source_attribution.py"})
        self.assertEqual(_changed(R0147_RELEASE, ENGINEERING, "--diff-filter=DRTC"), set())
        # Only these three modules are production code; the one other file is a new test module.
        self.assertEqual({path for path in CHANGE_FILES if "/tests/" not in path}, PRODUCT_FILES)

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0147_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0147_RELEASE}..{release}").split(), [ENGINEERING, release])
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_release_preserves_the_earlier_release_contracts_unchanged(self):
        """The r0146 public-clone contract correction and every earlier contract are byte-for-byte untouched."""
        release = _release_commit() or ENGINEERING
        touched = _changed(R0147_RELEASE, release)
        self.assertFalse([path for path in touched if re.match(r"updatecenter/tests/test_r01\d\d_release\.py$", path)
                          and path != "updatecenter/tests/test_r0148_release.py"])
        existing = [path for path in _git("ls-tree", "-r", "--name-only", R0147_RELEASE, "updatecenter/tests/").split()
                    if re.match(r"updatecenter/tests/test_r\d{4}_release\.py$", path)]
        self.assertIn("updatecenter/tests/test_r0146_release.py", existing)
        self.assertIn("updatecenter/tests/test_r0147_release.py", existing)
        for path in existing:
            self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}:{path}"), _git("rev-parse", f"{release}:{path}"), path)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0147_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0147_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0147_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0147_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0147_RELEASE, release)
                          if "/static/" in path or "/templates/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        touched = _changed(R0147_RELEASE, release)
        self.assertEqual({path for path in touched if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        self.assertEqual(touched - RELEASE_FILES, CHANGE_FILES)

    def test_encoder_and_engine_code_is_untouched(self):
        release = _release_commit() or ENGINEERING
        for prefix in ("encoders", "library", "monitoring", "rbds", "hardware", "production", "ogremote"):
            self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}:{prefix}"), _git("rev-parse", f"{release}:{prefix}"),
                             prefix)
        # The canonical source the Admin and recorder now use is the unchanged encoder-group constant.
        encoder_manager = _show(release, "encoders/services/encoder_manager.py").decode()
        self.assertEqual(len(re.findall(r'^DEFAULT_INPUT_DEVICE = "airtap"$', encoder_manager, re.M)), 1)
        self.assertEqual(
            _show(R0147_RELEASE, "encoders/services/encoder_manager.py"), _show(release, "encoders/services/encoder_manager.py"))

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0148.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0147_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_cross_check_rejects_an_incorrect_declaration(self):
        """Negative controls: a declaration that the diff does not support is reported, not accepted."""
        release = _require_release(self)
        data = json.loads(_show(release, MANIFEST_PATH))
        app_paths = planner.app_label_paths_for_checkout(PROJECT_ROOT)
        for label, change in (
            ("migration that does not exist", {"migrations_required": ["library.0104_does_not_exist"],
                                               "migration_compatibility": "additive"}),
            ("new unit that does not exist", {"systemd_units_new_required": ["isadoraair-nonexistent.service"]}),
            ("changed unit that does not exist", {"systemd_units_changed": ["isadoraair-nonexistent.service"]}),
        ):
            with self.subTest(label):
                mutated = manifest_mod.validate_manifest_dict({**data, **change}, source_label="r0148.json")
                findings = cross_check.cross_check_release(mutated, release, PROJECT_ROOT, app_paths, R0147_RELEASE)
                self.assertTrue(list(findings), label)

    def test_production_change_is_exactly_the_declared_modules(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0147_RELEASE, release)
        modules = {path[:-3].replace("/", ".") for path in changed if path.endswith(".py") and "/tests/" not in path}
        self.assertEqual(modules, PRODUCTION_MODULES)
        self.assertEqual({path for path in changed if not path.endswith(".py") and not path.startswith("docs/")
                          and path != MANIFEST_PATH}, set())

    def test_models_change_is_documentation_only(self):
        before = _show(R0147_RELEASE, "aircheck/models.py").decode()
        after = _show(ENGINEERING, "aircheck/models.py").decode()
        self.assertNotEqual(before, after)
        # Same code: field definitions (including help_text, so no migration), Meta, methods.
        self.assertEqual(_code_dump(before), _code_dump(after))

    def test_recorder_change_is_exactly_the_canonical_source_import_and_value(self):
        before = _show(R0147_RELEASE, "aircheck/services/recorder.py").decode()
        after = _show(ENGINEERING, "aircheck/services/recorder.py").decode()
        self.assertNotEqual(before, after)
        reverted = _RevertRecorderChange().visit(ast.parse(after))
        self.assertEqual(ast.dump(_strip_docstrings(reverted)), _code_dump(before))
        self.assertEqual(len(re.findall(r"source_device=DEFAULT_INPUT_DEVICE", after)), 1)
        self.assertNotIn("cfg.source_device", after)
        self.assertEqual(len(re.findall(r"cfg\.source_device", before)), 1)

    def test_admin_offers_no_editable_source_and_shows_the_canonical_one(self):
        tree = ast.parse(_show(ENGINEERING, "aircheck/admin.py").decode())
        admin_class = next(node for node in tree.body
                           if isinstance(node, ast.ClassDef) and node.name == "AircheckConfigAdmin")
        assigned = {target.id: node.value for node in admin_class.body if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        fields = [constant.value for constant in ast.walk(assigned["fieldsets"])
                  if isinstance(constant, ast.Constant) and isinstance(constant.value, str)]
        self.assertNotIn("source_device", fields)
        self.assertIn("effective_source", fields)
        self.assertEqual(ast.literal_eval(assigned["readonly_fields"]), ["effective_source"])
        method = next(node for node in admin_class.body
                      if isinstance(node, ast.FunctionDef) and node.name == "effective_source")
        imported = [alias.name for node in ast.walk(method) if isinstance(node, ast.ImportFrom)
                    for alias in node.names]
        self.assertEqual(imported, ["DEFAULT_INPUT_DEVICE"])

    def test_the_only_session_creation_path_is_the_recorder(self):
        release = _release_commit() or ENGINEERING
        writers = []
        for path in _git("ls-tree", "-r", "--name-only", release).split():
            if not path.endswith(".py") or "/tests/" in path or "/migrations/" in path:
                continue
            text = _show(release, path).decode("utf-8", errors="ignore")
            if re.search(r"(?<!class )AircheckSession\s*\(|AircheckSession\.objects\.(create|bulk_create|get_or_create|update_or_create)", text):
                writers.append(path)
        self.assertEqual(writers, ["aircheck/services/recorder.py"])

    def test_declared_restart_is_exactly_gunicorn(self):
        """Only Gunicorn can load the changed Admin and recorder code; the buffer and recovery services are oneshot
        units; every other long-running service loads only aircheck.models, whose change is documentation-only."""
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        reach = {service: graph.service_reach(service) for service in LONG_RUNNING_ENTRIES}
        for module in PRODUCTION_MODULES | ONE_SHOT_COMMANDS:
            self.assertIsNotNone(graph.path(module), module)
        self.assertEqual(GUNICORN_ONLY_MODULES <= reach["isadoraair-gunicorn"], True)
        for service, modules in reach.items():
            with self.subTest(service=service):
                if service != "isadoraair-gunicorn":
                    self.assertEqual(modules & GUNICORN_ONLY_MODULES, set())
                    self.assertEqual(modules & {"aircheck.views", "aircheck.services.recovery"}, set())
                self.assertEqual(modules & ONE_SHOT_COMMANDS, set())
                self.assertIn("aircheck.models", modules)
        loading = {service for service, modules in reach.items() if modules & GUNICORN_ONLY_MODULES}
        self.assertEqual(loading, set(RESTARTS))
        # The Aircheck buffer and recovery services are oneshot units started by timers.
        for unit, command in (("isadoraair-aircheck-buffer", "maintain_aircheck_buffer"),
                              ("isadoraair-aircheck-recovery", "maintain_aircheck_recovery")):
            text = _show(release, f"deploy/{unit}.service").decode()
            self.assertIn("Type=oneshot", text, unit)
            self.assertIn(f"manage.py {command}", text, unit)
            self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}:deploy/{unit}.service"),
                             _git("rev-parse", f"{release}:deploy/{unit}.service"))
            self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}:deploy/{unit}.timer"),
                             _git("rev-parse", f"{release}:deploy/{unit}.timer"))
        # The modules the encoders import from this app are models only (a documentation-only change).
        self.assertEqual(reach["isadoraair-encoders"] & {m for m in reach["isadoraair-encoders"] if m.startswith("aircheck")},
                         {"aircheck", "aircheck.apps", "aircheck.models"} & reach["isadoraair-encoders"])


class R0148MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0147_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0148.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0148", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0148"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0148ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0148_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0147_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 147)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0147_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0147_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0148_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, LAST + 1)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0144"], commits["r0145"], commits["r0146"], commits["r0147"],
                          commits["r0148"]),
                         (R0132_RELEASE, R0144_RELEASE, R0145_RELEASE, R0146_RELEASE, R0147_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0147 -> r0148", out.getvalue())


class R0148TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0148", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0148", release))
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
        self.assertEqual(engine_restart_safety.plan_restarts_engine(plan), "isadoraair-engine" in restarts)

    def test_direct_plan_is_migration_free_gunicorn_only_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0147", R0147_RELEASE), ("r0148", release)])
        self._assert_plan("r0147")

    def test_direct_plan_never_touches_the_engine_or_other_services(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0147_RELEASE, "r0148", known_units=_known_units())
        for untouched in ("isadoraair-engine", "isadoraair-monitoring", "isadoraair-encoders", "isadoraair-rbds",
                          "isadoraair-active-message-worker", "isadoraair-spoken-render-worker", "isadoraair-render",
                          "isadoraair-validation"):
            self.assertNotIn(untouched, plan.services_requiring_restart)

    def test_catch_up_from_r0146_keeps_the_r0147_engine_restart(self):
        self._assert_plan("r0146")

    def test_catch_up_from_r0145_keeps_the_engine_restarts(self):
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

    def test_an_installed_commit_outside_the_release_ancestry_is_rejected(self):
        release = _require_release(self)
        with self.assertRaises(Exception):
            derive_plan(_repository(), release, "0" * 40, "r0148", known_units=_known_units())
        with self.assertRaises(Exception):
            # r0147's own commit presented as r0146 is not the commit the chain records for r0146.
            derive_plan(_repository(), release, R0147_RELEASE, "r0146", known_units=_known_units())

    def test_restart_rehearsal_touches_only_gunicorn(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[2] for call in runner.calls if call[1] == "restart"], ["isadoraair-gunicorn.service"])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in ("isadoraair-engine", "isadoraair-monitoring", "isadoraair-encoders", "isadoraair-rbds",
                          "isadoraair-active-message-worker", "isadoraair-spoken-render-worker", "isadoraair-render",
                          "isadoraair-validation", "isadoraair-aircheck"):
            self.assertNotIn(untouched, touched)


class R0148ReachabilityTests(SimpleTestCase):
    def test_every_published_object_is_reachable_from_r_alone(self):
        """A bare repository holding only R's history (what a fresh clone of R gets) has every object this contract
        relies on: r0147, the accepted commit with its tree, R and every release file."""
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}", cwd=bare), ENGINEERING_TREE)
            self.assertEqual(_git("rev-parse", f"{R0147_RELEASE}^{{tree}}", cwd=bare), R0147_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            isolated = derive_plan(_repository(bare), release, R0147_RELEASE, "r0148", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0147_RELEASE, "r0148", known_units=_known_units())
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


class R0148PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0148",
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
        self._assert_parity("r0147")

    def test_catch_up_from_r0146_application_and_protected_planners_match(self):
        self._assert_parity("r0146")

    def test_catch_up_from_r0145_application_and_protected_planners_match(self):
        self._assert_parity("r0145")

    def test_catch_up_from_r0144_application_and_protected_planners_match(self):
        self._assert_parity("r0144")

    def test_catch_up_from_r0132_application_and_protected_planners_match(self):
        self._assert_parity("r0132")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
