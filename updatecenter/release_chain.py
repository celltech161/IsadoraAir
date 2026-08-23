"""Release-chain assembly and structural validation -- [P0] 1.1 Phase A.

Takes a directory of `deploy/releases/<release_id>.json` files (each
individually structurally valid per `manifest.py`) and answers the
CHAIN-level question `manifest.py` deliberately does not: is this
*set* of releases a single, unambiguous, cycle-free line from one
bootstrap release to one "latest" release?

This module still does not touch git or the filesystem beyond reading
the manifest files it's handed and (in `resolve_release_commit`)
asking `git_adapter` a read-only question -- "does the actual
repository agree with what this manifest set claims" is
`cross_check.py`'s job, one layer further out."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from . import git_adapter, manifest as manifest_mod

RELEASES_DIRNAME_DEFAULT = "deploy/releases"


class ChainError(ValueError):
    """Raised for any problem with the release SET as a whole --
    duplicate/missing/cyclic/ambiguous relationships between otherwise
    individually-valid manifests. Kept distinct from
    manifest.ManifestError so callers (and tests) can tell "one file is
    malformed" apart from "the files disagree with each other"."""


@dataclasses.dataclass(frozen=True)
class ChainedRelease:
    """One release plus its position in the resolved chain."""
    manifest: manifest_mod.ReleaseManifest
    index: int  # 0 = bootstrap, increasing toward latest


def load_manifest_files(releases_dir: Path) -> dict[str, manifest_mod.ReleaseManifest]:
    """Reads every `*.json` file directly under `releases_dir` (not
    recursive -- a flat directory of one file per release is the whole
    point), validates each individually via manifest.validate_manifest_dict,
    and additionally enforces that a file named `<x>.json` actually
    declares `release_id == "<x>"` -- catching a copy-paste-renamed
    manifest before it ever reaches chain assembly, where a
    filename/content mismatch would otherwise just look like a
    same-named duplicate or a phantom missing release depending on
    which one git_adapter later resolves against.

    Raises manifest.ManifestError (individual file malformed) or
    ChainError (filename/release_id mismatch, or a JSON syntax error --
    wrapped as ChainError since it's still "this file cannot even be
    read as a candidate release," not a structural-content problem
    manifest.py itself would catch). Never partially returns a mix of
    valid and invalid -- any single bad file fails the whole load, on
    the same "fail safe, don't guess" basis as everything else in this
    feature."""
    if not releases_dir.is_dir():
        raise ChainError(f"releases directory does not exist: {releases_dir}")
    result: dict[str, manifest_mod.ReleaseManifest] = {}
    for path in sorted(releases_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ChainError(f"{path.name}: could not read/parse as JSON ({exc})") from exc
        parsed = manifest_mod.validate_manifest_dict(raw, source_label=path.name)
        expected_stem = parsed.release_id
        if path.stem != expected_stem:
            raise ChainError(
                f"{path.name}: file name does not match its own release_id "
                f"({expected_stem!r}) -- rename the file or fix release_id"
            )
        if parsed.release_id in result:
            raise ChainError(f"duplicate release_id {parsed.release_id!r} (already loaded from another file)")
        result[parsed.release_id] = parsed
    return result


def load_manifest_files_at_ref(checkout_root: Path, ref: str,
                                releases_dirname: str = RELEASES_DIRNAME_DEFAULT) -> dict[str, manifest_mod.ReleaseManifest]:
    """Same contract as load_manifest_files, but reads the release set
    from git history at `ref` (via git_adapter.list_files_at_commit +
    read_bytes_at_commit) instead of the literal working-tree disk.

    This is the one that actually matters for planning: a station that
    is several releases behind has, by definition, never checked out
    the newer releases' manifest files -- only `git fetch` (never
    checkout) brings their commits into this checkout's object
    database. Reading from `ref` (the planner resolves this to
    `origin/<branch>` when available, see planner.py) is what makes
    "WRJE may be several releases behind" actually plannable at all.
    load_manifest_files (disk-based) remains useful separately -- e.g.
    for a human authoring a NEW manifest to validate it before ever
    committing it (manage.py validate_release_manifests's default
    mode) -- but the planner itself must not use it."""
    sha = git_adapter.rev_parse(checkout_root, ref)
    if sha is None:
        raise ChainError(f"could not resolve ref {ref!r} to a commit")
    names = git_adapter.list_files_at_commit(checkout_root, sha, releases_dirname)
    if names is None:
        raise ChainError(f"{releases_dirname} does not exist at {ref!r} ({sha[:12]})")
    result: dict[str, manifest_mod.ReleaseManifest] = {}
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        relative_path = f"{releases_dirname}/{name}"
        raw_bytes = git_adapter.read_bytes_at_commit(checkout_root, sha, relative_path)
        if raw_bytes is None:
            raise ChainError(f"{relative_path}: could not be read at {ref!r} ({sha[:12]})")
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChainError(f"{name}: could not read/parse as JSON at {ref!r} ({exc})") from exc
        parsed = manifest_mod.validate_manifest_dict(raw, source_label=name)
        expected_stem = parsed.release_id
        if Path(name).stem != expected_stem:
            raise ChainError(
                f"{name}: file name does not match its own release_id "
                f"({expected_stem!r}) -- rename the file or fix release_id"
            )
        if parsed.release_id in result:
            raise ChainError(f"duplicate release_id {parsed.release_id!r} (already loaded from another file)")
        result[parsed.release_id] = parsed
    return result


def build_chain(manifests: dict[str, manifest_mod.ReleaseManifest]) -> list[ChainedRelease]:
    """Validates the SET's shape and returns it as one ordered list,
    bootstrap first. Every failure mode below is deliberate and tested
    (tests/test_release_chain.py):

      * zero releases                       -> ChainError
      * zero or more-than-one bootstrap      -> ChainError (exactly one
        release with previous_release_id=None is required -- more than
        one means an ambiguous chain ORIGIN, not just an ambiguous tip)
      * a previous_release_id that doesn't
        resolve to a loaded release          -> ChainError ("missing
        predecessor")
      * two releases sharing the same
        previous_release_id                  -> ChainError ("forked" --
        the chain must be a single line, never a tree)
      * a cycle anywhere in the graph        -> ChainError
      * a release unreachable from the
        bootstrap (disconnected)             -> ChainError -- this can
        only actually happen alongside one of the above cases given the
        single-predecessor-pointer structure, but is checked explicitly
        rather than assumed impossible
    """
    if not manifests:
        raise ChainError("no release manifests found -- at least a bootstrap release is required")

    bootstraps = [m for m in manifests.values() if m.is_bootstrap]
    if len(bootstraps) == 0:
        raise ChainError("no bootstrap release found (every release has a non-null previous_release_id)")
    if len(bootstraps) > 1:
        ids = sorted(m.release_id for m in bootstraps)
        raise ChainError(f"more than one bootstrap release found: {ids!r} -- the chain origin is ambiguous")

    # successor[X] = release whose previous_release_id == X
    successor: dict[str, manifest_mod.ReleaseManifest] = {}
    for rel in manifests.values():
        if rel.previous_release_id is None:
            continue
        if rel.previous_release_id not in manifests:
            raise ChainError(
                f"release {rel.release_id!r} declares previous_release_id "
                f"{rel.previous_release_id!r}, which does not exist in the loaded set"
            )
        if rel.previous_release_id in successor:
            other = successor[rel.previous_release_id].release_id
            raise ChainError(
                f"release {rel.previous_release_id!r} has two successors "
                f"({rel.release_id!r} and {other!r}) -- the chain must be a single "
                f"line, never a fork"
            )
        successor[rel.previous_release_id] = rel

    # Walk forward from the bootstrap, detecting a cycle by bounding
    # the walk length to len(manifests) -- if that bound is exceeded,
    # something loops back on itself (impossible to reach "no
    # successor" within a finite acyclic set of this size otherwise).
    ordered: list[ChainedRelease] = [ChainedRelease(manifest=bootstraps[0], index=0)]
    seen_ids = {bootstraps[0].release_id}
    current = bootstraps[0]
    while current.release_id in successor:
        nxt = successor[current.release_id]
        if nxt.release_id in seen_ids:
            raise ChainError(f"cycle detected in release chain at {nxt.release_id!r}")
        ordered.append(ChainedRelease(manifest=nxt, index=len(ordered)))
        seen_ids.add(nxt.release_id)
        current = nxt
        if len(ordered) > len(manifests):
            raise ChainError("cycle detected in release chain (walk exceeded total release count)")

    if len(ordered) != len(manifests):
        unreachable = sorted(set(manifests) - seen_ids)
        raise ChainError(
            f"release(s) {unreachable!r} are not reachable from the bootstrap release "
            f"-- disconnected chain"
        )

    index_by_id = {item.manifest.release_id: item.index for item in ordered}
    for item in ordered:
        minimum = item.manifest.minimum_supported_release_id
        if minimum is None:
            continue
        if item.manifest.is_bootstrap:
            raise ChainError("bootstrap release cannot declare minimum_supported_release_id")
        if minimum not in index_by_id:
            raise ChainError(
                f"release {item.manifest.release_id!r} declares unknown "
                f"minimum_supported_release_id {minimum!r}"
            )
        if index_by_id[minimum] >= item.index:
            raise ChainError(
                f"release {item.manifest.release_id!r} minimum_supported_release_id "
                f"{minimum!r} must be an earlier release in the same chain"
            )

    return ordered


def resolve_release_commit(chained: ChainedRelease, checkout_root: Path,
                            releases_dirname: str = RELEASES_DIRNAME_DEFAULT) -> str | None:
    """The commit associated with one release, per manifest.py's
    documented rule: the bootstrap release's `bootstrap_commit` field
    directly, or -- for every other release -- whichever commit first
    introduced `deploy/releases/<release_id>.json` into this
    repository's git history (git_adapter.find_introducing_commit).
    Returns None if that commit cannot be established (file not found
    in history, or added more than once) -- callers must treat that as
    "this release's position in git history is unknown," never guess."""
    rel = chained.manifest
    if rel.is_bootstrap:
        return rel.bootstrap_commit
    relative_path = f"{releases_dirname}/{rel.release_id}.json"
    return git_adapter.find_introducing_commit(checkout_root, relative_path)


def resolve_unique_release_commits(
    chain: list[ChainedRelease], checkout_root: Path,
    releases_dirname: str = RELEASES_DIRNAME_DEFAULT,
) -> dict[str, str]:
    """Resolve every release id to one distinct immutable commit.

    A normal release whose manifest was modified/deleted/re-added is
    unresolved by ``find_introducing_commit``. Two manifests first
    added by one commit are also rejected: otherwise two deployment
    transitions would collapse onto one source identity and installed
    release resolution would silently skip one of them.
    """
    resolved: dict[str, str] = {}
    owner_by_commit: dict[str, str] = {}
    for chained in chain:
        release_id = chained.manifest.release_id
        commit = resolve_release_commit(chained, checkout_root, releases_dirname)
        if commit is None or not git_adapter.commit_exists(checkout_root, commit):
            raise ChainError(
                f"release {release_id!r} has no unique, immutable, reachable commit identity"
            )
        other = owner_by_commit.get(commit)
        if other is not None:
            raise ChainError(
                f"releases {other!r} and {release_id!r} resolve to the same commit {commit[:12]} -- "
                "each release transition must be introduced by its own commit"
            )
        owner_by_commit[commit] = release_id
        resolved[release_id] = commit
    return resolved


def resolve_installed_release(chain: list[ChainedRelease], checkout_root: Path,
                               head_sha: str,
                               releases_dirname: str = RELEASES_DIRNAME_DEFAULT) -> ChainedRelease | None:
    """Which release, if any, the checkout currently at `head_sha` is
    "on" -- the LATEST release in the chain whose own commit is `head_sha`
    itself or an ancestor of it. Walks the chain from latest to
    earliest (not earliest to latest) so the first match found is
    already the correct answer, no extra "keep the latest match" state
    needed.

    Returns None if not even the bootstrap release's commit is an
    ancestor of head_sha -- meaning this checkout predates the entire
    known release chain, or the chain's bootstrap_commit is simply
    wrong. Callers must treat None as "cannot determine installed
    release," not as "assume the bootstrap.\""""
    for chained in reversed(chain):
        commit = resolve_release_commit(chained, checkout_root, releases_dirname)
        if commit is None:
            continue
        if commit == head_sha:
            return chained
        if git_adapter.is_ancestor(checkout_root, commit, head_sha):
            return chained
    return None
