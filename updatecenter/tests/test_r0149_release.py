"""r0149 immutable release contract (Hour Refill Start Next eligibility with a connected Remote DJ, roadmap 2.27).

Published r0148 (4e13350) plus the five accepted engineering commits of
``feature/2.27-start-next-remote-dj-eligibility``, then R:
  e710899 .. a86eec5 -- the accepted engineering commits themselves, linear on r0148, NOT cherry-picked, squashed,
                        rebased or rewritten: library/services/engine.py, library/services/hour_refill_causality.py,
                        library/services/hour_refill_projection.py and library/admin.py (help text), two existing test
                        modules and five existing 2.27 roadmap contract documents (11 files);
  R                  -- metadata-only child of a86eec5 adding this contract, the r0149 manifest and two release
                        documents.

Because the accepted commits are the published commits, every provenance claim is provable from the published
history alone: there are no unpublished source objects and nothing here skips in a public clone.

r0149 declares no migration and restarts Gunicorn, then the engine. The executable change is exactly: in the engine,
the Start Next ownership snapshot and one new read-only microphone helper; in the causality module, the Start Next
classifier and its evidence builder; in the projection module, the playback-window capture and the transport block;
and one Admin help string. Engine startup, shutdown, resume, first-real-audio recovery, the deck mixer and the deck
bus are untouched, so the r0146 Gate A / Gate B GStreamer preflight is not repeated for a station already accepted
on r0148 with an unchanged runtime (the runbook keeps it mandatory elsewhere), and the engine restart safeguard is
unchanged and still gates this release.
"""
from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tempfile
import time
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
from isadoraair_updater.systemd import SystemdError, SystemdManager
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
R0148_RELEASE = "4e13350940d5a4b6670dc319d6804e5249906352"
R0148_TREE = "89aa7226c3bc3a094e8512a89f227b1c350dc22d"
#: (commit, tree) in first-parent order on r0148: the independently accepted 2.27 engineering commits.
ENGINEERING_CHAIN = (
    ("e710899bd503d79f0808d7ae55ed2b507c0b0ac0", "bfdad580da5f4cbd0a5202fb2a450cef4e12c500"),
    ("63d7849e0acc5e1aa6e4917c4438659839a356ee", "adce87fa936ef45f2793591e44b48c04e20894dc"),
    ("8ae4f4b64c329aa7aab788446244920b92bfa4f1", "b58d592435a435ca4237004cbdcc813296a2d855"),
    ("4a89b64913c4a550e0a8ed46b549ee4d22a41a10", "a434393262dbb86e76c1cc6cea3bea4a0cc81589"),
    ("a86eec5bd8a3aef3134ea562e0249788e09b75ec", "feef3890f432f3ef417213fe1813336f60c30646"),
)
ENGINEERING = ENGINEERING_CHAIN[-1][0]
ENGINEERING_TREE = ENGINEERING_CHAIN[-1][1]
#: The review-remediation commit and the candidate it sits on: documentation and one docstring.
REMEDIATION_PARENT = ENGINEERING_CHAIN[-2][0]
APPROVED_IDENTITY = "Justin Reed <celltech161@gmail.com>"
PRODUCT_FILES = {
    "library/admin.py",
    "library/services/engine.py",
    "library/services/hour_refill_causality.py",
    "library/services/hour_refill_projection.py",
}
TEST_FILES = {
    "library/tests/test_hour_refill_causality_2_27.py",
    "library/tests/test_hour_refill_start_next_2_27.py",
}
CONTRACT_DOCS = {
    "docs/roadmap/2_27_PHASE_B_PROPOSAL_CONTRACT.md",
    "docs/roadmap/2_27_PHASE_C_GUARDED_INSTALL_CONTRACT.md",
    "docs/roadmap/2_27_PHASE_D_OPERATIONAL_CONTRACT.md",
    "docs/roadmap/2_27_PHASE_D_ROLLOUT_PACKET.md",
    "docs/roadmap/2_27_START_NEXT_CAUSALITY_EXTENSION.md",
}
CHANGE_FILES = PRODUCT_FILES | TEST_FILES | CONTRACT_DOCS
PRODUCTION_MODULES = {path[:-3].replace("/", ".") for path in PRODUCT_FILES}
#: The whole executable change, by module: (added members, changed members). Docstrings and comments are ignored.
EXECUTABLE_CHANGE = {
    "library/services/engine.py": (
        {("PlaybackEngine", "_hour_refill_mic_engaged")},
        {("PlaybackEngine", "_start_next_ownership")},
    ),
    "library/services/hour_refill_causality.py": (
        set(), {("", "classify_start_next_observation"), ("", "start_next_evidence_from_engine")},
    ),
    "library/services/hour_refill_projection.py": (
        {("", "remote_dj_connection_permitted")},
        {("", "capture_playback_window"), ("", "project_refill"), ("", "transport_block")},
    ),
    "library/admin.py": (set(), {("HourRefillPolicyForm", "Meta")}),
}
START_NEXT_HELP = (
    "A completed Start Next fade that cut a scheduled music song short and started the next item. Overlap, "
    "start-only, fade to silence, Manual and live-microphone (studio or Remote DJ) transitions are never causes; a "
    "connected Remote DJ with the mic off does not prevent one."
)
MANIFEST_PATH = "deploy/releases/r0149.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0149.md",
    "docs/releases/r0149_installation.md",
    "updatecenter/tests/test_r0149_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_WITH_MONITORING = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = CATCH_UP_WITH_MONITORING + ("isadoraair-active-message-worker",)
UNTOUCHED_BY_DIRECT_PLAN = (
    "isadoraair-monitoring", "isadoraair-encoders", "isadoraair-rbds", "isadoraair-active-message-worker",
    "isadoraair-spoken-render-worker", "isadoraair-render", "isadoraair-validation",
)
LAST = 149  # the release number of this contract


def _span(first: int) -> tuple[str, ...]:
    return tuple(f"r{n:04d}" for n in range(first, LAST + 1))


#: installed release -> (installed commit, releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0148": (R0148_RELEASE, _span(149), (), None, RESTARTS),
    "r0147": (R0147_RELEASE, _span(148), (), None, RESTARTS),
    "r0146": (R0146_RELEASE, _span(147), (), None, RESTARTS),
    "r0145": (R0145_RELEASE, _span(146), (), None, RESTARTS),
    "r0144": (R0144_RELEASE, _span(145), (), None, CATCH_UP_WITH_MONITORING),
    "r0143": (R0143_RELEASE, _span(144), (), None, CATCH_UP_WITH_MONITORING),
    "r0142": (R0142_RELEASE, _span(143), (), None, CATCH_UP_WITH_MONITORING),
    "r0141": (R0141_RELEASE, _span(142), (), None, CATCH_UP_WITH_MONITORING),
    "r0140": (R0140_RELEASE, _span(141), (), None, CATCH_UP_WITH_MONITORING),
    "r0139": (R0139_RELEASE, _span(140), (R0140_MIGRATION,), "additive", CATCH_UP_WITH_MONITORING),
    "r0138": (R0138_RELEASE, _span(139), (R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_WITH_MONITORING),
    "r0136": (R0136_RELEASE, _span(137), (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_WITH_MONITORING),
    "r0132": (R0132_RELEASE, _span(133), (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_WITH_MONITORING),
    "r0130": (R0130_RELEASE, _span(131),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_WITH_MONITORING),
    "r0124": (R0124_RELEASE, _span(125),
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
RUNBOOK = "docs/releases/r0149_installation.md"
NOTES = "docs/releases/r0149.md"


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
        raise AssertionError(f"r0149 manifest introduced more than once: {commits!r}")
    if not commits:
        # Fail closed. "Not committed yet" is true only while HEAD does not hold the manifest. Git reports no added
        # file for a merge commit, so a merge release commit must never read as "no R yet" and skip its own checks.
        if _git("ls-tree", "--name-only", "HEAD", "--", MANIFEST_PATH):
            raise AssertionError(
                "the r0149 manifest is committed at HEAD but no commit introduces it (a merge release commit?)")
        return None
    return commits[0]


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


def _members(source: str) -> dict:
    """Every top-level and class member of ``source`` as its AST without docstrings (comments are not in an AST)."""
    found = {}
    for node in _strip_docstrings(ast.parse(source)).body:
        if isinstance(node, ast.ClassDef):
            for index, member in enumerate(node.body):
                found[(node.name, getattr(member, "name", None) or f"#{ast.dump(member)}")] = ast.dump(member)
            found[(node.name, "<header>")] = ast.dump(ast.ClassDef(
                name=node.name, bases=node.bases, keywords=node.keywords, body=[ast.Pass()],
                decorator_list=node.decorator_list, type_params=[]))
        else:
            name = getattr(node, "name", None) or ",".join(
                getattr(target, "id", "") for target in getattr(node, "targets", []))
            found[("", name or f"#{ast.dump(node)}")] = ast.dump(node)
    return found


def _admin_help_texts(commit: str) -> dict:
    tree = ast.parse(_show(commit, "library/admin.py").decode())
    form = next(node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "HourRefillPolicyForm")
    meta = next(node for node in form.body if isinstance(node, ast.ClassDef) and node.name == "Meta")
    value = next(node.value for node in meta.body if isinstance(node, ast.Assign)
                 and [target.id for target in node.targets] == ["help_texts"])
    return ast.literal_eval(value)


def _constant(commit: str, path: str, name: str):
    tree = ast.parse(_show(commit, path).decode())
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and [getattr(target, "id", None) for target in node.targets] == [name]]
    if len(values) != 1:
        raise AssertionError(f"{name} is not assigned exactly once in {path}")
    return ast.literal_eval(values[0])


def _engine_state(**changes) -> dict:
    """A well-formed, fresh engine status snapshot with nothing live, as the safeguard reads it."""
    state = {
        "timestamp": time.time(), "engine_session_id": "contract-engine",
        "state_revision": {"instance": "contract-engine", "seq": 1}, "transport": "PLAYING",
        "mic_live": False, "remote_dj_configured": True, "remote_dj_connected": False, "remote_dj_live": False,
        "remote_dj_reconnecting": False, "manual_mode": False, "remote_dj_slot": "available",
        "remote_dj_connection": None,
    }
    state.update(changes)
    return state


class R0149ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_and_engine_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0149.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0149", "r0148", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0148 = json.loads(_show(R0148_RELEASE, "deploy/releases/r0148.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0148["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0148["schema_version"])
        self.assertEqual(data["minimum_updater_protocol_version"], r0148["minimum_updater_protocol_version"])
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
        self.assertEqual(data["services_requiring_restart"], ["isadoraair-gunicorn", "isadoraair-engine"])
        self.assertEqual(list(data), list(r0148))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Start Next eligibility (2.27)", "a connected Remote DJ with mic Off no longer excludes",
                         "Manual, a live or requested mic, unknown mic state, other causes and mixed sets stay excluded",
                         "Hour Refill policy is unchanged; nothing is armed or activated",
                         "Restart Gunicorn and the engine (interrupts program audio)",
                         "no migration, encoder, Monitoring, static, unit, package or protected-runtime change"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0149")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0149", "r0148"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_document_the_behavior_qualification_and_limitations(self):
        notes = _flat(_text(NOTES))
        for required in (
            "Independent Codex review: ACCEPT WITH NONBLOCKING OBSERVATIONS — READY FOR RELEASE QUALIFICATION",
            "no outstanding P0, P1 or P2 finding",
            "The accepted commits themselves; not cherry-picked, squashed or rewritten",
            "**r0149 does not activate Hour Refill.**", "**The inherited problem.**", "**The correction.**",
            "A connected Remote DJ who is only monitoring no longer excludes an otherwise eligible Start Next",
            "Studio Console and Remote DJ workspace Start Next commands keep the same permission checks",
            "**Actual Manual mode remains excluded.**",
            "**Actual local or remote microphone engagement remains excluded:**",
            "**A requested Remote DJ microphone remains excluded even while temporarily muted**",
            "**Emergency Mute cancels microphone intent.**", "**Unknown microphone state fails closed:**",
            "applies only to a nonempty set of qualified causes that are all Start Next",
            "A mixed set is never trimmed to its Start Next causes to escape the block",
            "restrictions for request substitution, Eject, automatic skip and media failure are intact",
            "## Eligibility is not a guarantee",
            "**does not guarantee a proposal, and does not guarantee an installation**",
            "needs a net shortage of **more than 30 s**", "the fitter", "the safety reserve",
            "the 20 s minimum insertion lead", "protected closing material", "media readiness", "separation",
            "the guarded install transaction", "engine adoption checks",
            "**5 s plus 1 s per inserted item**. One inserted track: **6 s**. Two inserted tracks: **7 s**. "
            "Two tracks plus one PSA: **8 s**",
            "an initial **14-day** shadow observation", "**cause-specific** supervised pilot qualification",
            "**no fixed 30-cause or ten-proposal prerequisite**", "**all available unique relevant proposals**",
            "isolated partial-hour regression qualification",
            "**separate operational qualification** before a station deliberately relies on partial-hour scheduling",
            "the corrected projection timing methodology", "**reported separately**",
            "**separate authorization** for host arming and for installation", "**seven days** of limited-install",
            "**at least one actual refill successfully observed airing**",
            "The original observation clock **does not restart**",
            "apply **only to eligible fresh-start segments**", "**excluded by default**",
            "**−0.410 s is a reconstructed estimate**", "**175.210 s dead-air interval** in that hour is independently",
            "**two available measurable Start Next cause-hours are supporting evidence, not statistical proof**",
            "**≤ 3 s** and a maximum of **≤ 5 s**", "r0149 adds no runtime timing field",
            "**399 of 399**", "**78 of 78**", "**321 of 321**",
            "**1,657 tests: 1,610 passed, 47 inherited errors, 0 failures, 0 skips**",
            "47 Active Messages `MediaRejected` errors", "**1.22**", "`spoken.tests.test_projection_deck_open`",
            "**1.22-D**", "**Two browser modules** were outside",
            "**No complete browser-to-browser WebRTC qualification is claimed.**",
            "## Unchanged behavior", "## Operational impact",
            "`migrations_required` is empty", "`migration_compatibility` is `null`", "`protected_runtime` is `null`",
            "Restart exactly: 1. `isadoraair-gunicorn` 2. `isadoraair-engine`",
            "**interrupts program audio**", "it is not a Gunicorn-only, non-disruptive release",
            "**engine restart safeguard**", "even with its microphone Off", "do not override it to install",
            "Publication does not authorize installation, and installation does not authorize Hour Refill activation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("3. `isadoraair-", "Restart Gunicorn only", "Restart the engine only",
                          "does not interrupt program audio", "without interrupting program audio",
                          "Hour Refill is now active", "statistically proven", "guarantees a refill"):
            self.assertNotIn(forbidden, notes)
        # The withdrawn 7/8/9 s wording appears only as the corrected earlier error.
        self.assertEqual(len(re.findall(r"7, 8 and 9 s", notes)), 1)

    def test_runbook_preserves_station_restart_and_activation_boundaries(self):
        runbook = _flat(_text(RUNBOOK))
        for required in (
            R0148_RELEASE, R0148_TREE, ENGINEERING, ENGINEERING_TREE,
            "Publication does not authorize deployment", "**The engine restart interrupts program audio.**",
            "**Installation is not Hour Refill activation.**", "There is **no migration**",
            "restarts `isadoraair-gunicorn` and then `isadoraair-engine`. Nothing else.",
            "stop and investigate", "Do not manually run `migrate`",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`",
            "Do not treat it as inaudible", "approved operator maintenance window", "**engine restart safeguard**",
            "even with its microphone Off", "**r0149 does not change the safeguard.**",
            "**Do not override the safeguard to install.**", "Below r0144", "return to Auto",
            "does not repeat the r0146 Gate A and Gate B", "`Deck mixer: audiomixer force-live=True`",
            "`Pipeline sample rate -> N`", "Gate A and Gate B apply again",
            "remain **mandatory** before installation", "KOGR's results never qualify WRJE",
            "## Common read-only preflight", "**full** action fingerprint", "`runtime_commit`",
            "no studio microphone is live; no Remote DJ is connected", "**each cause switch**",
            "that milestone is not evidence that anything was applied or restarted",
            "## Installation acceptance", "`Engine stopped.`", "`Restoring interrupted log_item`",
            "`Resume state rejected: ...`", "`FIRST-AUDIO DEADLINE`", "exactly one Play event per item",
            START_NEXT_HELP, "**Observation only (dry run) remains checked**", "both **host armed** rows still read *no*",
            "No new Hour Refill install record and no refill-origin log item exists",
            "## KOGR installation checklist", "**Verify it again on the station**",
            "**do not change them during installation.**", "## WRJE installation and acceptance",
            "WRJE's Hour Refill activation is qualified **independently**",
            "**A successful installation at one station is not authorization to install, arm or activate the other.**",
            "## Post-installation shadow verification of the newly permitted path",
            "**Run this only under its own operator authorization, after installation acceptance.**",
            "Auto mode.", "The studio microphone Off.", "the **remote microphone Off**",
            "**Observation only (dry run) checked** and the **host unarmed**", "**Exactly one durable cause**",
            "`blocked_transport` with reason `remote_dj`", "**No live installation occurs:**",
            "**Capture verified:**", "**Full proposal path verified:**",
            "A `below_threshold` or `already_satisfied` outcome is **not** a capture defect",
            "## Remaining pilot prerequisites", "**Installing r0149 does not restart the observation clock.**",
            "**Sign On**", "**New Music Friday**", "**ArloG**", "**Start Next-only pilot policy**",
            "**host arming**", "**clear Observation only**", "**first actual refill**", "**Seven days**",
            "**At least one actual refill successfully observed airing**",
            "Stop and preserve", "Do not use manual Git resets", "not rollback authority",
        ):
            self.assertIn(required, runbook)

    def test_documents_match_the_manifest_and_the_admin_help(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = _flat(_text(NOTES))
        runbook = _flat(_text(RUNBOOK))
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap, configuration, Hour Refill policy or protected-runtime operation is "
                      "expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0149 release commit R. 2. Restart "
                      "`isadoraair-gunicorn`. 3. Restart `isadoraair-engine`. No migration", runbook)
        release = _release_commit() or ENGINEERING
        self.assertEqual(_admin_help_texts(release)["capture_start_next"], START_NEXT_HELP)
        self.assertIn(f'reads: "{START_NEXT_HELP}"', runbook)


class R0149EngineRestartSafeguardTests(SimpleTestCase):
    """r0149 restarts the engine, so the existing safeguard gates it, unchanged."""

    def _assess(self, **changes):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine_state.json"
            path.write_text(json.dumps(_engine_state(**changes)), encoding="utf-8")
            return engine_restart_safety.assess_engine_restart(path=path)

    def test_the_safeguard_gates_this_plan(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        self.assertTrue(engine_restart_safety.plan_restarts_engine(
            SimpleNamespace(services_requiring_restart=data["services_requiring_restart"])))

    def test_safeguard_and_update_center_admission_code_are_byte_identical_to_r0148(self):
        release = _release_commit() or ENGINEERING
        for path in ("library/services/engine_restart_safety.py", "updatecenter/views.py",
                     "updatecenter/planner.py", "monitoring", "deploy/isadoraair-engine.service"):
            self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}:{path}"), _git("rev-parse", f"{release}:{path}"), path)
        views = _show(release, "updatecenter/views.py").decode()
        self.assertIn("plan_restarts_engine", views)
        self.assertIn("admit_engine_restart", views)

    def test_a_monitoring_remote_dj_still_blocks_the_engine_restart(self):
        """r0149 stops a mic-off Remote DJ excluding a Start Next from Hour Refill; it does not make an engine
        restart safe with that DJ connected."""
        clean = self._assess()
        self.assertEqual((clean.level, clean.reasons, clean.requires_override), ("safe", (), False))
        monitoring_dj = self._assess(
            remote_dj_connected=True, remote_dj_slot="occupied", remote_dj_connection={"status": "connected"})
        self.assertEqual((monitoring_dj.level, monitoring_dj.requires_override), ("blocked", True))
        self.assertIn(engine_restart_safety.REMOTE_DJ_CONNECTED, monitoring_dj.reasons)
        self.assertFalse(engine_restart_safety.decide_engine_restart(
            None, monitoring_dj, override_requested=False).allowed)
        for changes in ({"mic_live": True}, {"remote_dj_reconnecting": True, "remote_dj_slot": "occupied"},
                        {"timestamp": time.time() - 3600}):
            with self.subTest(changes=sorted(changes)):
                self.assertTrue(self._assess(**changes).requires_override)

    def test_engine_lifecycle_recovery_and_deck_pipeline_code_is_untouched(self):
        """Startup, shutdown, resume, first-real-audio recovery (r0147) and the deck mixer / bus (r0146) are the
        same code: the engine's executable change is one changed and one added Start Next evidence member."""
        release = _release_commit() or ENGINEERING
        before = _members(_show(R0148_RELEASE, "library/services/engine.py").decode())
        after = _members(_show(release, "library/services/engine.py").decode())
        added, changed = EXECUTABLE_CHANGE["library/services/engine.py"]
        self.assertEqual(set(before) - set(after), set())
        self.assertEqual(set(after) - set(before), added)
        self.assertEqual({key for key in set(before) & set(after) if before[key] != after[key]}, changed)
        for member in ("run", "stop", "_handle_signal_glib", "_request_orderly_shutdown", "_build_resume_state",
                       "_queue_resume_context", "_write_state", "_first_audio_arm", "_first_audio_deadline",
                       "_create_deck", "_start_next_track", "_any_mic_live"):
            key = ("PlaybackEngine", member)
            if key in before:
                self.assertEqual(before[key], after[key], member)
        self.assertIn(("PlaybackEngine", "stop"), before)
        self.assertIn(("PlaybackEngine", "_build_resume_state"), before)
        self.assertIn(("PlaybackEngine", "_first_audio_deadline"), before)


class R0149HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_is_the_exact_accepted_linear_history_on_r0148(self):
        """Published provenance only: runs in every clone, with nothing skipped."""
        self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}^{{tree}}"), R0148_TREE)
        commits = [commit for commit, _tree in ENGINEERING_CHAIN]
        self.assertEqual(_git("rev-list", "--reverse", f"{R0148_RELEASE}..{ENGINEERING}").split(), commits)
        parent = R0148_RELEASE
        for commit, tree in ENGINEERING_CHAIN:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            self.assertNotIn("cherry picked from commit", _git("log", "-1", "--format=%B", commit))
            parent = commit
        self.assertEqual(_git("rev-list", "--merges", f"{R0148_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_introducing("deploy/releases/r0148.json"), [R0148_RELEASE])

    def test_engineering_and_release_use_the_approved_public_identity(self):
        commits = [commit for commit, _tree in ENGINEERING_CHAIN]
        release = _release_commit()
        for commit in commits + ([release] if release else []):
            self.assertEqual(_git("log", "-1", "--format=%an <%ae>|%cn <%ce>", commit),
                             f"{APPROVED_IDENTITY}|{APPROVED_IDENTITY}", commit)

    def test_change_inventory_is_exact(self):
        self.assertEqual(len(CHANGE_FILES), 11)
        self.assertEqual(_changed(R0148_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0148_RELEASE, ENGINEERING, "--diff-filter=M"), CHANGE_FILES)
        self.assertEqual(_changed(R0148_RELEASE, ENGINEERING, "--diff-filter=ADRTC"), set())

    def test_release_is_one_packaging_only_child_and_preserves_the_accepted_implementation(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0148_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0148_RELEASE}..{release}").split(),
                         [commit for commit, _tree in ENGINEERING_CHAIN] + [release])
        self.assertEqual(_changed(R0148_RELEASE, release), CHANGE_FILES | RELEASE_FILES)
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_the_review_remediation_changed_no_executable_code(self):
        self.assertEqual(_changed(REMEDIATION_PARENT, ENGINEERING), {
            "docs/roadmap/2_27_PHASE_D_ROLLOUT_PACKET.md",
            "docs/roadmap/2_27_START_NEXT_CAUSALITY_EXTENSION.md",
            "library/services/engine.py",
        })
        before = _show(REMEDIATION_PARENT, "library/services/engine.py").decode()
        after = _show(ENGINEERING, "library/services/engine.py").decode()
        self.assertNotEqual(before, after)
        self.assertEqual(_code_dump(before), _code_dump(after))

    def test_the_executable_change_is_exactly_the_accepted_members(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(set(EXECUTABLE_CHANGE), PRODUCT_FILES)
        for path, (added, changed) in EXECUTABLE_CHANGE.items():
            with self.subTest(path=path):
                before = _members(_show(R0148_RELEASE, path).decode())
                after = _members(_show(release, path).decode())
                self.assertEqual(set(before) - set(after), set())
                self.assertEqual(set(after) - set(before), added)
                self.assertEqual({key for key in set(before) & set(after) if before[key] != after[key]}, changed)

    def test_the_admin_change_is_only_the_start_next_help_text(self):
        release = _release_commit() or ENGINEERING
        before, after = _admin_help_texts(R0148_RELEASE), _admin_help_texts(release)
        self.assertEqual(set(before), set(after))
        self.assertEqual({key for key in before if before[key] != after[key]}, {"capture_start_next"})
        self.assertEqual(after["capture_start_next"], START_NEXT_HELP)
        old = _show(R0148_RELEASE, "library/admin.py").decode()
        new = _show(release, "library/admin.py").decode()

        class _DropHelp(ast.NodeTransformer):
            def visit_Assign(self, node):
                if [getattr(target, "id", None) for target in node.targets] == ["help_texts"]:
                    return ast.Pass()
                return node

        self.assertEqual(ast.dump(_DropHelp().visit(ast.parse(old))), ast.dump(_DropHelp().visit(ast.parse(new))))

    def test_thresholds_reserve_and_insertion_lead_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        install = "library/services/hour_refill_install.py"
        self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}:{install}"), _git("rev-parse", f"{release}:{install}"))
        base = _constant(release, install, "SAFETY_BUDGET_BASE_SECONDS")
        per_item = _constant(release, install, "SAFETY_BUDGET_PER_ITEM_SECONDS")
        self.assertEqual([base + per_item * items for items in (1, 2, 3)], [6.0, 7.0, 8.0])
        self.assertEqual(_constant(release, install, "INSERTION_LEAD_SECONDS"), 20.0)
        self.assertEqual(_constant(release, "library/services/hour_refill_projection.py",
                                   "START_NEXT_MUSIC_TRIGGER_THRESHOLD_SECONDS"), 30)

    def test_release_preserves_the_earlier_release_contracts_unchanged(self):
        """The r0146 public-clone contract correction and every earlier contract are byte-for-byte untouched."""
        release = _release_commit() or ENGINEERING
        existing = [path for path in _git("ls-tree", "-r", "--name-only", R0148_RELEASE, "updatecenter/tests/").split()
                    if re.match(r"updatecenter/tests/test_r\d{4}_release\.py$", path)]
        for required in ("test_r0145_release.py", "test_r0146_release.py", "test_r0147_release.py",
                         "test_r0148_release.py"):
            self.assertIn(f"updatecenter/tests/{required}", existing)
        for path in existing:
            self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}:{path}"), _git("rev-parse", f"{release}:{path}"), path)
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "updatecenter"),
                         {"updatecenter/tests/test_r0149_release.py"} if _release_commit() else set())

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0148_RELEASE, release)
                          if "/static/" in path or "/templates/" in path or path.startswith("templates/")])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        self.assertEqual({path for path in _changed(R0148_RELEASE, release) if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        for prefix in ("aircheck", "encoders", "monitoring", "rbds", "hardware", "production", "ogremote",
                       "webrequests", "spoken", "active_messages", "produced_content", "isadoraair",
                       "library/migrations", "library/models.py", "library/views.py", "library/urls.py"):
            self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}:{prefix}"), _git("rev-parse", f"{release}:{prefix}"),
                             prefix)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0149.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0148_RELEASE,
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
                mutated = manifest_mod.validate_manifest_dict({**data, **change}, source_label="r0149.json")
                findings = cross_check.cross_check_release(mutated, release, PROJECT_ROOT, app_paths, R0148_RELEASE)
                self.assertTrue(list(findings), label)

    def test_production_change_is_exactly_the_declared_modules(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0148_RELEASE, release)
        modules = {path[:-3].replace("/", ".") for path in changed if path.endswith(".py") and "/tests/" not in path}
        self.assertEqual(modules, PRODUCTION_MODULES)
        self.assertEqual({path for path in changed if not path.endswith(".py") and not path.startswith("docs/")
                          and path != MANIFEST_PATH}, set())

    def test_declared_restarts_are_exactly_the_services_that_load_the_changed_modules(self):
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        for module in PRODUCTION_MODULES:
            self.assertIsNotNone(graph.path(module), module)
        reach = {service: graph.service_reach(service) for service in LONG_RUNNING_ENTRIES}
        loading = {service for service, modules in reach.items() if modules & PRODUCTION_MODULES}
        self.assertEqual(loading, set(RESTARTS))
        refill = {"library.services.hour_refill_causality", "library.services.hour_refill_projection"}
        self.assertLessEqual(refill | {"library.services.engine"}, reach["isadoraair-engine"])
        self.assertIn("library.admin", reach["isadoraair-gunicorn"])
        for service in UNTOUCHED_BY_DIRECT_PLAN:
            self.assertEqual(reach[service] & PRODUCTION_MODULES, set(), service)


class R0149MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0149.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0149", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0149"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0149ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0149_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0148_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 148)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0148_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0148_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0149_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, LAST + 1)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0144"], commits["r0145"], commits["r0146"], commits["r0147"],
                          commits["r0148"], commits["r0149"]),
                         (R0132_RELEASE, R0144_RELEASE, R0145_RELEASE, R0146_RELEASE, R0147_RELEASE, R0148_RELEASE,
                          release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0148 -> r0149", out.getvalue())


class R0149TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0149", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0149", release))
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

    def test_direct_plan_is_migration_free_gunicorn_then_engine_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0148", R0148_RELEASE), ("r0149", release)])
        self._assert_plan("r0148")

    def test_direct_plan_never_touches_other_services(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0148_RELEASE, "r0149", known_units=_known_units())
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        for untouched in UNTOUCHED_BY_DIRECT_PLAN:
            self.assertNotIn(untouched, plan.services_requiring_restart)

    def test_catch_up_from_r0147(self):
        self._assert_plan("r0147")

    def test_catch_up_from_r0146(self):
        self._assert_plan("r0146")

    def test_catch_up_from_r0145(self):
        self._assert_plan("r0145")

    def test_catch_up_from_r0144_keeps_the_r0145_monitoring_restart(self):
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
            derive_plan(_repository(), release, "0" * 40, "r0149", known_units=_known_units())
        with self.assertRaises(Exception):
            # r0148's own commit presented as r0147 is not the commit the chain records for r0147.
            derive_plan(_repository(), release, R0148_RELEASE, "r0147", known_units=_known_units())
        with self.assertRaises(Exception):
            # The accepted engineering tip is not an installable release.
            derive_plan(_repository(), release, ENGINEERING, "r0149", known_units=_known_units())

    def test_restart_rehearsal_touches_gunicorn_then_engine_only(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[2] for call in runner.calls if call[1] == "restart"],
                         ["isadoraair-gunicorn.service", "isadoraair-engine.service"])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in UNTOUCHED_BY_DIRECT_PLAN + ("isadoraair-aircheck",):
            self.assertNotIn(untouched, touched)
        with self.assertRaises(SystemdError):
            manager.restart_declared(tuple(reversed(RESTARTS)))


class R0149ReachabilityTests(SimpleTestCase):
    def test_every_published_object_is_reachable_from_r_alone(self):
        """A bare repository holding only R's history (what a fresh clone of R gets) has every object this contract
        relies on: r0148, each accepted commit with its tree, R and every release file."""
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            for commit, tree in ENGINEERING_CHAIN:
                self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}", cwd=bare), tree)
            self.assertEqual(_git("rev-parse", f"{R0148_RELEASE}^{{tree}}", cwd=bare), R0148_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            isolated = derive_plan(_repository(bare), release, R0148_RELEASE, "r0149", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0148_RELEASE, "r0149", known_units=_known_units())
            self.assertEqual(isolated.fingerprint, shared.fingerprint)
            self.assertEqual(isolated.services_requiring_restart, RESTARTS)


class _Clone:
    """A disposable station checkout under a temporary directory; no configuration identity is written."""

    def __init__(self, root: Path, release: str):
        self.origin = root / "origin.git"
        self.work = root / "station"
        subprocess.run(["git", "clone", "--quiet", "--shared", "--bare", str(PROJECT_ROOT), str(self.origin)],
                       check=True, capture_output=True)
        _git("update-ref", "refs/heads/main", release, cwd=self.origin)
        subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(self.origin), str(self.work)],
                       check=True, capture_output=True)

    def git(self, *args: str) -> str:
        return _git(*args, cwd=self.work)

    def at(self, commit: str) -> None:
        self.git("checkout", "-q", "-f", "-B", "main", commit)
        self.git("branch", "-q", "--set-upstream-to=origin/main", "main")
        self.git("clean", "-q", "-fdx")


class R0149PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0149",
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
            self.assertTrue(engine_restart_safety.plan_restarts_engine(application))
            self.assertEqual(application.fingerprint, protected.fingerprint)

    def test_direct_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity("r0148")

    def test_catch_up_from_r0147_application_and_protected_planners_match(self):
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
