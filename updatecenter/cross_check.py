"""Manifest-vs-repository-reality cross-checking -- [P0] 1.1 Phase A.

`manifest.py` proves one manifest is internally well-formed.
`release_chain.py` proves a SET of manifests forms one unambiguous
chain. Neither ever asks whether a manifest's *claims* actually match
the real repository content at its target commit -- that is this
module's entire job, and it is a DIFFERENT kind of check on purpose
(see docs/UPDATE_CENTER.md's "machine-verifiable facts vs.
release-author intent" section): a release author decides intent
(which services need restarting, whether a schema change is additive)
-- nothing here second-guesses that. But anything with an objective,
git-inspectable answer (does this migration file exist, does this
systemd unit have a deploy/ template, does the requirements hash
actually match) is verified here, never taken on faith.

Every check reads the TARGET commit's content via
git_adapter.path_exists_at_commit / read_bytes_at_commit -- never by
checking that commit out. The working tree is never touched by
anything in this module."""
from __future__ import annotations

import dataclasses

from . import git_adapter, manifest as manifest_mod

REQUIREMENTS_PATH = "requirements.txt"
DEPLOY_DIR = "deploy"
_PRE_GUARD_RELEASES = frozenset({"r0001", "r0002", "r0003", "r0004", "r0005"})


@dataclasses.dataclass(frozen=True)
class CrossCheckFinding:
    """One concrete disagreement between a manifest's claim and the
    target commit's actual content. `field` names which manifest field
    is in question (for UI grouping); `detail` is a human-readable,
    non-secret explanation safe to display/log verbatim."""
    field: str
    detail: str


def cross_check_release(rel: manifest_mod.ReleaseManifest, target_commit: str, checkout_root,
                         app_label_paths: dict | None = None,
                         previous_commit: str | None = None) -> list[CrossCheckFinding]:
    """Returns every finding for one release against its resolved
    target commit -- empty list means the manifest's objectively-
    checkable claims all match reality. Never raises for an ordinary
    mismatch (that's exactly what this function exists to surface as
    data, not an exception) -- it only raises if `target_commit` itself
    isn't a real, reachable commit at all, which is a precondition
    failure the caller is expected to have already ruled out via
    git_adapter.commit_exists()."""
    if not git_adapter.commit_exists(checkout_root, target_commit):
        raise ValueError(f"target_commit {target_commit!r} does not exist in this checkout's object database")

    findings: list[CrossCheckFinding] = []
    findings.extend(_check_migrations(rel, target_commit, checkout_root, app_label_paths or {}))
    findings.extend(_check_requirements(rel, target_commit, checkout_root))
    findings.extend(_check_units(rel, target_commit, checkout_root))
    if previous_commit is not None:
        findings.extend(_check_protected_runtime_intent(
            rel, previous_commit, target_commit, checkout_root,
        ))
    return findings


def _check_protected_runtime_intent(rel, previous_commit, target_commit,
                                    checkout_root) -> list[CrossCheckFinding]:
    # The immutable r0001-r0005 chain predates this r0006 rule; r0003
    # legitimately changed this tree before manual_bootstrap_required existed.
    if rel.release_id in _PRE_GUARD_RELEASES:
        return []
    paths = git_adapter.changed_paths_between(
        checkout_root, previous_commit, target_commit, "deploy/updater_runtime",
    )
    if paths is None:
        return [CrossCheckFinding(
            field="manual_bootstrap_required",
            detail="could not verify the protected-runtime predecessor diff",
        )]
    # Once Phase D is installed, a signed protected-runtime candidate is
    # precisely the mechanism authorized to change this tree. The legacy
    # manual valve remains mandatory only when the target manifest does not
    # carry a strictly parsed protected_runtime declaration. Root still
    # independently validates its generation, descriptor, attestations and
    # signed policy before any mutation.
    if paths and not (
        rel.manual_bootstrap_required or rel.protected_runtime is not None
    ):
        return [CrossCheckFinding(
            field="manual_bootstrap_required",
            detail=(
                "deploy/updater_runtime/ changes require either "
                "manual_bootstrap_required=true or a validated "
                "protected_runtime candidate"
            ),
        )]
    return []


def _migration_ref_to_path(ref: str, app_label_paths: dict) -> str:
    """"library.0079_mediaplaybackincident" -> "library/migrations/0079_mediaplaybackincident.py"

    `app_label_paths` maps an app_label to its repo-relative package
    directory when that differs from the naive "app_label at repo
    root" assumption -- e.g. the real `tts` app_label's package
    actually lives at `isadoraair/tts/`, not a top-level `tts/`
    directory (confirmed live: the naive assumption produced a false
    "migration does not exist" finding against the real, correctly-
    committed `isadoraair/tts/migrations/0001_initial.py`). Falls back
    to the naive `{app_label}/` convention for any label not in the
    mapping -- correct for the large majority of this project's apps,
    which really do live at repo-root/<app_label>/."""
    app_label, _, name = ref.partition(".")
    package_dir = app_label_paths.get(app_label, app_label)
    return f"{package_dir}/migrations/{name}.py"


def _check_migrations(rel, target_commit, checkout_root, app_label_paths) -> list[CrossCheckFinding]:
    findings = []
    for ref in rel.migrations_required:
        path = _migration_ref_to_path(ref, app_label_paths)
        exists = git_adapter.path_exists_at_commit(checkout_root, target_commit, path)
        if exists is not True:
            findings.append(CrossCheckFinding(
                field="migrations_required",
                detail=f"{ref!r} declared required, but {path} does not exist at the target commit",
            ))
    return findings


def _check_requirements(rel, target_commit, checkout_root) -> list[CrossCheckFinding]:
    # Only worth reading requirements.txt at all when the manifest
    # actually makes a claim about it -- a release that never touched
    # dependencies has nothing to cross-check here, and a repository
    # legitimately might not have a requirements.txt at all (this
    # project's own does, but this function must not assume every
    # possible target commit/test fixture does too).
    if not rel.python_requirements_changed:
        return []
    actual = git_adapter.read_bytes_at_commit(checkout_root, target_commit, REQUIREMENTS_PATH)
    if actual is None:
        return [CrossCheckFinding(
            field="python_requirements_changed",
            detail=f"manifest declares requirements changed, but {REQUIREMENTS_PATH} could not be read at the target commit",
        )]
    actual_hash = manifest_mod.sha256_hex(actual)
    if rel.requirements_sha256 is not None and actual_hash != rel.requirements_sha256:
        return [CrossCheckFinding(
            field="requirements_sha256",
            detail=(
                f"manifest declares requirements_sha256={rel.requirements_sha256!r} "
                f"but {REQUIREMENTS_PATH} at the target commit actually hashes to "
                f"{actual_hash!r}"
            ),
        )]
    return []


def _check_units(rel, target_commit, checkout_root) -> list[CrossCheckFinding]:
    findings = []
    for unit in (*rel.systemd_units_changed, *rel.systemd_units_new_required, *rel.systemd_units_new_optional):
        path = f"{DEPLOY_DIR}/{unit}"
        exists = git_adapter.path_exists_at_commit(checkout_root, target_commit, path)
        if exists is not True:
            findings.append(CrossCheckFinding(
                field="systemd_units",
                detail=f"{unit!r} is declared, but {path} does not exist at the target commit",
            ))
    for unit in rel.systemd_units_removed_or_renamed:
        path = f"{DEPLOY_DIR}/{unit}"
        exists = git_adapter.path_exists_at_commit(checkout_root, target_commit, path)
        if exists is True:
            findings.append(CrossCheckFinding(
                field="systemd_units_removed_or_renamed",
                detail=f"{unit!r} is declared removed/renamed, but {path} still exists at the target commit",
            ))
    return findings
