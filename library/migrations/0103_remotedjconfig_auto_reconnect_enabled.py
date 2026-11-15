"""Roadmap 2.17 Package B: optional automatic fresh-session Remote DJ reconnect.

Additive: a BooleanField with default False, so every existing station keeps
today's behaviour (a lost session stays disconnected until Connect is pressed).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0102_uitheme_log_display_appearance"),
    ]

    operations = [
        migrations.AddField(
            model_name="remotedjconfig",
            name="auto_reconnect_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Roadmap 2.17 Package B -- separate from the temporary same-session recovery above: when a "
                    "connected Remote DJ session has definitively ENDED through a connection loss (not a "
                    "Disconnect, Kick, revocation, engine shutdown or Emergency Mute), the DJ's browser may "
                    "start a brand-new session on its own -- a new sign-in token, new connection and new "
                    "microphone setup, with the Remote Mic Off -- up to 3 attempts, with a Cancel button. Off: a "
                    "lost session stays disconnected until the DJ presses Connect (unchanged behaviour). Checked "
                    "again before every attempt, so turning it off stops a reconnect already in progress. "
                    "Requires Remote DJ itself (Master switch) to be on."
                ),
                verbose_name="Enable automatic Remote DJ reconnect",
            ),
        ),
    ]
