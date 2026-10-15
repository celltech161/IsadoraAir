"""iPortal Phase D3A: exact produced-content identity for active messages.

1. ``ActiveBroadcastMessage.content_type`` (nullable for the backfill).
2. Native COMPATIBILITY content types, created if absent -- the two broad
   kinds D1-D2B knew -- with D1's authority unchanged for them (activation
   and recall of an ACTIVE message need control: no producer self-publish,
   not recallable; replay: the policy default):

       urgent         -> ``urgent_pa``       (delivery mode urgent)
       public_address -> ``public_address``  (delivery mode public_address)

3. Every existing message is backfilled from its frozen ``kind``,
   deterministically. Nothing is imported from legacy OGRemote (no category
   table, users, assignments, submissions or files).

Self-contained (historical models only). The schema tightening -- NOT NULL
and the new one-ACTIVE-per-(producer, content type) uniqueness -- is
0004, in its own transaction after these rows commit.

This migration and 0004 are Phase D3A's manual operations for the protected
updater (RunPython; AlterField/RemoveConstraint/AddConstraint): at release
time they need a migration-authorization companion or local approval
(docs/UPDATE_CENTER.md, "Trusted companion migration authorization").
"""
import django.db.models.deletion
from django.db import migrations, models

COMPATIBILITY_TYPES = (
    # (kind, key, name, description)
    ("urgent", "urgent_pa", "Urgent PA",
     "Urgent messages (native compatibility type for the D1 urgent kind)."),
    ("public_address", "public_address", "Public Address",
     "Public-address messages (native compatibility type for the D1 public-address kind)."),
)


def forwards(apps, schema_editor):
    ContentType = apps.get_model("produced_content", "ProducedContentType")
    Message = apps.get_model("active_messages", "ActiveBroadcastMessage")
    for sort_order, (kind, key, name, description) in enumerate(COMPATIBILITY_TYPES):
        content_type = ContentType.objects.filter(key=key).first()
        if content_type is None:
            content_type = ContentType.objects.create(
                key=key, name=name, description=description, enabled=True, sort_order=sort_order,
                delivery_mode=kind, target_category=None, output_mode="", recipe_key="", artist_tag="",
                recallable=False, producer_self_publish=False, replay_interval_seconds=None)
        elif content_type.delivery_mode != kind:
            raise RuntimeError(f"produced content type {key!r} exists with delivery mode "
                               f"{content_type.delivery_mode!r}; the {kind} backfill needs {kind!r}")
        Message.objects.filter(kind=kind, content_type__isnull=True).update(content_type=content_type)
    if Message.objects.filter(content_type__isnull=True).exists():
        raise RuntimeError("an active message of an unknown kind cannot be backfilled")


class Migration(migrations.Migration):

    dependencies = [
        ("active_messages", "0002_playout_correlation"),
        ("produced_content", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="activebroadcastmessage",
            name="content_type",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="active_broadcast_messages",
                to="produced_content.producedcontenttype"),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
