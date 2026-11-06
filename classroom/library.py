"""MAL/AnimeThemes song pools, with persistent caching and an offline catalog."""
import json
import logging
import random
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from django.conf import settings
from django.db import close_old_connections
from .engine import GameError, room_state
from .models import CachedResource
from . import metadata
from .metadata import get_json

LOG = logging.getLogger(__name__)
RANDOM = random.SystemRandom()
API = "https://api.animethemes.moe"
MAX_MAL_ENTRIES = 20_000
MAX_CANDIDATES = 20_000
MAX_THEME_CANDIDATES = 80
ANIME_BATCH_SIZE = 10
ANIME_PAGE_SIZE = 15
MAX_ANIME_PAGES = 10
ANIME_TTL = 30 * 86400
MISSING_ANIME_TTL = 86400
CACHE_LOCK = threading.RLock()
INCLUDE = "animethemes.animethemeentries.videos,animethemes.song.artists,animesynonyms,resources"
STARTER = [
    (1, "Cowboy Bebop"), (1535, "Death Note"), (5114, "Fullmetal Alchemist: Brotherhood"),
    (16498, "Shingeki no Kyojin"), (20, "Naruto"), (21, "One Piece"),
    (30276, "One Punch Man"), (38000, "Kimetsu no Yaiba"), (4224, "Toradora!"),
    (20583, "Haikyuu!!"), (9253, "Steins;Gate"), (11061, "Hunter x Hunter (2011)"),
]


@contextmanager
def cache_work():
    # Recheck after acquiring the lock so prefetch and preparation share misses.
    # No database transaction or room lock is held during provider requests.
    while not CACHE_LOCK.acquire(timeout=0.1):
        metadata.checkpoint()
    try:
        metadata.checkpoint()
        yield
    finally:
        CACHE_LOCK.release()


def cached(key, seconds, fetch):
    metadata.checkpoint()
    entry = CachedResource.objects.filter(key=key, expires_at__gt=time.time()).first()
    if entry:
        return entry.value
    with cache_work():
        entry = CachedResource.objects.filter(key=key, expires_at__gt=time.time()).first()
        if entry:
            return entry.value
        value = fetch()
        CachedResource.objects.update_or_create(key=key, defaults={"value": value, "expires_at": time.time() + seconds})
        return value


def mal_list(username):
    def fetch():
        entries = []
        for offset in range(0, MAX_MAL_ENTRIES + 1, 300):
            data = get_json(f"https://myanimelist.net/animelist/{quote(username, safe='')}/load.json",
                            {"status": 7, "offset": offset})
            if not isinstance(data, list):
                raise GameError(f"MAL list for {username} is private, missing, or temporarily unavailable.")
            if len(data) > 300 or len(entries) + len(data) > MAX_MAL_ENTRIES:
                raise GameError(f"MAL list for {username} is too large to import.")
            # Do not persist unrelated profile metadata or unbounded fields
            # from each page; the quiz only needs these three scalar values.
            for entry in data:
                entry = entry if isinstance(entry, dict) else {}
                mal_id, title, status = entry.get("anime_id"), entry.get("anime_title"), entry.get("status")
                entries.append({"anime_id": mal_id if type(mal_id) is int and mal_id > 0 else None,
                                "anime_title": title if isinstance(title, str) and 1 <= len(title) <= 500 else None,
                                "status": status if type(status) is int else None})
            if len(data) < 300:
                return entries
        raise GameError(f"MAL list for {username} is too large to import.")
    return cached(f"mal:{username.casefold()}", 7 * 86400, fetch)


def _fetch_anime_batch(mal_ids):
    found = {mal_id: [] for mal_id in mal_ids}
    requested = {str(mal_id): mal_id for mal_id in mal_ids}
    seen = set()
    for page in range(1, MAX_ANIME_PAGES + 1):
        data = get_json(API + "/anime", {
            "filter[has]": "resources", "filter[site]": "MyAnimeList",
            "filter[external_id]": ",".join(requested), "include": INCLUDE,
            "page[size]": ANIME_PAGE_SIZE, "page[number]": page,
        })
        if (not isinstance(data, dict) or not isinstance(data.get("anime"), list)
                or len(data["anime"]) > ANIME_PAGE_SIZE or not isinstance(data.get("links"), dict)
                or "next" not in data["links"]):
            raise GameError("The music provider returned invalid anime data.")
        next_page = data["links"]["next"]
        if next_page is not None and (not isinstance(next_page, str) or not next_page):
            raise GameError("The music provider returned invalid anime data.")
        for anime in data["anime"]:
            if (not isinstance(anime, dict) or type(anime.get("id")) is not int
                    or anime["id"] in seen or not isinstance(anime.get("name"), str)
                    or not anime["name"] or not isinstance(anime.get("resources"), list)
                    or not isinstance(anime.get("animethemes"), list)
                    or not isinstance(anime.get("animesynonyms"), list)):
                raise GameError("The music provider returned invalid anime data.")
            seen.add(anime["id"])
            matched = {requested[str(resource.get("external_id"))] for resource in anime["resources"]
                       if isinstance(resource, dict) and resource.get("site") == "MyAnimeList"
                       and str(resource.get("external_id")) in requested}
            if not matched:
                raise GameError("The music provider returned invalid anime data.")
            for mal_id in matched:
                found[mal_id].append(anime)
        if next_page is None:
            return found
        if not data["anime"]:
            break
        # Increment our own page parameter. Never request an untrusted next URL.
    raise GameError("The music provider returned too many anime pages.")


def _load_anime_batch(mal_ids):
    try:
        found = _fetch_anime_batch(mal_ids)
    except metadata.ResponseTooLarge:
        # Large franchises can exceed the transport cap even in a small batch.
        if len(mal_ids) == 1:
            raise
        middle = len(mal_ids) // 2
        return {**_load_anime_batch(mal_ids[:middle]), **_load_anime_batch(mal_ids[middle:])}
    now = time.time()
    CachedResource.objects.bulk_create([
        CachedResource(key=f"anime:mal:{mal_id}", value=anime,
                       expires_at=now + (ANIME_TTL if anime else MISSING_ANIME_TTL))
        for mal_id, anime in found.items()
    ], update_conflicts=True, unique_fields=["key"], update_fields=["value", "expires_at"])
    return found


def _cached_anime(ids):
    keys = [key for i in ids for key in (f"anime:mal:{i}", f"anime:{i}")]
    entries = CachedResource.objects.filter(key__in=keys, expires_at__gt=time.time()).in_bulk(field_name="key")
    found = {}
    for mal_id in ids:
        entry = entries.get(f"anime:mal:{mal_id}")
        if entry and isinstance(entry.value, list):
            found[mal_id] = entry.value
            continue
        # Reuse successful old title lookups, but retry their false misses.
        legacy = entries.get(f"anime:{mal_id}")
        if legacy and isinstance(legacy.value, dict) and legacy.value:
            found[mal_id] = [legacy.value]
    return found


def anime_data_batch(mal_ids):
    """Resolve exact MAL IDs; preserve every AnimeThemes record for each ID."""
    ids = list(mal_ids)
    if len(ids) > MAX_THEME_CANDIDATES or any(type(i) is not int or i <= 0 for i in ids):
        raise GameError("Invalid anime lookup IDs.")
    ids = list(dict.fromkeys(ids))
    metadata.checkpoint()
    # Cache hits must not wait for an unrelated background HTTP request.
    found = _cached_anime(ids)
    ids = [i for i in ids if i not in found]
    for offset in range(0, len(ids), ANIME_BATCH_SIZE):
        batch = ids[offset:offset + ANIME_BATCH_SIZE]
        with cache_work():
            found.update(_cached_anime(batch))
            missing = [i for i in batch if i not in found]
            if missing:
                found.update(_load_anime_batch(missing))
    return found


def songs_from_anime(anime, title, options):
    songs = []
    aliases = [title, *[a["text"] for a in anime.get("animesynonyms", []) if a.get("text")]]
    for theme in anime.get("animethemes", []):
        if theme.get("type") not in options["types"]:
            continue
        videos = [v for entry in theme.get("animethemeentries", [])
                  if (options.get("allowNsfw", False) or not entry.get("nsfw"))
                  and (options.get("allowSpoilers", False) or not entry.get("spoiler"))
                  for v in entry.get("videos", [])
                  if urlparse(v.get("link", "")).scheme == "https"]
        if not videos:
            continue
        # A modest-resolution stream reduces classroom buffering.
        video = min(videos, key=lambda v: (abs((v.get("resolution") or 720) - 480), v.get("size") or 0))
        song = theme.get("song") or {}
        songs.append({"id": f"at-{theme['id']}", "anime": anime["name"], "aliases": aliases,
            "song": song.get("title") or "Unknown title", "artist": ", ".join(a["name"] for a in song.get("artists", [])) or "Artist not listed",
            "type": theme["type"], "year": anime.get("year"), "media": video["link"]})
    return songs


def local_catalog():
    path = settings.DATA_DIR / "catalog.json"
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise GameError("Add data/catalog.json and your media files first. See the local catalog example in the README.") from exc
    if not isinstance(entries, list) or not 1 <= len(entries) <= 5000:
        raise GameError("catalog.json must contain between 1 and 5,000 songs.")
    songs = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k].strip()
            for k in ("anime", "song", "media", "type")):
            raise GameError(f"Catalog entry {i + 1} needs anime, song, media, and type fields.")
        if entry["type"] not in ["OP", "ED"]:
            raise GameError(f"Catalog entry {i + 1} type must be OP or ED.")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise GameError(f"Catalog entry {i + 1} aliases must be a list of titles.")
        media_path(entry["media"])
        songs.append({**entry, "id": f"local-{i}", "aliases": aliases, "artist": entry.get("artist", "Local collection")})
    return songs


def media_path(name):
    base = (settings.DATA_DIR / "media").resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file() or path.suffix.lower() not in (".webm", ".mp4", ".mp3", ".wav", ".ogg", ".m4a"):
        raise GameError("Media must be an existing audio/video file inside data/media.", 404)
    return path


def prepare(load_id, options, usernames):
    def check():
        with room_state() as s:
            if s["loadId"] != load_id or s["phase"] != "loading":
                raise InterruptedError()

    with metadata.work_context(check=check):
        try:
            _prepare(load_id, options, usernames)
        finally:
            metadata.close_session()


def list_candidates(entries, statuses):
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") not in statuses:
            continue
        mal_id, title = entry.get("anime_id"), entry.get("anime_title")
        if (type(mal_id) is int and mal_id > 0 and isinstance(title, str) and 1 <= len(title) <= 500):
            yield mal_id, title


def _prepare(load_id, options, usernames):
    close_old_connections()
    try:
        def progress(message, value):
            with room_state() as s:
                if s["loadId"] != load_id or s["phase"] != "loading":
                    raise InterruptedError()
                s.update(message=message, loadProgress=value)

        warnings, skipped_users, titles = [], [], set()
        pool_limited = False
        if options["source"] == "local":
            songs = [song for song in local_catalog() if song["type"] in options["types"]]
        elif options["source"] == "starter":
            # Verified metadata ships with the app; music streams from AnimeThemes.
            catalog = json.loads((Path(__file__).parent / "starter.json").read_text())
            songs = [song for anime in catalog for song in songs_from_anime(anime, anime["name"], options)]
        else:
            if not usernames:
                raise GameError("Players need to save a MAL username before using the group's lists.")
            anime_map = {}
            for i, username in enumerate(usernames):
                progress(f"Reading {username}'s MAL list…", round(15 * i / len(usernames)))
                try:
                    entries = mal_list(username)
                except GameError as exc:
                    if "rate limiting" in str(exc):
                        raise
                    skipped_users.append(username)
                    continue
                for mal_id, title in list_candidates(entries, options["statuses"]):
                    if mal_id not in anime_map and len(anime_map) >= MAX_CANDIDATES:
                        pool_limited = True
                        continue
                    anime_map[mal_id] = title
            if not anime_map:
                message = "No anime matched the selected MAL list statuses. Check the players' public lists."
                if skipped_users:
                    message += " Lists could not be imported for: " + ", ".join(skipped_users) + "."
                raise GameError(message)
            titles.update(anime_map.values())
            candidates = list(anime_map.items())
            RANDOM.shuffle(candidates)
            candidates = candidates[:MAX_THEME_CANDIDATES]
            songs = []
            for i in range(0, len(candidates), ANIME_BATCH_SIZE):
                batch = candidates[i:i + ANIME_BATCH_SIZE]
                progress(f"Finding themes · {batch[0][1]}", min(95, 15 + round(80 * i / len(candidates))))
                try:
                    resolved = anime_data_batch([mal_id for mal_id, _ in batch])
                    for mal_id, title in batch:
                        for anime in resolved[mal_id]:
                            songs.extend(songs_from_anime(anime, title, options))
                except GameError as exc:
                    if "rate limiting" in str(exc):
                        raise
                    warnings.extend(title for _, title in batch)
                if len(songs) >= max(30, options["rounds"] * 3):
                    break
        songs = list({song["id"]: song for song in songs}.values())
        if not songs:
            raise GameError("No playable songs matched these settings. Try another source or enable both OP and ED.")
        # Keep answers from the playable pool in autocomplete even if a large
        # collection reaches the title cap.
        playable_titles = {title for song in songs for title in [song["anime"], *song.get("aliases", [])]
                           if isinstance(title, str) and 1 <= len(title) <= 500}
        titles = set(sorted(playable_titles)[:MAX_CANDIDATES]) | set(
            sorted(titles - playable_titles)[:max(0, MAX_CANDIDATES - len(playable_titles))])
        RANDOM.shuffle(songs)
        queue = songs[:options["rounds"]]
        for song in queue:
            song["roundId"] = secrets.token_hex(12)
            song["startAt"] = RANDOM.randint(0, 30) if options["randomStart"] else 0
        with room_state() as s:
            if s["loadId"] == load_id and s["phase"] == "loading":
                message = f"{len(songs)} themes in the pool · {len(queue)} unique rounds ready."
                if len(queue) < options["rounds"]:
                    message += " Fewer songs matched than requested; no songs will repeat."
                if warnings:
                    message += f" {len(warnings)} titles could not be loaded."
                if skipped_users:
                    message += " Lists skipped (private, missing, or unavailable): " + ", ".join(skipped_users) + "."
                if pool_limited:
                    message += f" The combined pool was limited to {MAX_CANDIDATES:,} anime."
                s.update(phase="ready", queue=queue, index=-1, titles=sorted(titles),
                         loadProgress=100, message=message, guesses={}, history=[])
    except InterruptedError:
        pass
    except Exception as exc:
        LOG.exception("Song preparation failed")
        with room_state() as s:
            if s["loadId"] == load_id and s["phase"] == "loading":
                s.update(phase="lobby", message=str(exc) if isinstance(exc, GameError) else "Song loading failed. Check the server log and try again.", loadProgress=0)
    finally:
        close_old_connections()


def start_prepare(load_id, options, usernames):
    threading.Thread(target=prepare, args=(load_id, options, usernames), daemon=True, name="song-loader").start()
