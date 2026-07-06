from django.db import migrations

# These three indicator checks now color their paired parameter card
# instead of showing their own tile (see monitoring/templates/monitoring/
# dashboard.html's TX_INDICATOR_FOR_PARAM map) -- they keep running and
# alerting exactly as before, just hidden from the card grid.
FOLDED_IN_CHECK_NAMES = ["TX RF Indicator", "TX VSWR Indicator", "TX Temp Indicator"]


def hide_cards(apps, schema_editor):
    MonitorCheck = apps.get_model("monitoring", "MonitorCheck")
    MonitorCheck.objects.filter(name__in=FOLDED_IN_CHECK_NAMES).update(show_as_card=False)


def reverse_show_cards(apps, schema_editor):
    MonitorCheck = apps.get_model("monitoring", "MonitorCheck")
    MonitorCheck.objects.filter(name__in=FOLDED_IN_CHECK_NAMES).update(show_as_card=True)


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0003_monitorcheck_show_as_card_and_more"),
    ]

    operations = [
        migrations.RunPython(hide_cards, reverse_show_cards),
    ]
