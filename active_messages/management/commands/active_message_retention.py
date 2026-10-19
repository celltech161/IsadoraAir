"""Reconcile active-message publication artifacts; reclaim only what is provably safe (iPortal D3E-C).

    manage.py active_message_retention                     # read-only report (metadata)
    manage.py active_message_retention --deep              # ... and re-hash bytes (slow)
    manage.py active_message_retention --sweep [--apply]   # unreferenced leftovers (dry-run without --apply)
    manage.py active_message_retention --retire <execution UUID> ... [--apply]

Nothing is removed without --apply, and nothing is ever selected by age
alone: the sweep removes only provably unreferenced artifacts past a grace
period (interrupted-publication leftovers in the reserved namespace, unbound
aggregate bytes, the remaining bytes of retired publications -- and, with
--include-production, Production's own sweeps); --retire acts only on
executions named here. No path is ever accepted. There is no timer behind
this command.

Authority: a TRUSTED MAINTENANCE operation. Whoever can run it already holds
the application's database credentials (the station account's .env) and
could change these rows directly, so the command asserts no Django identity:
it takes no username, and a retirement is recorded as the system (detail.via
"maintenance_command"), never as a person.

Exit status: 0 when every requested action succeeded (findings alone are not
failures); 1 for an operational failure (database, storage, a failed removal).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from active_messages import reconcile, retention
from production.services import reconcile as production_reconcile

MAX_RETIRE = 50


def _grace_hours(value) -> float:
    hours = float(value)
    if not hours >= 1:
        raise ValueError
    return hours


class Command(BaseCommand):
    help = ("Report on active-message publication artifacts; with --sweep / --retire and --apply, reclaim only "
            "provably safe ones. Read-only by default.")

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--sweep", action="store_true",
                          help="Reclaim unreferenced leftovers (dry-run unless --apply).")
        mode.add_argument("--retire", nargs="+", metavar="EXECUTION_UUID",
                          help=f"Retire these executions' publications (at most {MAX_RETIRE}; dry-run unless --apply).")
        parser.add_argument("--apply", action="store_true", help="Actually reclaim what is reported as eligible.")
        parser.add_argument("--deep", action="store_true", help="Report mode: also re-hash bytes (slow).")
        parser.add_argument("--grace-hours", type=_grace_hours, default=retention.DEFAULT_GRACE.total_seconds() / 3600,
                            help="Sweep: only touch leftovers idle at least this long (minimum 1; default 24).")
        parser.add_argument("--limit", type=int, default=retention.DEFAULT_LIMIT,
                            help=f"Rows per artifact class (1..{retention.MAX_LIMIT}; default {retention.DEFAULT_LIMIT}).")
        parser.add_argument("--include-production", action="store_true",
                            help="Sweep: also run Production's own reconcile sweeps (parts, orphan media, work).")
        parser.add_argument("--no-attention", action="store_true",
                            help="Report mode: do not raise Monitoring events for integrity findings.")

    def handle(self, *args, **options):
        if options["apply"] and not (options["sweep"] or options["retire"]):
            raise CommandError("--apply needs --sweep or --retire")
        if not 1 <= options["limit"] <= retention.MAX_LIMIT:
            raise CommandError(f"--limit must be from 1 to {retention.MAX_LIMIT}")
        grace = timedelta(hours=options["grace_hours"])
        try:
            if options["retire"]:
                return self.retire(options)
            if options["sweep"]:
                return self.sweep(options, grace)
            return self.report(options, grace)
        except retention.RetentionUnavailable as exc:
            raise CommandError(f"storage unavailable: {exc.code}") from None
        except DatabaseError as exc:
            raise CommandError(f"database unavailable: {type(exc).__name__}") from None

    # -- report -----------------------------------------------------------------------------
    def report(self, options, grace):
        report = reconcile.reconcile(deep=options["deep"], limit=options["limit"], grace=grace)
        self.stdout.write(f"Active-message artifacts ({'deep' if report.deep else 'metadata'} check; read-only)")
        self.stdout.write("inspected: " + self._counts(report.inspected))
        self.stdout.write("healthy:   " + self._counts(report.healthy))
        self.stdout.write("findings by category: " + self._counts(report.by("category")))
        if report.truncated:
            self.stdout.write(self.style.WARNING("truncated (raise --limit): " + ", ".join(sorted(report.truncated))))
        for finding in sorted(report.findings, key=lambda item: (-("info", "warning", "critical").index(item.severity),
                                                                 item.code, item.identity)):
            style = {"critical": self.style.ERROR, "warning": self.style.WARNING}.get(finding.severity, str)
            self.stdout.write(style(f"  [{finding.severity}] {finding.code} {finding.artifact} {finding.identity}: "
                                    f"{finding.evidence} ({finding.category}; {finding.eligibility})"
                                    + (f" -> {finding.action}" if finding.action else "")))
        if not options["no_attention"]:
            emitted = reconcile.attention(report)
            if emitted:
                self.stdout.write(f"Monitoring attention raised for {emitted} condition code(s).")

    # -- sweep ------------------------------------------------------------------------------
    def sweep(self, options, grace):
        apply, limit = options["apply"], options["limit"]
        self.stdout.write("" if apply else self.style.WARNING("DRY RUN -- pass --apply to reclaim anything."))
        reports = [("interrupted publications", retention.sweep_publication_namespace(apply=apply, grace=grace,
                                                                                      limit=limit)),
                   ("retired publication bytes", retention.finish_retirements(apply=apply, limit=limit)),
                   ("unbound aggregate bytes", retention.purge_unbound_aggregates(apply=apply, grace=grace,
                                                                                  limit=limit))]
        failures = sum(len(report.failures) for _label, report in reports)
        for label, report in reports:
            self._summary(label, report)
        if options["include_production"]:
            for label, sweep in (("production stale parts", production_reconcile.sweep_stale_parts),
                                 ("production orphan media", production_reconcile.sweep_orphan_media),
                                 ("production stale work", production_reconcile.sweep_stale_work)):
                result = sweep(grace=grace, apply=apply)
                verb = "removed" if apply else "would remove"
                self.stdout.write(f"{label}: {verb} {len(result.removed)}, kept (too young) {result.kept_young}, "
                                  f"unexpected {len(result.unexpected)}, errors {len(result.errors)}")
                failures += len(result.errors)
        if failures:
            raise CommandError(f"{failures} cleanup(s) failed; nothing was reported as reclaimed for them")

    # -- retire -----------------------------------------------------------------------------
    def retire(self, options):
        identities = options["retire"]
        if len(identities) > MAX_RETIRE:
            raise CommandError(f"at most {MAX_RETIRE} executions per run")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("DRY RUN -- pass --apply to retire (a trusted maintenance action)."))
        report = retention.CleanupReport(apply=options["apply"])
        for identity in identities:
            report.add(retention.retire_publication(identity, apply=options["apply"]))
        self._summary("publication retirement", report)
        if report.failures:
            raise CommandError(f"{len(report.failures)} retirement(s) left bytes behind; run --sweep --apply")

    # -- output -----------------------------------------------------------------------------
    @staticmethod
    def _counts(counter) -> str:
        return ", ".join(f"{key} {value}" for key, value in sorted(counter.items())) or "none"

    def _summary(self, label, report):
        counts = {action: report.count(action) for action in
                  ("retired", "purged", "removed", "eligible", "already_retired", "kept", "refused", "failed")}
        self.stdout.write(f"{label}: " + ", ".join(f"{key} {value}" for key, value in counts.items() if value)
                          + (f"; reclaimed {report.bytes_reclaimed} bytes" if report.apply
                             else f"; reclaimable (not reclaimed) {report.bytes_reclaimable} bytes"))
        for outcome in report.outcomes:
            if outcome.action in ("kept",):
                continue
            reasons = f" ({', '.join(outcome.reasons)})" if outcome.reasons else ""
            style = self.style.ERROR if outcome.action == "failed" else str
            self.stdout.write(style(f"  {outcome.action}: {outcome.artifact} {outcome.identity}{reasons}"))
