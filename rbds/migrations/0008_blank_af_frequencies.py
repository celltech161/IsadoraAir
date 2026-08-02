from django.db import migrations


def blank_af_frequencies(apps, schema_editor):
    """AF transmission is now hard-blocked at runtime (rbds_manager.py)
    because mec_af()'s list encoding doesn't match the real RDS Method
    A frame structure -- see uecp.py's mec_af docstring. Defensively
    blank any stored value too, so nothing relies on a pre-2026-08-02
    row that predates RBDSConfig.clean()'s validation."""
    RBDSConfig = apps.get_model("rbds", "RBDSConfig")
    RBDSConfig.objects.exclude(af_frequencies_mhz="").update(af_frequencies_mhz="")


class Migration(migrations.Migration):

    dependencies = [
        ("rbds", "0007_rbdsconfig_ecc"),
    ]

    operations = [
        migrations.RunPython(blank_af_frequencies, migrations.RunPython.noop),
    ]
