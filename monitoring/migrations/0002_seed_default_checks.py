from django.db import migrations

# Seeds the checks that make sense to have enabled on day one. Thresholds
# for transmitter_param/transmitter_indicator rows are placeholders from
# the Aquabroadcast COBALT manual's own parameter descriptions (pages
# 62-63) — tune them once real normal-operating ranges for this specific
# transmitter/site are known.
#
# No audio_silence row is seeded here on purpose: it depends on which
# Encoder.input_device groups exist, which is encoders app data, not
# something a monitoring migration should reach into. Add one via admin
# after this ships, using the same device slug shown in the Encoders
# section (encoders.services.encoder_manager._slug()).
SEED_CHECKS = [
    ("Playback Engine", "systemd", 0, {"systemd_unit": "isadoraair-engine.service"}),
    ("Gunicorn", "systemd", 1, {"systemd_unit": "isadoraair-gunicorn.service"}),
    ("nginx", "systemd", 2, {"systemd_unit": "nginx.service"}),
    ("StereoTool", "systemd", 3, {"systemd_unit": "stereotool.service"}),
    ("Stream Encoders", "systemd", 4, {"systemd_unit": "isadoraair-encoders.service"}),

    ("Disk: / (OS)", "disk", 5, {
        "disk_path": "/", "warning_threshold": 60.0, "critical_threshold": 75.0,
    }),
    ("Disk: /srv/isadoraair (library)", "disk", 6, {
        "disk_path": "/srv/isadoraair", "warning_threshold": 60.0, "critical_threshold": 75.0,
    }),

    ("CPU Usage", "cpu", 7, {"warning_threshold": 85.0, "critical_threshold": 95.0}),
    ("Memory Usage", "memory", 8, {"warning_threshold": 85.0, "critical_threshold": 95.0}),
    # thermal_zone_label left blank on purpose -- confirmed via direct
    # psutil.sensors_temperatures() check on this box that there's no
    # single stable top-level key across chips (coretemp/acpitz/nvme/
    # pch_cannonlake all appear); watching the max across every entry is
    # more robust than hardcoding one chip's label.
    ("CPU Temperature", "temperature", 9, {"warning_threshold": 75.0, "critical_threshold": 85.0}),

    ("TX Forward Power", "transmitter_param", 10, {
        "transmitter_parameter": "psu.fwd_power", "threshold_direction": "below",
        "warning_threshold": 50.0, "critical_threshold": 10.0,
    }),
    ("TX Reverse Power", "transmitter_param", 11, {
        "transmitter_parameter": "psu.rev_power", "threshold_direction": "above",
        "warning_threshold": 5.0, "critical_threshold": 15.0,
    }),
    ("TX VSWR", "transmitter_param", 12, {
        "transmitter_parameter": "psu.vswr", "threshold_direction": "above",
        "warning_threshold": 1.5, "critical_threshold": 2.0,
    }),
    ("TX PA Temperature", "transmitter_param", 13, {
        "transmitter_parameter": "psu.pa_temperature", "threshold_direction": "above",
        "warning_threshold": 60.0, "critical_threshold": 75.0,
    }),
    ("TX Fan 1 Speed", "transmitter_param", 14, {
        "transmitter_parameter": "psu.fan_speed_measured_fan1", "threshold_direction": "below",
        "warning_threshold": 500.0, "critical_threshold": 200.0,
    }),
    ("TX RF Interlock", "transmitter_indicator", 15, {
        "transmitter_indicator": "status.rf_interlock", "fault_values": "True",
    }),
    ("TX RF Indicator", "transmitter_indicator", 16, {
        "transmitter_indicator": "status.indicator.rf", "fault_values": "R", "warn_values": "O,B",
    }),
    ("TX VSWR Indicator", "transmitter_indicator", 17, {
        "transmitter_indicator": "status.indicator.vswr", "fault_values": "R", "warn_values": "O",
    }),
    ("TX Temp Indicator", "transmitter_indicator", 18, {
        "transmitter_indicator": "status.indicator.temp", "fault_values": "R", "warn_values": "O",
    }),
]


def seed_checks(apps, schema_editor):
    MonitorCheck = apps.get_model("monitoring", "MonitorCheck")
    for name, kind, sort_order, extra in SEED_CHECKS:
        MonitorCheck.objects.get_or_create(
            name=name,
            defaults={"kind": kind, "sort_order": sort_order, **extra},
        )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_checks, reverse_noop),
    ]
