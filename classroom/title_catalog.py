"""Public anime names from already populated MAL caches; never fetch providers."""
import threading
import time

from django.db import connection
from django.db.models import Q

from .engine import normalize
from .models import CachedResource

MAX_TITLES = 50_000
CACHE_SECONDS = 30
LOCK = threading.Lock()
_snapshot = None


def invalidate():
    global _snapshot
    with LOCK:
        _snapshot = None


def titles():
    """Share one bounded snapshot across LAN clients, outside the room lock.

    Expired provider metadata still contains useful names. Only title strings
    leave this module: no cache keys, usernames, list statuses, or media links.
    """
    global _snapshot
    database = str(connection.settings_dict["NAME"])
    with LOCK:
        if _snapshot and _snapshot[0] == database and _snapshot[1] > time.monotonic():
            return _snapshot[2]
        names = {}

        def add(value):
            if not isinstance(value, str):
                return
            value = value.strip()
            if (not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value)
                    or any(0xD800 <= ord(c) <= 0xDFFF for c in value) or not normalize(value)):
                return
            key = value.casefold()
            if key not in names and len(names) < MAX_TITLES:
                names[key] = value

        entries = CachedResource.objects.filter(
            Q(key__startswith="mal:") | Q(key__startswith="anime:")
        ).order_by("key").values_list("key", "value")
        for key, value in entries.iterator(chunk_size=50):
            if key.startswith("mal:"):
                if isinstance(value, list):
                    for entry in value:
                        if (isinstance(entry, dict) and type(entry.get("anime_id")) is int
                                and entry["anime_id"] > 0):
                            add(entry.get("anime_title"))
            else:
                mal_id = key.removeprefix("anime:").removeprefix("mal:")
                if not mal_id.isascii() or not mal_id.isdecimal() or not mal_id.strip("0"):
                    continue
                records = value if isinstance(value, list) else [value]
                for anime in records:
                    if not isinstance(anime, dict):
                        continue
                    add(anime.get("name"))
                    aliases = anime.get("animesynonyms")
                    if isinstance(aliases, list):
                        for alias in aliases:
                            if isinstance(alias, dict):
                                add(alias.get("text"))
            if len(names) >= MAX_TITLES:
                break
        result = sorted(names.values(), key=lambda name: (name.casefold(), name))
        _snapshot = (database, time.monotonic() + CACHE_SECONDS, result)
        return result
