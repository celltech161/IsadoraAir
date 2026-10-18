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
# * Name collision (the name is unique): if DEFAULT_NAME belongs to an
#   unrelated check, that row is never touched or renamed; the check is created
#   under the deterministic FALLBACK_NAME (the unit name itself). Only if that
#   is ALSO held by an unrelated row is nothing created --
#   `manage.py provision_active_message_worker_check` (or Django admin) then
#   adds it under a name the operator chooses.
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
MAX_SORT_ORDER = 32767


def provision_active_message_worker_check(apps, schema_editor):
    MonitorCheck = apps.get_model("monitoring", "MonitorCheck")
    checks = MonitorCheck.objects.using(schema_editor.connection.alias)
    if checks.filter(kind="systemd", systemd_unit=UNIT).exists():
        return
    taken = set(checks.filter(name__in=(DEFAULT_NAME, FALLBACK_NAME)).values_list("name", flat=True))
    name = next((candidate for candidate in (DEFAULT_NAME, FALLBACK_NAME) if candidate not in taken), None)
    if name is None:
        return
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
