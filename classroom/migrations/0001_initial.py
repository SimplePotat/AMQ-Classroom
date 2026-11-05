from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(name="Profile", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=24)),
            ("name_key", models.CharField(max_length=80, unique=True)),
            ("mal_username", models.CharField(blank=True, max_length=32)),
            ("password", models.CharField(max_length=160)),
            ("avatar", models.PositiveSmallIntegerField(default=0)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.CreateModel(name="Room", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("state", models.JSONField(default=dict)),
        ]),
        migrations.CreateModel(name="CachedResource", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("key", models.CharField(max_length=200, unique=True)),
            ("value", models.JSONField()),
            ("expires_at", models.FloatField()),
        ]),
        migrations.CreateModel(name="GameResult", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("result", models.JSONField()),
        ]),
    ]
