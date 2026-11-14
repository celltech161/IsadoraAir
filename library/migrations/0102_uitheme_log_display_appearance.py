"""Console / Remote DJ log display appearance: how dark played rows are
(UITheme.played_item_darkening, default 0.55 -- the previous fixed value) and
the outline colour of the row on air (UITheme.current_item_outline_color,
default opaque white -- the previous fixed value). Display only."""
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0101_uitheme_show_played_items"),
    ]

    operations = [
        migrations.AddField(
            model_name="uitheme",
            name="played_item_darkening",
            field=models.FloatField(
                default=0.55,
                validators=[django.core.validators.MinValueValidator(0.0),
                            django.core.validators.MaxValueValidator(1.0)],
                help_text="How dark already-aired rows are when Show played items is on: a black "
                          "overlay of this opacity over each row's own category colour. 0% leaves "
                          "the category colour untouched, 100% makes the row black. Takes effect "
                          "when the page is reloaded.",
            ),
        ),
        migrations.AddField(
            model_name="uitheme",
            name="current_item_outline_color",
            field=models.CharField(
                default="rgba(255, 255, 255, 1.00)", max_length=50,
                validators=[django.core.validators.RegexValidator(
                    r"^(#[0-9a-fA-F]{3,8}|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*(0|1|0?\.\d+|1\.0+)\s*)?\))$",
                    "Enter a hex colour or an rgb()/rgba() value.")],
                help_text="Outline drawn around the row(s) actually on air when Show played items "
                          "is on (default opaque white). Colour and opacity.",
            ),
        ),
    ]
