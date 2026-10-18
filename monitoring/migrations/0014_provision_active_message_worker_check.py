# D3E-B -- provision the Monitoring check for isadoraair-active-message-worker.
#
# Configuration only (no schema change): one ordinary "Systemd Service"
# MonitorCheck of the exact unit, so an unattended release that installs and
# starts the worker also monitors it. monitoring/services/probes.py recognizes
# that unit and adds its heartbeat readiness.
#
# * An existing systemd check of the exact unit -- under any name, enabled or
#   not -- is the operator's and is left exactly as it is (nothing is read back
#   or rewritten).
# * Otherwise exactly one is created: enabled, the model's default debounce and
#   notification settings, shown as a card, sorted after the existing systemd
#   checks.
# * Names (MonitorCheck.name is unique): the check is created under the first
#   free name of a fixed, bounded series -- DEFAULT_NAME, then FALLBACK_NAME
#   (the unit name), then "DEFAULT_NAME (2)" ... "DEFAULT_NAME (MAX_SUFFIX)".
#   A row already holding a name is never touched, renamed or adopted: only
#   kind + systemd_unit identify the worker's check.
# * If every candidate is taken the migration FAILS (CandidateNamesExhausted,
#   rolling back): it never succeeds without the check. Nothing swallows a
#   database error either -- a concurrent unique-name collision also fails it.
# * Re-running it can never create a second check for the unit.
# * Reverse is a no-op: a check an operator may have customized since is never
#   deleted by a migration.
#
# It reads and writes MonitorCheck rows only -- never systemd, a service or
# Active Messages. RunPython is outside Update Center's automatic migration
# allowlist, so this operation is classified "manual"; a release carrying it
# authorizes exactly this operation through its trusted
# deploy/migration_authorizations/<release>.json companion (see
# docs/ACTIVE_MESSAGES.md, "Protected deployment").
from django.db import migrations

UNIT = "isadoraair-active-message-worker.service"
DEFAULT_NAME = "Active Message Worker"
FALLBACK_NAME = UNIT
MAX_SUFFIX = 100                    # "Active Message Worker (2)" .. "(100)": 101 candidates in all
MAX_NAME_LENGTH = 100               # MonitorCheck.name max_length
MAX_SORT_ORDER = 32767
CANDIDATE_NAMES = (DEFAULT_NAME, FALLBACK_NAME,
                   *(f"{DEFAULT_NAME} ({number})" for number in range(2, MAX_SUFFIX + 1)))


class CandidateNamesExhausted(RuntimeError):
    pass


if any(len(candidate) > MAX_NAME_LENGTH for candidate in CANDIDATE_NAMES):
    raise RuntimeError("a candidate Monitoring check name exceeds MonitorCheck.name's length")


def provision_active_message_worker_check(apps, schema_editor):
    MonitorCheck = apps.get_model("monitoring", "MonitorCheck")
    checks = MonitorCheck.objects.using(schema_editor.connection.alias)
    if checks.filter(kind="systemd", systemd_unit=UNIT).exists():
        return
    taken = set(checks.filter(name__in=CANDIDATE_NAMES).values_list("name", flat=True))
    name = next((candidate for candidate in CANDIDATE_NAMES if candidate not in taken), None)
    if name is None:
        raise CandidateNamesExhausted(
            f"cannot provision the Monitoring check for {UNIT}: every candidate name "
            f"({DEFAULT_NAME!r}, {FALLBACK_NAME!r}, {DEFAULT_NAME!r} (2)..({MAX_SUFFIX})) is used by another "
            f"check. Rename one of those checks, or add a Systemd Service check for {UNIT} under any free name "
            f"in Django admin (Monitoring > Monitor Checks), then retry the update.")
    highest = max(checks.filter(kind="systemd").values_list("sort_order", flat=True), default=0)
    checks.create(
        name=name, kind="systemd", systemd_unit=UNIT, enabled=True,
        sort_order=min(highest + 1, MAX_SORT_ORDER),
        consecutive_failures_required=2, notify_on_warning=True, notify_on_critical=True, show_as_card=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0013_backup_recovery_assurance_check"),
    ]

    operations = [
        migrations.RunPython(provision_active_message_worker_check, migrations.RunPython.noop),
    ]
