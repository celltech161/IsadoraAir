"""Non-privileged PLAN_UPDATE logic -- [P0] 1.1 Phase A.

Ties together `git_adapter` (safety checks + read-only commit
resolution), `release_chain` (chain structure + per-release commit
resolution), and `cross_check` (manifest-vs-reality) into one
deterministic, serializable `Plan`. This module uses the CURRENT
checkout's Django migration machinery only for current schema health,
applied-state bookkeeping, and a clearly-labelled dependency preview.
It cannot validate a newer target source's migration graph; that is a
mandatory future Phase B staged-source gate. Everything it imports
from is otherwise dependency-free by design (see manifest.py/
git_adapter.py/release_chain.py/cross_check.py's own docstrings).

Nothing here mutates the working tree. `fetch_updates()` is the one
function that performs a network operation (git fetch) and is never
called implicitly by `build_plan()` -- see views.py for exactly when
it's invoked and why that's a deliberate, explicit operator action
rather than a GET-request side effect (docs/UPDATE_CENTER.md's
"Check for Updates side-effect policy")."""
from __future__ import annotations

import dataclasses
import hashlib
import json

from django.db.migrations.loader import MigrationLoader
from django.db import connection

from . import cross_check, git_adapter, manifest as manifest_mod, release_chain, schema_health as schema_health_mod

# Deliberately matches ARCHITECTURE_REPORT.md §15's restart ordering,
# derived from these units' own declared `After=` dependencies
# (deploy/isadoraair-engine.service and isadoraair-monitoring.service
# both declare `After=...isadoraair-gunicorn.service`) -- gunicorn
# first, then everything that depends on it being up, encoders/rbds
# last since nothing else in this project declares an After= on them.
RESTART_ORDER = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",
    "isadoraair-encoders", "isadoraair-rbds",
)


class SafetyStatus:
    """Explicit, finite vocabulary -- matches ARCHITECTURE_REPORT.md
    §7's "SAFETY STATUS" list, extended with a few states this
    implementation's own checks actually need to distinguish. Every
    non-READY_TO_PLAN/UP_TO_DATE value means PLAN_UPDATE refused to
    produce action recommendations -- "fail safe rather than guessing
    destructive deployment actions" applies to every one of these."""
    READY_TO_PLAN = "ready_to_plan"
    UP_TO_DATE = "up_to_date"
    DIRTY_CHECKOUT = "dirty_checkout"
    DETACHED_HEAD = "detached_head"
    NO_ORIGIN_REMOTE = "no_origin_remote"
    LOCAL_COMMITS_NOT_ON_ORIGIN = "local_commits_not_on_origin"
    DIVERGED_FROM_ORIGIN = "diverged_from_origin"
    INVALID_RELEASE_MANIFEST = "invalid_release_manifest"
    INSTALLED_RELEASE_UNKNOWN = "installed_release_unknown"
    TARGET_COMMIT_UNKNOWN = "target_commit_unknown"
    INSTALLED_RELEASE_TOO_OLD = "installed_release_too_old"
    CROSS_CHECK_FAILED = "cross_check_failed"
    MANUAL_SYSTEM_PACKAGE_ACTION_REQUIRED = "manual_system_package_action_required"
    MIGRATION_MANUAL_GATE_REQUIRED = "migration_manual_gate_required"
    # [P0] 1.1 correction: a real production incident (see docs/
    # UPDATE_CENTER.md's "Schema vs. feature activation" section)
    # proved that a migration absent from every release manifest's
    # `migrations_required` is NOT automatically safe to leave
    # unapplied -- it may be genuine drift the manifest chain simply
    # failed to declare. This status fires when Django's OWN actual
    # pending-migration plan (schema_health.py, independent of any
    # manifest) contains something the release chain's expected set
    # does not account for. Never silently ignored, never auto-applied
    # -- a hard stop, same tier as CROSS_CHECK_FAILED.
    SCHEMA_DRIFT_DETECTED = "schema_drift_detected"

    # States in this set are the only ones where an operator could ever
    # eventually click an Update button (Phase B) -- every other state
    # is a hard stop. UP_TO_DATE has nothing to do, so it's not
    # "actionable" either, just healthy.
    ACTIONABLE = frozenset({READY_TO_PLAN})


class TargetSchemaValidationStatus:
    """Phase A can declare expected transition migrations, but it
    cannot load a newer target checkout's Django migration graph.

    A future Phase B executor must materialize the target source and
    compare that graph's read-only plan with the manifest-derived
    expectation before any migration can be applied.
    """
    NOT_APPLICABLE = "not_applicable_no_target_transition"
    NOT_EVALUATED = "not_evaluated_plan_blocked"
    PENDING = "target_schema_plan_validation_pending"


@dataclasses.dataclass(frozen=True)
class MigrationPlan:
    explicitly_required: tuple[str, ...]  # exactly what the release manifests declared
    # Previewed only from migrations that happen to exist in the
    # CURRENT checkout's graph. This is useful evidence, never the
    # authoritative target-source plan.
    current_graph_dependency_preview: tuple[str, ...]
    unknown_to_current_graph: tuple[str, ...]
    already_applied: tuple[str, ...]  # manifest/preview refs already recorded on this station
    expected_transition_unapplied: tuple[str, ...]  # declarative expectation, NOT "what Django will run"
    compatibility: str | None  # "additive" / "destructive" / None (no migrations in plan)


@dataclasses.dataclass(frozen=True)
class Plan:
    safety_status: str
    safety_detail: str
    installed_release_id: str | None
    installed_commit: str | None
    target_release_id: str | None
    target_commit: str | None
    releases_in_plan: tuple[str, ...]  # ordered, oldest to newest, EXCLUDING installed, INCLUDING target
    migrations: MigrationPlan | None
    python_requirements_changed: bool
    apt_packages_new: tuple[str, ...]
    systemd_units_changed: tuple[str, ...]
    systemd_units_new_required: tuple[str, ...]
    systemd_units_new_optional: tuple[str, ...]
    systemd_units_removed_or_renamed: tuple[str, ...]
    collectstatic_required: bool
    services_requiring_restart: tuple[str, ...]  # already in RESTART_ORDER order
    nginx_changed: bool
    runtime_components_changed: bool
    cross_check_findings: tuple[cross_check.CrossCheckFinding, ...]
    fingerprint: str  # stable hash of this plan's own content, for Phase B revalidation (§10)
    # [P0] 1.1 correction: independent of safety_status/releases_in_plan
    # on purpose -- "is the source checkout current" and "is the
    # database schema current" are two different axes (see docs/
    # UPDATE_CENTER.md), never conflated into one field. Always
    # computed, even when safety_status is a hard refusal (dirty
    # checkout, detached HEAD, ...) -- Django's own migration state is
    # answerable regardless of git state.
    schema_health_status: str
    schema_pending_migrations: tuple[str, ...]
    schema_health_detail: str
    target_schema_validation_status: str
    target_schema_validation_detail: str

    def to_serializable(self) -> dict:
        """A plain-dict, JSON-safe shape -- used both for /updates/'s
        template context and as the basis of the fingerprint below.
        Deliberately flat and explicit rather than dataclasses.asdict()
        on the nose, so field renames in this dataclass don't silently
        change the fingerprint's meaning without a human noticing."""
        return {
            "safety_status": self.safety_status,
            "installed_release_id": self.installed_release_id,
            "installed_commit": self.installed_commit,
            "target_release_id": self.target_release_id,
            "target_commit": self.target_commit,
            "releases_in_plan": list(self.releases_in_plan),
            "expected_transition_migrations_unapplied": (
                list(self.migrations.expected_transition_unapplied) if self.migrations else []
            ),
            "migrations_compatibility": self.migrations.compatibility if self.migrations else None,
            "python_requirements_changed": self.python_requirements_changed,
            "apt_packages_new": list(self.apt_packages_new),
            "systemd_units_changed": list(self.systemd_units_changed),
            "systemd_units_new_required": list(self.systemd_units_new_required),
            "systemd_units_new_optional": list(self.systemd_units_new_optional),
            "systemd_units_removed_or_renamed": list(self.systemd_units_removed_or_renamed),
            "collectstatic_required": self.collectstatic_required,
            "services_requiring_restart": list(self.services_requiring_restart),
            "nginx_changed": self.nginx_changed,
            "runtime_components_changed": self.runtime_components_changed,
            "schema_health_status": self.schema_health_status,
            "schema_pending_migrations": list(self.schema_pending_migrations),
            "target_schema_validation_status": self.target_schema_validation_status,
        }


def _fingerprint(serializable: dict) -> str:
    """sha256 of the plan's own canonical (sorted-keys) JSON -- a
    future Phase B executor recomputes this same fingerprint from its
    OWN independent read of the release chain/target commit and
    refuses to execute if it doesn't match what was approved, instead
    of trusting the fingerprint (or anything else) Django handed it
    verbatim. See ARCHITECTURE_REPORT.md §10."""
    canonical = json.dumps(serializable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe(safety_status: str, detail: str, schema_health: schema_health_mod.SchemaHealth, **overrides) -> Plan:
    """Builds a refused/non-actionable Plan -- every field defaults to
    "nothing known," overridable for the few states that DO know a
    partial answer (e.g. DIVERGED_FROM_ORIGIN still knows installed_*).

    `schema_health` is required, not optional -- see Plan's own
    schema_health_status docstring: database schema health is an
    independent axis from the git/manifest-chain safety_status this
    function otherwise builds, computed once at the top of build_plan
    and threaded through every return path, including every refusal."""
    base = dict(
        safety_status=safety_status, safety_detail=detail,
        installed_release_id=None, installed_commit=None,
        target_release_id=None, target_commit=None,
        releases_in_plan=(), migrations=None,
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(),
        nginx_changed=False, runtime_components_changed=False,
        cross_check_findings=(),
        schema_health_status=schema_health.status,
        schema_pending_migrations=schema_health.pending_migrations,
        schema_health_detail=schema_health.detail,
        target_schema_validation_status=TargetSchemaValidationStatus.NOT_EVALUATED,
        target_schema_validation_detail=(
            "No target-source migration plan has been evaluated because update planning is blocked or not yet applicable."
        ),
    )
    base.update(overrides)

    def _jsonable(v):
        if isinstance(v, tuple):
            return [_jsonable(x) for x in v]
        if isinstance(v, cross_check.CrossCheckFinding):
            return {"field": v.field, "detail": v.detail}
        return v

    fingerprint = _fingerprint({"safety_status": safety_status, **{k: _jsonable(v) for k, v in overrides.items()}})
    return Plan(fingerprint=fingerprint, **base)


def fetch_updates(checkout_root) -> bool:
    """The one network/write operation in this module -- see this
    module's own top docstring. Never called from build_plan()."""
    return git_adapter.fetch_remote(checkout_root)


def _preview_current_graph_migrations(
    refs: list[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Given explicitly-required app.migration_name refs, returns
    (explicit, dependency-preview, unknown-to-current-graph) using
    only the CURRENT checkout's Django graph -- so a release that lists
    webrequests.0008 still correctly surfaces tts.0001_initial if 0008
    actually depends on it (see that migration's own dependencies
    list). Ordering follows the graph's own topological order
    (loader.graph.forwards_plan), not manifest declaration order.

    This is explicitly NOT target migration-plan validation. It reads the
    CURRENTLY-RUNNING process's own installed-app migration graph
    (MigrationLoader against the live connection), never the target
    commit's hypothetical graph -- Phase A never checks out or loads
    code from a target commit, so target migrations or dependency edges
    may be absent. Phase B must stage/materialize target source, load
    THAT graph in a controlled non-mutating environment, and compare
    its migration plan with the release-chain expectation before APPLY."""
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    explicit_keys = []
    for ref in refs:
        app_label, _, name = ref.partition(".")
        explicit_keys.append((app_label, name))

    closure_keys: list[tuple[str, str]] = []
    seen = set()
    unknown = []
    for key in explicit_keys:
        if key not in loader.graph.nodes:
            unknown.append(key)
            continue  # target commit cross-check verifies the file; current graph cannot interpret it
        for dep_key in loader.graph.forwards_plan(key):
            if dep_key not in seen:
                seen.add(dep_key)
                closure_keys.append(dep_key)

    explicit_set = set(explicit_keys)
    extra = [k for k in closure_keys if k not in explicit_set]
    to_ref = lambda k: f"{k[0]}.{k[1]}"
    return (
        tuple(to_ref(k) for k in explicit_keys),
        tuple(to_ref(k) for k in extra),
        tuple(to_ref(k) for k in unknown),
    )


def _applied_migration_set() -> set[tuple[str, str]]:
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    return set(loader.applied_migrations.keys())


def app_label_paths_for_checkout(checkout_root) -> dict:
    """{app_label: repo-relative package directory} for every installed
    app whose package does NOT live at a top-level directory matching
    its own app_label -- e.g. the real `tts` app_label's package lives
    at `isadoraair/tts/`, not a top-level `tts/` (found live: the naive
    assumption produced a false cross-check finding against a
    correctly-committed migration). Built from Django's own app
    registry (`django.apps.apps`), which reflects the CURRENTLY LOADED
    checkout -- an acceptable approximation for a target commit other
    than HEAD, since an app relocating its package path across
    releases is a rare, structural change this project has not made
    and would need its own explicit handling regardless."""
    from django.apps import apps as django_apps
    from pathlib import Path as _Path

    checkout_root = _Path(checkout_root).resolve()
    mapping = {}
    for config in django_apps.get_app_configs():
        try:
            relative = _Path(config.path).resolve().relative_to(checkout_root)
        except (ValueError, OSError):
            continue  # app lives outside this checkout (e.g. a third-party package) -- not this project's own code
        if str(relative) != config.label:
            mapping[config.label] = str(relative)
    return mapping


def build_plan(checkout_root, releases_dirname: str = release_chain.RELEASES_DIRNAME_DEFAULT) -> Plan:
    """The whole read-only planning pipeline. Never fetches (callers
    decide when that happens, see fetch_updates above) and never
    checks out/mutates anything. Every failure mode returns a non-
    actionable Plan with a specific safety_status rather than raising
    -- an exception here would be a Phase A bug, not an expected
    outcome of "the checkout/manifests happen to be in a state that
    can't be planned right now."

    `releases_dirname` is a repo-RELATIVE path string (e.g.
    "deploy/releases"), not a filesystem Path -- the release set is
    read from git history at a resolved ref (origin/<branch> when
    available, else HEAD), never from the literal working-tree disk.
    A station behind on releases has, correctly, never checked out a
    newer release's manifest file -- only `git fetch` (which this
    function never performs itself) brings its commit into the object
    database at all. See release_chain.load_manifest_files_at_ref's
    own docstring.

    [P0] 1.1 correction: CURRENT schema health (schema_health.py) is computed
    ONCE, right here, before any git/manifest check -- it is answerable
    regardless of checkout state and is threaded through EVERY return
    path (including every refusal) as Plan.schema_health_status, a
    field independent of safety_status on purpose. See this module's
    Plan docstring and docs/UPDATE_CENTER.md's "Schema vs. feature
    activation" section for why this exists: a real production
    incident proved that ANY migration pending against currently
    loaded source makes the installed schema unhealthy. A target
    manifest cannot mask that fact."""
    schema_health = schema_health_mod.check_schema_health()

    dirty = git_adapter.get_worktree_dirty(checkout_root)
    if dirty:
        return _safe(SafetyStatus.DIRTY_CHECKOUT, "The checkout has uncommitted changes -- refusing to plan until it is clean.", schema_health)

    if git_adapter.is_detached_head(checkout_root):
        return _safe(SafetyStatus.DETACHED_HEAD, "HEAD is detached (not on a branch) -- refusing to plan from an ambiguous position.", schema_health)

    origin_url = git_adapter.get_origin_url(checkout_root)
    if not origin_url:
        return _safe(SafetyStatus.NO_ORIGIN_REMOTE, "No 'origin' remote is configured -- cannot determine an available update.", schema_health)

    branch = git_adapter.get_current_branch(checkout_root)
    head_sha = git_adapter.rev_parse(checkout_root, "HEAD")
    if not head_sha:
        return _safe(SafetyStatus.INSTALLED_RELEASE_UNKNOWN, "Could not resolve the current commit (HEAD).", schema_health)

    remote_ref = f"origin/{branch}" if branch else None
    if remote_ref:
        counts = git_adapter.ahead_behind(checkout_root, "HEAD", remote_ref)
        if counts is not None:
            ahead, behind = counts
            if ahead > 0 and behind > 0:
                return _safe(
                    SafetyStatus.DIVERGED_FROM_ORIGIN,
                    f"Local {branch} has diverged from {remote_ref} ({ahead} ahead, {behind} behind) -- refusing to guess a merge.",
                    schema_health, installed_commit=head_sha,
                )
            if ahead > 0:
                return _safe(
                    SafetyStatus.LOCAL_COMMITS_NOT_ON_ORIGIN,
                    f"Local {branch} has {ahead} commit(s) not present on {remote_ref} -- "
                    "release identity is not authoritative until history is synchronized.",
                    schema_health, installed_commit=head_sha,
                )

    read_ref = remote_ref if (remote_ref and git_adapter.rev_parse(checkout_root, remote_ref)) else "HEAD"
    try:
        manifests = release_chain.load_manifest_files_at_ref(checkout_root, read_ref, releases_dirname)
        chain = release_chain.build_chain(manifests)
    except (manifest_mod.ManifestError, release_chain.ChainError) as exc:
        return _safe(SafetyStatus.INVALID_RELEASE_MANIFEST, str(exc), schema_health)

    try:
        release_commits = release_chain.resolve_unique_release_commits(
            chain, checkout_root, releases_dirname,
        )
    except release_chain.ChainError as exc:
        return _safe(SafetyStatus.TARGET_COMMIT_UNKNOWN, str(exc), schema_health)

    installed = release_chain.resolve_installed_release(chain, checkout_root, head_sha)
    if installed is None:
        return _safe(
            SafetyStatus.INSTALLED_RELEASE_UNKNOWN,
            "Could not determine which known release this checkout's current commit corresponds to.",
            schema_health, installed_commit=head_sha,
        )
    installed_commit = release_commits[installed.manifest.release_id]

    # CURRENT_SCHEMA_HEALTH is absolute: if the currently loaded
    # source declares a migration the current DB has not applied, the
    # installation is unhealthy. Neither the installed manifest nor a
    # future target manifest may "account for" and thereby mask it.
    # This is the exact WebRequestConfig incident boundary.
    if schema_health.status != schema_health_mod.SchemaHealthStatus.SCHEMA_CURRENT:
        pending_detail = (
            f" Pending: {', '.join(schema_health.pending_migrations)}."
            if schema_health.pending_migrations else ""
        )
        return _safe(
            SafetyStatus.SCHEMA_DRIFT_DETECTED,
            "The database is not synchronized with the CURRENTLY installed Django source; "
            "a future release plan cannot make current schema drift healthy." + pending_detail,
            schema_health,
            installed_release_id=installed.manifest.release_id, installed_commit=installed_commit,
        )

    latest = chain[-1]
    if latest.manifest.release_id == installed.manifest.release_id:
        return _safe(
            SafetyStatus.UP_TO_DATE, "Already on the latest known release.", schema_health,
            installed_release_id=installed.manifest.release_id, installed_commit=installed_commit,
            target_release_id=installed.manifest.release_id, target_commit=installed_commit,
            target_schema_validation_status=TargetSchemaValidationStatus.NOT_APPLICABLE,
            target_schema_validation_detail="No newer target release exists, so target-source migration validation is not applicable.",
        )

    target_commit = release_commits[latest.manifest.release_id]

    releases_in_plan = chain[installed.index + 1: latest.index + 1]

    index_by_release_id = {item.manifest.release_id: item.index for item in chain}
    for chained in releases_in_plan:
        minimum = chained.manifest.minimum_supported_release_id
        if minimum is not None and installed.index < index_by_release_id[minimum]:
            return _safe(
                SafetyStatus.INSTALLED_RELEASE_TOO_OLD,
                f"Release {chained.manifest.release_id!r} requires an installed baseline of at least "
                f"{minimum!r}; this station is on {installed.manifest.release_id!r}.",
                schema_health,
                installed_release_id=installed.manifest.release_id,
                installed_commit=installed_commit,
                target_release_id=latest.manifest.release_id,
                target_commit=target_commit,
            )

    app_label_paths = app_label_paths_for_checkout(checkout_root)
    all_findings: list[cross_check.CrossCheckFinding] = []
    for chained in releases_in_plan:
        rel_commit = release_commits[chained.manifest.release_id]
        all_findings.extend(cross_check.cross_check_release(chained.manifest, rel_commit, checkout_root, app_label_paths))

    if all_findings:
        return _safe(
            SafetyStatus.CROSS_CHECK_FAILED,
            f"{len(all_findings)} manifest claim(s) disagree with actual repository content -- see cross_check_findings.",
            schema_health, installed_release_id=installed.manifest.release_id, installed_commit=installed_commit,
            target_release_id=latest.manifest.release_id, target_commit=target_commit,
            cross_check_findings=tuple(all_findings),
        )

    explicit_refs: list[str] = []
    for chained in releases_in_plan:
        for ref in chained.manifest.migrations_required:
            if ref not in explicit_refs:
                explicit_refs.append(ref)

    apt_new: list[str] = []
    units_changed: list[str] = []
    units_new_required: list[str] = []
    units_new_optional: list[str] = []
    units_removed: list[str] = []
    restart_set: set[str] = set()
    requirements_changed = False
    collectstatic = False
    nginx_changed = False
    runtime_components_changed = False
    compatibility_seen: set[str] = set()

    for chained in releases_in_plan:
        m = chained.manifest
        for pkg in m.apt_packages_new:
            if pkg not in apt_new:
                apt_new.append(pkg)
        for u in m.systemd_units_changed:
            if u not in units_changed:
                units_changed.append(u)
        for u in m.systemd_units_new_required:
            if u not in units_new_required:
                units_new_required.append(u)
        for u in m.systemd_units_new_optional:
            if u not in units_new_optional:
                units_new_optional.append(u)
        for u in m.systemd_units_removed_or_renamed:
            if u not in units_removed:
                units_removed.append(u)
        restart_set.update(m.services_requiring_restart)
        requirements_changed = requirements_changed or m.python_requirements_changed
        collectstatic = collectstatic or m.collectstatic_required
        nginx_changed = nginx_changed or m.nginx_changed
        runtime_components_changed = runtime_components_changed or m.runtime_components_changed
        if m.migrations_required:
            compatibility_seen.add(m.migration_compatibility)

    overall_compatibility = None
    if compatibility_seen:
        overall_compatibility = "destructive" if "destructive" in compatibility_seen else "additive"

    migrations = None
    if explicit_refs:
        explicit_tuple, preview_extra, unknown_to_current = _preview_current_graph_migrations(explicit_refs)
        applied = _applied_migration_set()
        all_preview_refs = explicit_tuple + preview_extra
        expected_unapplied = tuple(
            ref for ref in all_preview_refs
            if tuple(ref.split(".", 1)) not in applied
        )
        already_applied = tuple(ref for ref in all_preview_refs if ref not in expected_unapplied)
        migrations = MigrationPlan(
            explicitly_required=explicit_tuple,
            current_graph_dependency_preview=preview_extra,
            unknown_to_current_graph=unknown_to_current,
            already_applied=already_applied,
            expected_transition_unapplied=expected_unapplied,
            compatibility=overall_compatibility,
        )

    ordered_restart = tuple(s for s in RESTART_ORDER if s in restart_set)

    safety_status = SafetyStatus.READY_TO_PLAN
    safety_detail = f"{len(releases_in_plan)} release(s) available: {', '.join(c.manifest.release_id for c in releases_in_plan)}."
    if apt_new:
        safety_status = SafetyStatus.MANUAL_SYSTEM_PACKAGE_ACTION_REQUIRED
        safety_detail = f"New apt package(s) required: {', '.join(apt_new)} -- must be installed manually before this update can proceed."
    elif migrations is not None and migrations.compatibility == "destructive":
        safety_status = SafetyStatus.MIGRATION_MANUAL_GATE_REQUIRED
        safety_detail = "This update includes a non-additive (destructive) migration -- requires an explicit maintenance-window gate, not the unattended path."

    serializable_for_fp = {
        "target_commit": target_commit,
        "releases_in_plan": [c.manifest.release_id for c in releases_in_plan],
        "expected_transition_migrations_unapplied": (
            list(migrations.expected_transition_unapplied) if migrations else []
        ),
        "apt_packages_new": apt_new,
        "systemd_units_changed": units_changed,
        "systemd_units_new_required": units_new_required,
    }

    return Plan(
        safety_status=safety_status, safety_detail=safety_detail,
        installed_release_id=installed.manifest.release_id, installed_commit=installed_commit,
        target_release_id=latest.manifest.release_id, target_commit=target_commit,
        releases_in_plan=tuple(c.manifest.release_id for c in releases_in_plan),
        migrations=migrations,
        python_requirements_changed=requirements_changed,
        apt_packages_new=tuple(apt_new),
        systemd_units_changed=tuple(units_changed),
        systemd_units_new_required=tuple(units_new_required),
        systemd_units_new_optional=tuple(units_new_optional),
        systemd_units_removed_or_renamed=tuple(units_removed),
        collectstatic_required=collectstatic,
        services_requiring_restart=ordered_restart,
        nginx_changed=nginx_changed,
        runtime_components_changed=runtime_components_changed,
        cross_check_findings=(),
        fingerprint=_fingerprint(serializable_for_fp),
        schema_health_status=schema_health.status,
        schema_pending_migrations=schema_health.pending_migrations,
        schema_health_detail=schema_health.detail,
        target_schema_validation_status=TargetSchemaValidationStatus.PENDING,
        target_schema_validation_detail=(
            "TARGET_SCHEMA_PLAN_VALIDATION_PENDING: Phase A has only the release-chain declaration "
            "and a current-graph preview. Phase B must stage the target source, compute its read-only "
            "Django migration plan, and require an exact match before APPLY."
        ),
    )
