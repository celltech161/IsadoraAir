"""Durable UpdateJob schema -- [P0] 1.1 Phase A.

Nothing in Phase A creates a row here. This model exists now so its
migration exists now, additive and simple, safe to review and apply
manually the one time Update Center support is first installed on a
station -- see this app's own module docstring in __init__.py and
docs/UPDATE_CENTER.md's "bootstrap limitation" section for why that
one manual step can never be automated away by the very feature it
bootstraps.

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
    except by not existing (no UpdateJob row is ever created by
    Phase A code) -- Phase B's daemon/executor is the only thing that
    will ever write a transition. Kept here, not as a bare tuple, so
    both the model's `choices=` and any future planner/executor code
    import the SAME names rather than risking a typo'd string drifting
    from the model's own choices list."""
    QUEUED = "queued"
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"

    CHOICES = [
        (QUEUED, "Queued"),
        (PLANNED, "Planned"),
        (RUNNING, "Running"),
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
