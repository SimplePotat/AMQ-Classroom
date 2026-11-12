from django.db import migrations, models


def move_cache(apps, schema_editor, source_name, target_name):
    source = apps.get_model("classroom", source_name).objects.using(schema_editor.connection.alias)
    target = apps.get_model("classroom", target_name).objects.using(schema_editor.connection.alias)
    entries = source.filter(key__startswith="openings:")
    for entry in entries.iterator(chunk_size=500):
        target.update_or_create(key=entry.key, defaults={
            "value": entry.value, "expires_at": entry.expires_at,
        })
    entries.delete()


def forwards(apps, schema_editor):
    move_cache(apps, schema_editor, "CachedResource", "OpeningsResource")


def backwards(apps, schema_editor):
    move_cache(apps, schema_editor, "OpeningsResource", "CachedResource")


class Migration(migrations.Migration):
    dependencies = [("classroom", "0001_initial")]
    operations = [
        migrations.CreateModel(name="OpeningsResource", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("key", models.CharField(max_length=200, unique=True)),
            ("value", models.JSONField()),
            ("expires_at", models.FloatField()),
        ]),
        migrations.RunPython(forwards, backwards),
    ]
