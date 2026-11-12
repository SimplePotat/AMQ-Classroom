from django.db import models


class Profile(models.Model):
    name = models.CharField(max_length=24)
    name_key = models.CharField(max_length=80, unique=True)
    mal_username = models.CharField(max_length=32, blank=True)
    password = models.CharField(max_length=160)
    avatar = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class Room(models.Model):
    state = models.JSONField(default=dict)


class CachedResource(models.Model):
    key = models.CharField(max_length=200, unique=True)
    value = models.JSONField()
    expires_at = models.FloatField()


class OpeningsResource(models.Model):
    """Provider-owned catalog/details, independent of AnimeThemes cache entries."""
    key = models.CharField(max_length=200, unique=True)
    value = models.JSONField()
    expires_at = models.FloatField()


class GameResult(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    result = models.JSONField()
