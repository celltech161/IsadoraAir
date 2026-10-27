# Roadmap 2.27 Phase D -- the hour-refill install gate.
#
# Removes the Phase A check constraint that forced HourRefillPolicy.dry_run_only
# to stay True, and makes the field editable in admin. It changes no row:
# existing policies keep dry_run_only=True, nothing is enabled, armed or
# invoked. Installation still also needs settings.HOUR_REFILL_INSTALL_ARMED.
#
# Both operations are deliberately outside the Update Center's mechanical
# allowlist (manual), so each station needs a Tier 3 reviewed approval for the
# exact release plan: applying this removes a safety barrier. Reverse
# (AddConstraint) succeeds only while every row has dry_run_only=True -- set
# observation first, then migrate back. See
# docs/roadmap/2_27_PHASE_D_OPERATIONAL_CONTRACT.md section 3.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0095_hour_refill_install'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='hourrefillpolicy',
            name='hour_refill_policy_dry_run_only_phase_a',
        ),
        migrations.AlterField(
            model_name='hourrefillpolicy',
            name='dry_run_only',
            field=models.BooleanField(default=True, help_text="While checked, the hour-refill runner only reports advisory proposals and never writes to a log. Installation also requires the station's reviewed gate migration and HOUR_REFILL_INSTALL_ARMED in its .env.", verbose_name='Observation only (dry run)'),
        ),
    ]
