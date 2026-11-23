"""r0146 immutable release contract (deck mixer continuity, roadmap 1.21 A/A2; r0145 public-clone contract correction).

Published r0145 (d6679c8) plus 7 linear engineering commits, each cherry-picked with -x from an independently accepted
candidate (equal patch IDs), then R:
  ad5cab2            -- B: r0145 release-contract portability (test only; from 8746a87); reproduces the accepted tree
                        123b1283 exactly;
  4f5deb2 .. 9b4728f -- A: deck mixer continuity (6 commits from f2e5dad .. 0dd27a3): force-live deck audiomixer and the
                        fixed S16LE deck bus, with their tests;
  R                  -- metadata-only child of 9b4728f adding this contract, the r0146 manifest and two release
                        documents.
Every changed file in the engineering tree is byte-identical to its accepted source tip.

What the published history does and does not contain (the r0145 lesson, applied from the start)
--------------------------------------------------------------------------------------------------
1. Present in every clone of R: r0145, the 7 integrated commits with their trees, and R. Each integrated commit names
   its accepted source in its ``(cherry picked from commit ...)`` trailer.
2. NOT present in the published ancestry: the original accepted source commits (ENGINEERING_CHAIN's third column and
   ACCEPTED_*); they exist only in the engineering repository.
3. Comparisons that need those objects (patch-ID equality, accepted source trees/ancestry, source-tip byte equality)
   run only when every source commit they use exists; when one is genuinely absent the test is SKIPPED naming it. A
   present but mismatching object fails; a malformed name, a non-commit object or any other Git failure raises.
4. Everything provable from the published history is asserted unconditionally (linear chain, parents, trees,
   trailers, non-empty commits, the B tree reproduced at its boundary, the 1 + 5 file sets from the integration
   boundaries, R as the metadata-only child), and the release chain, manifest, migration, restart, trusted-updater plan
   and planner-parity contracts are mandatory in every environment.

The installation runbook's GStreamer preflight is a mandatory two-gate installation requirement (Gate A: construct-time
force-live deck mixer; Gate B: the live mixer's actual fixed-format source caps at the station sample rate, from a
standalone probe), and the release notes' limitations are an explicit contract; R0146PreflightContractTests asserts both
semantically, and EXECUTES the runbook's Gate B evaluator against synthetic probe output so each pass criterion is
proven to be enforced. The embedded probe is pinned byte for byte to the accepted engineering gate.

r0146 declares no migration and restarts the engine only: the one changed production module is library.services.engine,
and its changed code (make_deck_mixer, make_deck_bus_caps, DECK_BUS_FORMAT, _build_main_pipeline) runs only when the
engine service's PlaybackEngine.start() builds its pipeline. Gunicorn imports that module (as in every release) but never
constructs or runs a PlaybackEngine, so no other long-running service executes the changed code.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import subprocess
import sys
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
R0145_TREE = "9e8bc5ff2581e34ed3a4b47ecfa99033953d9a94"
#: (commit, tree, accepted source commit it was cherry-picked from) in first-parent order on r0145.
ENGINEERING_CHAIN = (
    ("ad5cab283edd303f1717d7d215fe968e7c6c2618", "123b12834431ee45bb490ca58433a85f6ee027a2",
     "8746a87bb3cd363d3161febbee95bbf58630ea4b"),
    ("4f5deb28f116cfa1e6f67f73c87383c35090cbfe", "0a78c26b1ad9135f03aba08f538bc853daa3d89e",
     "f2e5dade7539b00484bf1d12bef9904b461806ba"),
    ("8dfb6ccbdcee43c7d7992d4b4c35e867842bfdde", "400e0c148d37f1e37616949d73178e5a15f3690c",
     "c8c8c92b316e1232d0a478c1288cfc2148511ba2"),
    ("101e86567fc8e4a55ce5d8bc11711fce402d15c4", "8752d028bd8eac25e7b7addcae14071a3159f820",
     "1d6f0aadecf4c3cf7d47ba55fc6c433599aa8a63"),
    ("c8b01be033f98cf5cf6af3db3a9b3529f7be9553", "cd27dec50ffa8296df2462dcc1f0c286c49f6f81",
     "04fc7926f37ed3d3fc2ad46bb03ea137f5d1b618"),
    ("b051026cfceda906853fb52db82b7f261804999f", "33f5ce620c4fdf7e50d29da02a69eb2b971d047d",
     "2d0b23fc3921db184e8428074e53f0d7e7b5f5db"),
    ("9b4728fd004959da478839d0c1ca96bd67dd6340", "ba50e0a85ac047a3bb4727022b6355db954604bd",
     "0dd27a3dcc2c6dea0124cc029531e61b834a72a0"),
)
#: Accepted source commits (commit, tree). NOT in the published ancestry (see the module docstring).
ACCEPTED_B = ("8746a87bb3cd363d3161febbee95bbf58630ea4b", "123b12834431ee45bb490ca58433a85f6ee027a2")
ACCEPTED_A2_PRODUCT = ("1d6f0aadecf4c3cf7d47ba55fc6c433599aa8a63", "df1d10906d3226f058b5cd18506c2cc56a13f9f1")
ACCEPTED_A = ("0dd27a3dcc2c6dea0124cc029531e61b834a72a0", "b4e22cc1e2fbda3d37c5ada9a9785583d5028d74")
SOURCE_COMMITS = tuple(origin for _commit, _tree, origin in ENGINEERING_CHAIN)
B_INTEGRATED = ENGINEERING_CHAIN[0][0]
ENGINEERING = ENGINEERING_CHAIN[-1][0]
ENGINEERING_TREE = ENGINEERING_CHAIN[-1][1]
B_FILES = {"updatecenter/tests/test_r0145_release.py"}
A_FILES = {
    "library/services/engine.py",
    "library/tests/_transport_harness.py",
    "library/tests/test_deck_mixer_continuity_1_21.py",
    "library/tests/test_engine_deck_lifecycle.py",
    "library/tests/test_vt_b0_timing_feasibility.py",
}
CHANGE_FILES = A_FILES | B_FILES
ADDED_FILES = {"library/tests/test_deck_mixer_continuity_1_21.py"}
MODIFIED_FILES = CHANGE_FILES - ADDED_FILES
PRODUCTION_MODULES = {"library.services.engine"}
#: The changed engine code; referenced by no other production module.
CHANGED_ENGINE_SYMBOLS = ("make_deck_mixer", "make_deck_bus_caps", "DECK_BUS_FORMAT", "_build_main_pipeline")
MANIFEST_PATH = "deploy/releases/r0146.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0146.md",
    "docs/releases/r0146_installation.md",
    "updatecenter/tests/test_r0146_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-engine",)
CATCH_UP_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = CATCH_UP_RESTARTS + ("isadoraair-active-message-worker",)
#: installed release -> (installed commit, releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0145": (R0145_RELEASE, ("r0146",), (), None, RESTARTS),
    "r0144": (R0144_RELEASE, ("r0145", "r0146"), (), None, CATCH_UP_RESTARTS),
    "r0143": (R0143_RELEASE, ("r0144", "r0145", "r0146"), (), None, CATCH_UP_RESTARTS),
    "r0142": (R0142_RELEASE, tuple(f"r{n:04d}" for n in range(143, 147)), (), None, CATCH_UP_RESTARTS),
    "r0141": (R0141_RELEASE, tuple(f"r{n:04d}" for n in range(142, 147)), (), None, CATCH_UP_RESTARTS),
    "r0140": (R0140_RELEASE, tuple(f"r{n:04d}" for n in range(141, 147)), (), None, CATCH_UP_RESTARTS),
    "r0139": (R0139_RELEASE, tuple(f"r{n:04d}" for n in range(140, 147)), (R0140_MIGRATION,), "additive",
              CATCH_UP_RESTARTS),
    "r0138": (R0138_RELEASE, tuple(f"r{n:04d}" for n in range(139, 147)), (R0139_MIGRATION, R0140_MIGRATION),
              "additive", CATCH_UP_RESTARTS),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, 147)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_RESTARTS),
    "r0132": (R0132_RELEASE, tuple(f"r{n:04d}" for n in range(133, 147)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_RESTARTS),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, 147)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_RESTARTS),
    "r0124": (R0124_RELEASE, tuple(f"r{n:04d}" for n in range(125, 147)),
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


def _source_commit_exists(commit: str) -> bool:
    """True when this exact commit exists in the repository under test, False when it is genuinely absent.

    ``git cat-file -e <40-hex>`` exits 0 for an existing object and 1, silently, for an absent one. Nothing else is
    "absent": a malformed name or any other failure (repository corruption, permissions, a missing Git) is raised, and
    so is an existing object that is not a commit, so none of them can pass as an ordinary skip."""
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError(f"not a full lowercase commit SHA: {commit!r}")
    result = subprocess.run(["git", "-C", str(PROJECT_ROOT), "cat-file", "-e", commit],
                            capture_output=True, text=True)
    if result.returncode == 0:
        kind = _git("cat-file", "-t", commit)
        if kind != "commit":
            raise AssertionError(f"{commit} exists but is a {kind}, not a commit")
        return True
    if result.returncode == 1 and not result.stderr.strip():
        return False
    raise RuntimeError(f"git cat-file -e {commit} failed unexpectedly (exit {result.returncode}): "
                       f"{result.stderr.strip()!r}")


def _require_sources(test, *commits: str) -> None:
    """Skip ``test`` only when a needed original accepted source commit is genuinely absent (a public clone)."""
    missing = [commit for commit in dict.fromkeys(commits) if not _source_commit_exists(commit)]
    if missing:
        shown = ", ".join(missing[:5]) + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
        test.skipTest(
            f"{len(missing)} of {len(set(commits))} original accepted source commit(s) this comparison needs are "
            f"absent from this repository (unpublished engineering objects; see the module docstring): {shown}"
        )


def _introducing(path: str = MANIFEST_PATH) -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--", path,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing()
    if len(commits) > 1:
        raise AssertionError(f"r0146 manifest introduced more than once: {commits!r}")
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




class R0146ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_engine_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0146.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0146", "r0145", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0145 = json.loads(_show(R0145_RELEASE, "deploy/releases/r0145.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0145["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0145["schema_version"])
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
        self.assertEqual(list(data), list(r0145))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Deck mixer continuity", "force-live", "fixed S16LE deck bus", "zero-deck idle",
                         "catch-up of about idle/4.5", "r0145 release contract", "public clone",
                         "Restart the engine only", "no migration", "protected-runtime change"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0146")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0146", "r0145"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_the_r0144_engine_restart_safeguard_gates_this_installation(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        self.assertTrue(engine_restart_safety.plan_restarts_engine(
            SimpleNamespace(services_requiring_restart=data["services_requiring_restart"])))

    def test_release_notes_document_changes_reviews_and_limitations(self):
        notes = " ".join(_artifact("docs/releases/r0146.md").decode().split())
        for required in (
            "Codex ACCEPT — Phase A2 ready for final regression gate", "1,015 of 1,015",
            "Codex ACCEPT — test-only correction approved for future release",
            "every patch is identical to its accepted source", "byte-identical to its accepted source tip",
            "**The inherited defect.**", "~4.5–5.5x real time", "roughly idle/4.5", "`force-live=true`",
            "can be set only at construction",
            "`audio/x-raw,format=S16LE,rate=<station rate>,channels=2,layout=interleaved`",
            "renegotiated to the first deck's S16LE", "negotiates once, from startup", "within 0.022 s of the clock",
            "exactly one PlayEvent is recorded per start", "`Deck mixer: audiomixer force-live=True`",
            "**That fallback is the old defective behaviour, not the correction.**",
            "38 pass where the source objects exist; 35 pass and 3 skip in a public clone",
            "KOGR's GStreamer 1.28.2 passed the runtime preflight",
            "**Claimed-but-unaired recovery** remains roadmap 1.21-B",
            "**Durable crash / power-outage checkpoint recovery** remains roadmap 1.21-C",
            "**Ordinary outgoing-tail truncation (F14b)**", "**Decoder starvation.**",
            "**The non-live fallback remains defective**", "**The deck bus is 16-bit.**",
            "was **not** identified", "no GStreamer upgrade, no Hour Refill policy change",
            "`migrations_required` is empty", "`migration_compatibility` is `null`", "`protected_runtime` is `null`",
            "1. `isadoraair-engine`", "**interrupts program audio**", "**engine restart safeguard**",
            "Do not override it to install", "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("2. `isadoraair-", "Restart Gunicorn", "root cause was identified", "fully passing"):
            self.assertNotIn(forbidden, notes)

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = " ".join(_artifact("docs/releases/r0146_installation.md").decode().split())
        for required in (
            R0145_RELEASE, ENGINEERING, ENGINEERING_TREE,
            "Publication does not authorize deployment", "separate station-specific", "There is **no migration**",
            "Nothing else.", "no Gunicorn, Monitoring, encoder, RBDS or worker restart", "stop and investigate",
            "a station on r0144 also restarts Gunicorn and Monitoring for r0145",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`", "Do not manually run `migrate`",
            "**approved operator maintenance window**", "**Do not override the safeguard to install.**",
            "predates the safeguard and will not check",
            "## GStreamer runtime preflight (mandatory, read-only, off-air)", "`GATE A PASS`", "`GATE B PASS`",
            "**KOGR's result does not qualify WRJE.**", "**Do not assume the station is on r0145.**",
            "WRJE was last independently documented on r0132",
            "Outstanding acceptance", "action fingerprint", "Do **not** key a microphone on air",
            "`Deck mixer: audiomixer force-live=True`", "the defective fallback is in use",
            "at least two ordinary song-to-song segues", "Do not create an idle for this check",
            "## Post-install Monitoring observation", "## KOGR acceptance", "## WRJE acceptance",
            "Do not reuse KOGR's results",
            "**A successful installation at one station is not authorization to install at the other.**",
            "Stop and preserve", "Do not use manual Git resets", "not rollback authority",
        ):
            self.assertIn(required, runbook)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = " ".join(_artifact("docs/releases/r0146.md").decode().split())
        runbook = " ".join(_artifact("docs/releases/r0146_installation.md").decode().split())
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0146 release commit R. 2. Restart "
                      "`isadoraair-engine`. No migration", runbook)
        self.assertIn("A direct r0145 → r0146 plan advances source to R, then restarts `isadoraair-engine`. "
                      "Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


GATE_B_PROBE_SHA256 = "f7c97fa6c574a6517a68cd50de3adf7bc8162ec80229add1ceb16f57e23364cd"
"""sha256 of the accepted engineering standalone caps gate (gst_a2_caps_gate.py); the runbook embeds it verbatim."""
RUNBOOK = "docs/releases/r0146_installation.md"
NOTES = "docs/releases/r0146.md"
PREFLIGHT_HEADING = "## GStreamer runtime preflight (mandatory, read-only, off-air)"


def _text(path: str) -> str:
    return _artifact(path).decode()


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(markdown: str, heading: str) -> str:
    start = markdown.index(heading)
    following = re.search(r"^## ", markdown[start + len(heading):], re.M)
    return markdown[start: start + len(heading) + following.start()] if following else markdown[start:]


def _heredoc(markdown: str, tag: str) -> str:
    """The body of the runbook's ``<<'TAG'`` heredoc (exactly one opener and one terminator, nothing else)."""
    assert len(re.findall(rf"<<'{tag}'$", markdown, re.M)) == 1, tag
    assert len(re.findall(rf"^{tag}$", markdown, re.M)) == 1, tag
    match = re.search(rf"<<'{tag}'\n(.*?)^{tag}$", markdown, re.M | re.S)
    assert match, tag
    return match.group(1)


def _synthetic_probe_output(rate: int) -> list[str]:
    """What the accepted gate prints on a healthy station (structure taken from a real 44100 Hz run)."""
    want = f"S16LE,{rate},2,interleaved"
    labels = (("zero_pads_startup", 0), ("deck1_S16LE", 1), ("zero_pads_after_removal", 0), ("deck2_F32LE", 1),
              ("deck2_F32LE+deck3_S32LE", 2), ("deck3_only", 1), ("zero_pads_again", 0), ("deck4_S16LE", 1),
              ("zero_end", 0))
    checkpoints = [{"label": label, "running_s": 2.0 + 2 * n, "src_caps": want, "deck_pads": decks,
                    "mixer_buffers": 200 * (n + 1), "mixer_lag_s": 0.004 if decks == 0 else -0.01}
                   for n, (label, decks) in enumerate(labels)]
    result = {"rate": rate, "deck_bus_capsfilter": True, "force_live": True, "caps_events": [want],
              "distinct_src_formats": ["S16LE"], "segments": 1, "latency_events": [53.22], "discont_buffers": 0,
              "zero_phase_buffers": 895, "zero_phase_max_abs": 0.0, "bus_messages": [], "checkpoints": checkpoints}
    events = [{"phase": "zero_startup", "t": 0.01, "event": "CAPS", "caps": want},
              {"phase": "zero_startup", "event": "SEGMENT", "start": 0, "base": 0}]
    return ["A2CAPS " + json.dumps(result), "A2EVENTS " + json.dumps(events)]


def _run_evaluator(evaluator: str, lines: list[str], *, rate: int, probe_rc: int = 0):
    return subprocess.run([sys.executable, "-I", "-c", evaluator], input="\n".join(lines) + "\n", text=True,
                          capture_output=True, env={"RATE": str(rate), "PROBE_RC": str(probe_rc)}, check=False)


class R0146PreflightContractTests(SimpleTestCase):
    """The mandatory two-gate GStreamer preflight and the complete limitations are part of the release contract."""

    def test_runbook_requires_both_gstreamer_gates_before_any_installation(self):
        runbook = _text(RUNBOOK)
        self.assertIn(PREFLIGHT_HEADING, runbook)
        section = _flat(_section(runbook, PREFLIGHT_HEADING))
        # Both gates are mandatory; nothing about the preflight is optional.
        self.assertNotRegex(section, r"(?i)\boptional(ly)?\b|\bmay (skip|omit)\b|\bif (desired|convenient)\b")
        for required in (
            "Both gates below are mandatory on every station before r0146 is installed",
            "must not install r0146 if either gate fails, is skipped or cannot be completed safely",
            "passing Gate A never substitutes for Gate B",
            "not an UpdateCenter action",
            # Gate A: construct-time force-live.
            "Gate A: construct-time capability", "Gst.ElementFactory.make_with_properties", "`force-live=true`",
            "must report `force-live` as `True`", "can be set only at construction",
            "Any fallback to the inherited non-live mixer", "is a **blocking failure**",
            # Gate B: the actual standalone mixer-source caps probe at the station rate.
            "Gate B: actual fixed-format mixer output", "A standalone probe at the **station's configured sample rate**",
            "`audio/x-raw,format=S16LE,rate=<station rate>,channels=2,layout=interleaved`",
            "negotiated caps on the mixer source pad", "not from the capsfilter's configured property",
            "exactly one consistent mixer-source CAPS event", "begins during zero-pad (no deck) startup",
            "no unexpected output-format renegotiation", "no DISCONT buffer", "no GStreamer bus error or warning",
            "clock-driven", "digital silence", "fails closed on an unsupported property, a missing plugin, unexpected caps",
            # Failure or inability blocks installation.
            "or could not be run to completion", "the station **must not** install r0146 until the cause is understood",
            "anything else blocks installation",
            # WRJE is independent, specific, and not a publication blocker.
            "**KOGR's result does not qualify WRJE.**",
            "both gates are required on WRJE, independently of KOGR, at WRJE's own configured sample rate",
            "with WRJE's own installed GStreamer", "pending preflight blocks a WRJE installation",
            "does not by itself prevent publication",
        ):
            self.assertIn(required, section)
        # The gates sit before the installation, in the common preflight and in the stop conditions.
        flat = _flat(runbook)
        self.assertIn("**GStreamer gates.** Gate A and Gate B above have both passed on this station", flat)
        self.assertIn("Only after Gate A and Gate B have both passed on that station", flat)
        self.assertIn("Gate A or Gate B did not pass or could not be completed before installation (do not install)", flat)
        self.assertIn("**both GStreamer gates (A and B) must pass on WRJE itself, at WRJE's own configured sample rate", flat)
        self.assertLess(runbook.index(PREFLIGHT_HEADING), runbook.index("## Installation expectations"))

    def test_gate_a_probe_constructs_the_live_deck_mixer_like_the_engine_and_fails_closed(self):
        runbook = _text(RUNBOOK)
        probe = _heredoc(runbook, "GATE_A_PROBE")
        ast.parse(probe)
        for required in ('make_with_properties', '"audiomixer", ["name", "force-live"], ["mixer", True]',
                         'mixer.get_property("force-live")', "live is not True", "sys.exit(", "GATE A FAIL", "GATE A PASS",
                         "mixer is None", "Exception"):
            self.assertIn(required, probe)
        self.assertEqual(probe.count("GATE A PASS"), 1)
        # Same construction as the engine's make_deck_mixer in the integrated tree.
        engine = _show(ENGINEERING, "library/services/engine.py").decode()
        self.assertIn('make("audiomixer", ["name", "force-live"], [name, True])', engine)
        self.assertIn("mixer.get_property(\"force-live\") is True", engine)
        self.assertIn('"$PY" -I - <<\'GATE_A_PROBE\'', runbook)
        self.assertIn("Gate A exit status: $? (0 = PASS; anything else blocks installation)", runbook)

    def test_gate_b_probe_is_the_accepted_standalone_gate_run_at_the_station_rate_off_air(self):
        runbook = _text(RUNBOOK)
        probe = _heredoc(runbook, "GATE_B_PROBE")
        self.assertEqual(hashlib.sha256(probe.encode()).hexdigest(), GATE_B_PROBE_SHA256)
        ast.parse(probe)
        # Invocation: the station interpreter, isolated mode, standard input, the station rate, the capsfilter ON, bounded.
        self.assertIn("""OUT=$(timeout 120 "$PY" -I - "$RATE" 1 <<'GATE_B_PROBE'""", runbook)
        self.assertIn('RATE="$RATE" PROBE_RC="$PROBE_RC" "$PY" -I -c "$EVALUATOR"', runbook)
        self.assertIn("Gate B exit status: $? (0 = PASS; anything else blocks installation)", runbook)
        self.assertIn('case "$RATE" in', runbook)
        # It measures the NEGOTIATED mixer-source caps, and is isolated: an appsink, no device, no Django, no file.
        for required in ('mixer.get_static_pad("src")', "src.get_current_caps()", "Gst.EventType.CAPS",
                         "Gst.BufferFlags.DISCONT", "appsink"):
            self.assertIn(required, probe)
        for forbidden in ("alsasink", "autoaudiosink", "pulsesink", "jackaudiosink", "django", "open(", "socket",
                          "subprocess", "os.system"):
            self.assertNotIn(forbidden, probe)
        # The probe's caps are the engine's own deck-bus format.
        engine = _show(ENGINEERING, "library/services/engine.py").decode()
        self.assertIn('DECK_BUS_FORMAT = "S16LE"', engine)
        self.assertIn("channels=2,layout=interleaved", engine)
        self.assertIn("format=S16LE,rate={RATE},channels=2,layout=interleaved", probe)

    def test_gate_b_evaluator_enforces_every_pass_criterion(self):
        runbook = _text(RUNBOOK)
        evaluator = _heredoc(runbook, "GATE_B_EVALUATOR")
        ast.parse(evaluator)
        for rate in (44100, 48000):
            ok = _run_evaluator(evaluator, _synthetic_probe_output(rate), rate=rate)
            self.assertEqual((ok.returncode, ok.stdout.startswith("GATE B PASS")), (0, True), (rate, ok.stdout))

        def mutate(change):
            lines = _synthetic_probe_output(44100)
            result, events = json.loads(lines[0][7:]), json.loads(lines[1][9:])
            change(result, events)
            return ["A2CAPS " + json.dumps(result), "A2EVENTS " + json.dumps(events)]

        def checkpoint(result, label):
            return next(c for c in result["checkpoints"] if c["label"] == label)

        want = "S16LE,44100,2,interleaved"
        failing = {
            "extra CAPS event (renegotiation)": mutate(lambda r, e: r.update(caps_events=[want, want])),
            "S32LE negotiated": mutate(lambda r, e: r.update(caps_events=["S32LE,44100,2,interleaved"],
                                                           distinct_src_formats=["S32LE"])),
            "wrong rate negotiated": mutate(lambda r, e: r.update(caps_events=["S16LE,48000,2,interleaved"])),
            "mono negotiated": mutate(lambda r, e: r.update(caps_events=["S16LE,44100,1,interleaved"])),
            "non-interleaved": mutate(lambda r, e: r.update(caps_events=["S16LE,44100,2,non-interleaved"])),
            "second format seen": mutate(lambda r, e: r.update(distinct_src_formats=["S16LE", "F32LE"])),
            "CAPS begins during a deck phase": mutate(lambda r, e: e[0].update(phase="deck1_S16")),
            "DISCONT buffer": mutate(lambda r, e: r.update(discont_buffers=1)),
            "bus error": mutate(lambda r, e: r.update(bus_messages=["ERROR boom"])),
            "bus warning": mutate(lambda r, e: r.update(bus_messages=["WARNING late"])),
            "zero-pad output is not silence": mutate(lambda r, e: r.update(zero_phase_max_abs=7)),
            "zero-pad output barely observed": mutate(lambda r, e: r.update(zero_phase_buffers=10)),
            "idle output lags the clock (non-live behaviour)": mutate(
                lambda r, e: checkpoint(r, "zero_pads_again").update(mixer_lag_s=2.5)),
            "idle output ahead of the clock": mutate(
                lambda r, e: checkpoint(r, "zero_pads_again").update(mixer_lag_s=-2.5)),
            "output stalls between checkpoints": mutate(
                lambda r, e: checkpoint(r, "zero_pads_again").update(mixer_buffers=checkpoint(r, "deck3_only")["mixer_buffers"])),
            "caps change at a checkpoint": mutate(lambda r, e: checkpoint(r, "deck2_F32LE").update(src_caps="F32LE,44100,2,interleaved")),
            "zero-pad checkpoints missing": mutate(
                lambda r, e: r.update(checkpoints=[c for c in r["checkpoints"] if not c["label"].startswith("zero")])),
            "a deck was present during a zero-pad checkpoint": mutate(
                lambda r, e: checkpoint(r, "zero_end").update(deck_pads=1)),
            "probe ran without the deck-bus capsfilter": mutate(lambda r, e: r.update(deck_bus_capsfilter=False)),
            "probe mixer was not force-live": mutate(lambda r, e: r.update(force_live=False)),
            "probe ran at another rate": mutate(lambda r, e: r.update(rate=48000)),
            "no probe output at all": [],
            "result without events": _synthetic_probe_output(44100)[:1],
            "result is not JSON": ["A2CAPS {not json", "A2EVENTS []"],
        }
        for name, lines in failing.items():
            with self.subTest(mutation=name):
                bad = _run_evaluator(evaluator, lines, rate=44100)
                self.assertEqual(bad.returncode, 1, bad.stdout)
                self.assertIn("GATE B FAIL - DO NOT INSTALL r0146 on this station", bad.stdout)
        with self.subTest(mutation="probe exited non-zero"):
            bad = _run_evaluator(evaluator, _synthetic_probe_output(44100), rate=44100, probe_rc=124)
            self.assertEqual(bad.returncode, 1, bad.stdout)
        with self.subTest(mutation="station rate differs from the probe's"):
            bad = _run_evaluator(evaluator, _synthetic_probe_output(44100), rate=48000)
            self.assertEqual(bad.returncode, 1, bad.stdout)

    def test_release_notes_state_the_mandatory_preflight_and_every_exclusion(self):
        notes = _text(NOTES)
        flat = _flat(notes)
        preflight = _flat(_section(notes, "## Mandatory station GStreamer preflight (installation gate)"))
        for required in (
            "on **any** station, both of these checks must pass on that station",
            "**Gate A, construct-time capability:**", "`force-live=true`",
            "A fallback to the inherited non-live mixer is a blocking failure",
            "**Gate B, actual fixed-format output:**", "a standalone probe at the station sample rate",
            "`audio/x-raw,format=S16LE,rate=<station rate>,channels=2,layout=interleaved`",
            "exactly once, from zero-pad startup", "no unexpected renegotiation, no DISCONT, no GStreamer bus error",
            "Both checks are mandatory.", "must not install r0146 if either check fails, is skipped or cannot be completed safely",
            "**WRJE must pass both checks independently**", "not a barrier to publication", "not an UpdateCenter action",
        ):
            self.assertIn(required, preflight)
        self.assertNotRegex(preflight, r"(?i)\boptional(ly)?\b")
        limitations = _section(notes, "## Limitations (accepted; not remediated in r0146)")
        items = [_flat(item) for item in re.split(r"^\s*\d+\. ", limitations, flags=re.M)[1:]]
        self.assertEqual(len(items), 12)

        def item(marker):
            found = [text for text in items if marker in text]
            self.assertEqual(len(found), 1, marker)
            return found[0]

        # Newly required categories: each must be present AND correctly described (never as resolved).
        d = item("Roadmap 1.21-D")
        self.assertRegex(d, r"No independent normal live-fill or exhausted-hour scheduler defect was established")
        self.assertRegex(d, r"r0146 contains no separate scheduler correction")
        e = item("Roadmap 1.21-E")
        self.assertRegex(e, r"Expanded Monitoring continuity classification and automatic dead-air recovery are \*\*not implemented\*\* in r0146")
        starvation = item("**Decoder starvation.**")
        self.assertRegex(starvation, r"Ordinary-path preparation starvation and mid-song decoder starvation remain separately tracked defects")
        self.assertRegex(starvation, r"r0146 does not correct either")
        dither = item("**Dithering.**")
        self.assertRegex(dither, r"deck-bus output is fixed at S16LE")
        self.assertRegex(dither, r"does not explicitly guarantee portable dithering behavior across different GStreamer installations")
        version = item("**GStreamer version.**")
        self.assertRegex(version, r"does not upgrade the system GStreamer runtime to 1\.28\.9 or any other version")
        self.assertRegex(version, r"compatible station-installed runtime, subject to the mandatory preflight")
        # Retained limitations.
        self.assertRegex(item("**Claimed-but-unaired recovery**"), r"remains roadmap 1\.21-B")
        self.assertRegex(item("**Claimed-but-unaired recovery**"), r"PlayEvent admission authority lies upstream of the deck mixer and is unchanged")
        self.assertRegex(item("**Durable crash / power-outage checkpoint recovery**"), r"remains roadmap 1\.21-C")
        self.assertRegex(item("**Ordinary outgoing-tail truncation (F14b)**"), r"remains separately tracked")
        self.assertRegex(item("**The non-live fallback remains defective**"), r"does not count as continuity protection")
        self.assertRegex(item("**The deck bus is 16-bit.**"), r"reduced to 16 bits by GStreamer's audio converter")
        self.assertRegex(item("**Hour Refill policy is unchanged.**"), r"unchanged")
        self.assertRegex(item("was **not** identified"), r"native cause of the crash seen in the unfixed Phase A candidate")
        # None of the excluded work is described as done in the release.
        self.assertNotRegex(flat, r"(?i)1\.21-[DE][^.]*\b(is|are|was|were) (now )?(implemented|resolved|fixed|corrected|included)")
        self.assertNotRegex(flat, r"(?i)(decoder starvation|dithering)[^.]*\b(is|are|was|were) (now )?(resolved|fixed|corrected)")
        self.assertIn("Not included: no GStreamer upgrade, no Hour Refill policy change, no live-fill or recovery feature.", flat)


class R0146HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_is_the_exact_linear_chain_on_r0145(self):
        """Published provenance only: runs in every clone."""
        self.assertEqual(_git("rev-parse", f"{R0145_RELEASE}^{{tree}}"), R0145_TREE)
        commits = [commit for commit, _tree, _origin in ENGINEERING_CHAIN]
        self.assertEqual(len(commits), 7)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0145_RELEASE}..{ENGINEERING}").split(), commits)
        parent = R0145_RELEASE
        for commit, tree, origin in ENGINEERING_CHAIN:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            self.assertIn(f"(cherry picked from commit {origin})", _git("log", "-1", "--format=%B", commit), commit)
            self.assertNotEqual(_changed(f"{commit}^", commit), set(), commit)
            parent = commit
        self.assertEqual(_git("rev-list", "--merges", f"{R0145_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_introducing("deploy/releases/r0145.json"), [R0145_RELEASE])
        self.assertEqual(len(set(SOURCE_COMMITS)), 7)
        self.assertEqual((SOURCE_COMMITS[0], SOURCE_COMMITS[3], SOURCE_COMMITS[-1]),
                         (ACCEPTED_B[0], ACCEPTED_A2_PRODUCT[0], ACCEPTED_A[0]))
        # The integrated B commit reproduces the accepted B tree exactly (same base: published evidence).
        self.assertEqual(ENGINEERING_CHAIN[0][1], ACCEPTED_B[1])

    def test_change_inventory_is_exact(self):
        """The 1 + 5 workstream file sets from the published integration boundaries (every clone)."""
        self.assertEqual(_changed(R0145_RELEASE, B_INTEGRATED), B_FILES)
        self.assertEqual(_changed(B_INTEGRATED, ENGINEERING), A_FILES)
        self.assertEqual(A_FILES & B_FILES, set())
        self.assertEqual(_changed(R0145_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0145_RELEASE, ENGINEERING, "--diff-filter=A"), ADDED_FILES)
        self.assertEqual(_changed(R0145_RELEASE, ENGINEERING, "--diff-filter=M"), MODIFIED_FILES)
        self.assertEqual(_changed(R0145_RELEASE, ENGINEERING, "--diff-filter=DRTC"), set())

    def test_every_commit_is_a_patch_identical_cherry_pick_of_its_accepted_source(self):
        """Patch-ID equality with each original accepted source commit (requires those objects; skipped, naming
        them, in a public clone)."""
        _require_sources(self, *SOURCE_COMMITS)
        for commit, _tree, origin in ENGINEERING_CHAIN:
            with self.subTest(commit=commit):
                self.assertEqual(_patch_id(commit), _patch_id(origin))
        self.assertEqual(_git("rev-list", "--reverse", f"{R0145_RELEASE}..{ACCEPTED_A[0]}").split(),
                         list(SOURCE_COMMITS[1:]))
        self.assertEqual(_git("rev-parse", f"{ACCEPTED_B[0]}^"), R0145_RELEASE)

    def test_accepted_sources_and_trees_are_reproduced(self):
        """Accepted source trees (requires those objects)."""
        _require_sources(self, ACCEPTED_A[0], ACCEPTED_A2_PRODUCT[0], ACCEPTED_B[0])
        for commit, tree in (ACCEPTED_A, ACCEPTED_A2_PRODUCT, ACCEPTED_B):
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)

    def test_every_changed_file_equals_its_accepted_source_tip(self):
        """Byte equality with the accepted source tips (requires those objects)."""
        _require_sources(self, ACCEPTED_A[0], ACCEPTED_B[0])
        self.assertEqual(_changed(R0145_RELEASE, ACCEPTED_A[0]), A_FILES)
        self.assertEqual(_changed(R0145_RELEASE, ACCEPTED_B[0]), B_FILES)
        for tip, paths in ((ACCEPTED_A[0], A_FILES), (ACCEPTED_B[0], B_FILES)):
            for path in sorted(paths):
                with self.subTest(path=path):
                    self.assertEqual(_blob(ENGINEERING, path), _blob(tip, path))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0145_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0145_RELEASE}..{release}").split(),
                         [commit for commit, _tree, _origin in ENGINEERING_CHAIN] + [release])
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0145_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0145_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0145_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0145_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0145_RELEASE, release)
                          if "/static/" in path or "/templates/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0145_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0145_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        touched = _changed(R0145_RELEASE, release)
        self.assertEqual({path for path in touched if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        self.assertEqual(touched - RELEASE_FILES, CHANGE_FILES)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0146.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0145_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_production_change_is_exactly_the_engine_module(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0145_RELEASE, release)
        modules = {path[:-3].replace("/", ".") for path in changed if path.endswith(".py") and "/tests/" not in path}
        self.assertEqual(modules, PRODUCTION_MODULES)
        self.assertEqual({path for path in changed if not path.endswith(".py") and not path.startswith("docs/")
                          and path != MANIFEST_PATH}, set())

    def test_declared_restart_is_exactly_the_engine(self):
        """The engine loads the changed module; its changed code (CHANGED_ENGINE_SYMBOLS) is referenced by no other
        production module, _build_main_pipeline is called only from PlaybackEngine.start, and only the engine
        service's run_engine command constructs a PlaybackEngine. Gunicorn's over-approximating import graph does
        reach library.services.engine (it imports engine constants/helpers), but no Gunicorn code path builds the
        engine pipeline, so a running Gunicorn never executes the changed code."""
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        reach = {service: graph.service_reach(service) for service in LONG_RUNNING_ENTRIES}
        self.assertIn("library.services.engine", reach["isadoraair-engine"])
        production = [path for path in graph.files if "/tests/" not in path]
        for path in production:
            if path == "library/services/engine.py":
                continue
            tree = ast.parse(_show(release, path).decode("utf-8"))
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
            with self.subTest(path=path):
                self.assertEqual(names & set(CHANGED_ENGINE_SYMBOLS), set())
                calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and getattr(node.func, "id", getattr(node.func, "attr", None)) == "PlaybackEngine"]
                if path != "library/management/commands/run_engine.py":
                    self.assertEqual(calls, [], "only the engine service constructs a PlaybackEngine")
        engine_tree = ast.parse(_show(release, "library/services/engine.py").decode("utf-8"))
        callers = []
        for cls in (node for node in engine_tree.body if isinstance(node, ast.ClassDef)):
            for fn in (node for node in cls.body if isinstance(node, ast.FunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_build_main_pipeline":
                        callers.append(f"{cls.name}.{fn.name}")
        self.assertEqual(callers, ["PlaybackEngine.start"])
        run_engine = ast.parse(_show(release, "library/management/commands/run_engine.py").decode("utf-8"))
        self.assertTrue([node for node in ast.walk(run_engine) if isinstance(node, ast.Call)
                         and getattr(node.func, "id", None) == "PlaybackEngine"])
        self.assertEqual(LONG_RUNNING_ENTRIES["isadoraair-engine"], "library.management.commands.run_engine")


class R0146MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0145_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0146.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0146", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0146"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0146ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0146_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0145_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 145)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0145_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0145_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0146_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 147)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0144"], commits["r0145"], commits["r0146"]),
                         (R0132_RELEASE, R0144_RELEASE, R0145_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0145 -> r0146", out.getvalue())


class R0146TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0146", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0146", release))
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
                         [("r0145", R0145_RELEASE), ("r0146", release)])
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

    def test_catch_up_from_r0132_wrje_last_documented_release(self):
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


class R0146ReachabilityTests(SimpleTestCase):
    def test_every_published_object_is_reachable_from_r_alone(self):
        """The published objects of (1) in the module docstring; the original source commits are deliberately not
        asserted here (they are not published)."""
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            for commit, tree, _origin in ENGINEERING_CHAIN:
                self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}", cwd=bare), tree)
            self.assertEqual(_git("rev-parse", f"{R0145_RELEASE}^{{tree}}", cwd=bare), R0145_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            isolated = derive_plan(_repository(bare), release, R0145_RELEASE, "r0146", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0145_RELEASE, "r0146", known_units=_known_units())
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


class R0146PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0146",
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
        self._assert_parity("r0145")

    def test_catch_up_from_r0144_application_and_protected_planners_match(self):
        self._assert_parity("r0144")

    def test_catch_up_from_r0132_application_and_protected_planners_match(self):
        self._assert_parity("r0132")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
