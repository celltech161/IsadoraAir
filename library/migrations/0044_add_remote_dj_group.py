from django.db import migrations

# Remote DJ over WebRTC (see /home/jreed/.claude/plans/warm-zooming-rose.md):
# remote DJs authenticate with their own real Django account, but the
# studio-mic-adjacent capability shouldn't be handed to every dashboard
# login by default -- api_remote_dj_token gates on membership in this
# group rather than "any logged-in user." Membership itself is assigned
# by hand in the admin per DJ, same as any other Group.


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name="remote_dj")


def reverse_delete(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="remote_dj").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0043_remove_analysisconfig_waveform_floor_db_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_group, reverse_delete),
    ]
