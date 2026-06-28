import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Collapse the Clock/Rotation/RotationSlot weighted-pool hierarchy
    into a single Rotation-of-Category-slots model, and add Playlist /
    PlaylistItem as the curated-track alternative. ScheduleBlock now
    points at either a Rotation or a Playlist (check-constrained).

    All affected tables were empty before this migration runs; no
    rows are preserved. Operation order matters: drop unique_together
    *before* the fields it references go away."""

    dependencies = [
        ('library', '0009_move_audio_models_to_hardware_app'),
    ]

    operations = [
        # 1. Tear ScheduleBlock loose from Clock so we can delete Clock.
        migrations.RemoveField(
            model_name='scheduleblock',
            name='clock',
        ),

        # 2. Drop ClockSlot. unique_together must go first because the
        #    fields it references are about to disappear.
        migrations.AlterUniqueTogether(
            name='clockslot',
            unique_together=set(),
        ),
        migrations.DeleteModel(name='ClockSlot'),

        # 3. Drop Clock now that nothing references it.
        migrations.DeleteModel(name='Clock'),

        # 4. Reshape RotationSlot: drop (rotation, category) unique,
        #    drop weight + active, add position, add new unique.
        migrations.AlterUniqueTogether(
            name='rotationslot',
            unique_together=set(),
        ),
        migrations.RemoveField(model_name='rotationslot', name='active'),
        migrations.RemoveField(model_name='rotationslot', name='weight'),
        migrations.AddField(
            model_name='rotationslot',
            name='position',
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AlterUniqueTogether(
            name='rotationslot',
            unique_together={('rotation', 'position')},
        ),
        migrations.AlterModelOptions(
            name='rotationslot',
            options={'ordering': ['rotation', 'position']},
        ),

        # 5. Create Playlist and PlaylistItem.
        migrations.CreateModel(
            name='Playlist',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['name']},
        ),
        migrations.CreateModel(
            name='PlaylistItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('position', models.PositiveIntegerField(db_index=True, default=0)),
                ('playlist', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='library.playlist')),
                ('track', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='playlist_items', to='library.track')),
            ],
            options={
                'ordering': ['playlist', 'position'],
                'unique_together': {('playlist', 'position')},
            },
        ),

        # 6. Re-add ScheduleBlock content FKs (nullable, both) and
        #    constrain exactly one.
        migrations.AddField(
            model_name='scheduleblock',
            name='rotation',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='schedule_blocks', to='library.rotation',
            ),
        ),
        migrations.AddField(
            model_name='scheduleblock',
            name='playlist',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='schedule_blocks', to='library.playlist',
            ),
        ),
        migrations.AddConstraint(
            model_name='scheduleblock',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(rotation__isnull=False, playlist__isnull=True)
                    | models.Q(rotation__isnull=True, playlist__isnull=False)
                ),
                name='scheduleblock_exactly_one_of_rotation_or_playlist',
            ),
        ),
    ]
