"""Console / Remote DJ log display: the station-wide "Show played items"
switch on the UITheme singleton. Display-only; default Off keeps the
Coming Up table exactly as before."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0100_dst_fallback_replay_pass"),
    ]

    operations = [
        migrations.AddField(
            model_name="uitheme",
            name="show_played_items",
            field=models.BooleanField(
                default=False,
                help_text="Off: the Coming Up table drops items as they air. On: the "
                          "Console and Remote DJ keep this hour's already-aired items "
                          "(darker) and outline the item on air, above the upcoming "
                          "items. Display only -- it never changes what plays or what "
                          "can be reordered. Takes effect when the page is reloaded.",
            ),
        ),
    ]
