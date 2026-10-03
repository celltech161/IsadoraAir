"""Durable UpdateJob audit mirror for the managed Update Center.

Phase A introduced the additive table without creating rows. Phase C's
superuser-only POST creates one row before protected-backend submission;
root-owned files remain execution truth and this PostgreSQL row remains the
durable application/UI mirror across Gunicorn restarts.

Every field matches something Phase B genuinely needs to make the job
durable across a Gunicorn restart (see ARCHITECTURE_REPORT.md §6) --
none were added speculatively. The one piece of Phase-B-shaped
groundwork laid here that Phase A itself never exercises is the
`active_lock` uniqueness constraint (§18's concurrency requirement) --
cheap and additive to add now, and adding it later would mean a second
migration touching this same table for a change that's already fully
understood today.
"""
import uuid

from django.conf import settings
from django.db import models


class UpdateJobState:
    """Explicit, finite vocabulary. Phase A never sets any of these
    except by not existing (no UpdateJob row was created by
    Phase A code). Phase C's application mirror writes reconciled transitions;
    root uses its separate vocabulary and state store. Kept here, not as a bare tuple, so
    both the model's `choices=` and any future planner/executor code
    import the SAME names rather than risking a typo'd string drifting
    from the model's own choices list."""
    QUEUED = "queued"
    PLANNED = "planned"
    RUNNING = "running"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"

    CHOICES = [
        (QUEUED, "Queued"),
        (PLANNED, "Planned"),
        (RUNNING, "Running"),
        (SUBMISSION_UNCERTAIN, "Submission uncertain"),
        (SUCCEEDED, "Succeeded"),
        (FAILED, "Failed"),
        (MANUAL_INTERVENTION_REQUIRED, "Manual intervention required"),
        (INTERRUPTED, "Interrupted"),
        (CANCELLED, "Cancelled"),
    ]
    # Every state an UpdateJob can be found in that is NOT one of these
    # means "still doing something" -- used by the active_lock
    # constraint below and by any future concurrency check
    # (ARCHITECTURE_REPORT.md §18). A job's own daemon-side code is
    # responsible for eventually landing in one of these; nothing here
    # times a job out on its own.
    TERMINAL = frozenset({
        SUCCEEDED, FAILED, MANUAL_INTERVENTION_REQUIRED, INTERRUPTED, CANCELLED,
    })


class UpdateJob(models.Model):
    """One attempt to move this station from `installed_release_id` to
    `target_release_id`. Created (Phase B) the moment an operator
    clicks the future Update button; read (Phase A and Phase B alike)
    by /updates/'s status view so progress survives a Gunicorn
    restart -- see ARCHITECTURE_REPORT.md §6 for the full durability
    design this schema exists to support."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Both an FK (nullable, SET_NULL -- a deleted user account must
    # never break this row) AND an immutable text snapshot. The FK is
    # for convenient admin/UI linking while it's still valid; the
    # snapshot is the actual audit-trail fact ("who initiated this",
    # matching monitoring.models.emit_event's own existing convention
    # of naming the acting user by identity, not by a value a client
    # could spoof) and is never modified after creation.
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    initiated_by_username = models.CharField(max_length=150, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    installed_release_id = models.CharField(max_length=32)
    target_release_id = models.CharField(max_length=32)
    installed_commit = models.CharField(max_length=40)
    target_commit = models.CharField(max_length=40)

    state = models.CharField(max_length=32, choices=UpdateJobState.CHOICES, default=UpdateJobState.QUEUED)
    current_step = models.CharField(max_length=64, blank=True, default="")

    # Bounded, human-readable -- NOT the full log. The full per-cycle
    # progress log lives in a root-owned file under /run/isadoraair
    # while a job is in flight (Phase B); this field is what a status
    # view shows without needing to read that file, and it's what
    # survives if that file is gone (tmpfs, does not survive a
    # reboot -- see completed_log_snapshot below for the durable copy).
    progress_detail = models.CharField(max_length=500, blank=True, default="")

    # planner.Plan.to_serializable() output, captured once at planning
    # time and never recomputed for this job -- Phase B's executor
    # independently RE-derives the same plan from the same inputs and
    # compares fingerprints rather than trusting this snapshot as
    # authorization (ARCHITECTURE_REPORT.md §10) -- this field is the
    # historical record of what was approved, not a source of truth
    # for what to execute.
    plan_snapshot = models.JSONField(default=dict, blank=True)
    plan_fingerprint = models.CharField(max_length=64, blank=True, default="")

    failure_classification = models.CharField(max_length=64, blank=True, default="")
    failure_detail = models.TextField(blank=True, default="")
    requires_manual_intervention = models.BooleanField(default=False)

    # Reviewed-migration-approval workflow: populated only when the
    # protected executor's mechanical migration classifier finds one or
    # more non-additive operations. Root independently computes this
    # (see updatecenter_probe.build_probe_payload) and reports it inside
    # the SAME GET_JOB_STATUS response failure_classification/detail
    # already use -- this is one more optional key on an existing
    # message, not a new protocol action. Django never writes this
    # field; job_service._reconcile_response only ever copies what root
    # reported. Structure: {"release_id", "target_commit",
    # "manifest_sha256", "migration_plan_digest", "manual_operations":
    # [{"ref", "operation_index", "operation", "classification",
    # "detail"}, ...]}. See MigrationPlanApproval below and
    # docs/UPDATE_CENTER.md's "Reviewed migration approval" section.
    migration_plan_review = models.JSONField(null=True, blank=True, default=None)
    # Protected updater evidence for an exact, retryable migration prefix.
    # This is an audit mirror only; root-owned job state remains authority.
    migration_recovery = models.JSONField(null=True, blank=True, default=None)

    # Durable copy of the daemon's own log for this job, written once
    # at completion (success OR failure) -- the live, in-progress log
    # lives on tmpfs and does not survive a reboot; this field is what
    # lets a post-reboot /updates/ still show a truthful account of a
    # job that was interrupted by that very reboot.
    completed_log_snapshot = models.TextField(blank=True, default="")

    # Concurrency lock (§18): exactly one row may have active_lock=1 at
    # a time -- every OTHER row (terminal, per UpdateJobState.TERMINAL)
    # must have active_lock=None. Postgres's default unique-constraint
    # behavior excludes NULLs from the uniqueness check, so this is the
    # standard "at most one row matching a condition" pattern without
    # needing a partial/conditional index. Phase A never sets this
    # field to anything but its default (None) since Phase A never
    # creates a row at all; the constraint exists now so applying it
    # later doesn't require a second migration touching this table.
    active_lock = models.SmallIntegerField(null=True, blank=True, editable=False, default=None)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Update Job"
        verbose_name_plural = "Update Jobs"
        constraints = [
            models.UniqueConstraint(fields=["active_lock"], name="updatecenter_one_active_job"),
            models.CheckConstraint(
                condition=models.Q(active_lock__isnull=True) | models.Q(active_lock=1),
                name="updatecenter_active_lock_null_or_one",
            ),
        ]
        # Structured now so a later dedicated `can_update_isadoraair`
        # permission can be introduced without a redesign or a second
        # migration purely for the permission -- granted to no one by
        # default; nothing in Phase A checks for it (see views.py's
        # staff-or-superuser view gate, which is what Phase A actually
        # enforces). ARCHITECTURE_REPORT.md §5 / this task's §2.5.
        permissions = [
            ("can_update_isadoraair", "Can execute IsadoraAir updates"),
        ]

    def __str__(self):
        return f"{self.id} ({self.installed_release_id} -> {self.target_release_id}, {self.state})"

    @property
    def partial_prefix_recovery(self) -> dict | None:
        """The root-owned recovery evidence, only when it is genuinely an
        actionable, PROVEN partial prefix -- otherwise None.

        Everything shown is copied from the protected updater's own
        finalized evidence (executor._finalize_recovery_from_observation
        and the success path); nothing is inferred here. It is None for a
        successful job (its evidence covers the complete plan and permits
        no action), for unfinalized evidence (not proven), for any other
        classification, and for malformed evidence.
        """
        evidence = self.migration_recovery
        if self.state not in {UpdateJobState.FAILED, UpdateJobState.MANUAL_INTERVENTION_REQUIRED}:
            return None
        if (not isinstance(evidence, dict)
                or evidence.get("finalized") is not True
                or evidence.get("classification") != "UPDATER_OWNED_PARTIAL_PREFIX"
                or evidence.get("permitted_action") != "retry_same_exact_release"
                or evidence.get("authorization_source") not in {"not_required", "central", "local"}):
            return None
        plan = evidence.get("ordered_target_plan")
        prefix = evidence.get("successful_prefix")
        if (not isinstance(plan, list) or not isinstance(prefix, list) or not prefix
                or prefix != plan[:len(prefix)]):
            return None
        return evidence


class MigrationPlanApproval(models.Model):
    """Deprecated application audit mirror of a protected approval.

    Since r0092 the authoritative decision is a root-owned ApprovalStore
    record outside the application migration graph. The web view asks that
    protected authority to approve first and writes this row only afterward
    for compatibility/display. Neither updatecenter_probe nor the executor
    consults this model, which is essential when updatecenter.0003 (the
    migration creating this table) is itself part of the reviewed plan.

    migration_plan_digest is intentionally NOT unique by itself --
    target_release_id is part of the identity too, since two different
    releases could theoretically compute the same content-derived digest
    only in a cryptographic-collision scenario this system does not need
    to additionally special-case; scoping by release_id costs nothing and
    removes any ambiguity about which release an approval is for."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    target_release_id = models.CharField(max_length=32)
    target_commit = models.CharField(max_length=40)
    migration_plan_digest = models.CharField(max_length=64)

    # Human-readable copy of exactly what was reviewed, for the audit
    # trail and for rendering the approval's own detail page -- never
    # consulted by the executor for the actual go/no-go decision, only
    # migration_plan_digest is. Same shape as UpdateJob.migration_plan_
    # review's own "manual_operations" list.
    manual_operations_snapshot = models.JSONField(default=list, blank=True)

    # Provenance only (which stopped job this review was read from) --
    # nullable/SET_NULL since a source job may later be pruned from the
    # bounded root job-state retention window without invalidating the
    # approval itself (the approval's own digest is self-contained).
    source_job = models.ForeignKey(
        UpdateJob, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    # Immutable snapshot, same convention as UpdateJob.initiated_by_username
    # -- a deleted/renamed account must never erase who actually approved
    # this from the audit trail.
    approved_by_username = models.CharField(max_length=150, editable=False)
    approved_at = models.DateTimeField(auto_now_add=True)

    # The operator's own written justification -- required in practice by
    # the review view (blank=True at the model layer only so a future
    # data migration/fixture is not forced to invent one).
    reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-approved_at"]
        verbose_name = "Migration Plan Approval"
        verbose_name_plural = "Migration Plan Approvals"
        constraints = [
            models.UniqueConstraint(
                fields=["target_release_id", "migration_plan_digest"],
                name="updatecenter_one_approval_per_release_digest",
            ),
        ]

    def __str__(self):
        return f"{self.target_release_id} @ {self.migration_plan_digest[:12]} (approved by {self.approved_by_username})"
