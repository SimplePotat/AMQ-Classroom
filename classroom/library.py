"""MAL/AnimeThemes song pools, with persistent caching and an offline catalog."""
import json
import logging
import random
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.db import close_old_connections
from .engine import GameError, room_state
from .models import CachedResource

LOG = logging.getLogger(__name__)
RANDOM = random.SystemRandom()
API = "https://api.animethemes.moe"
INCLUDE = "animethemes.animethemeentries.videos,animethemes.song.artists,animesynonyms,resources"
STARTER = [
    (1, "Cowboy Bebop"), (1535, "Death Note"), (5114, "Fullmetal Alchemist: Brotherhood"),
    (16498, "Shingeki no Kyojin"), (20, "Naruto"), (21, "One Piece"),
    (30276, "One Punch Man"), (38000, "Kimetsu no Yaiba"), (4224, "Toradora!"),
    (20583, "Haikyuu!!"), (9253, "Steins;Gate"), (11061, "Hunter x Hunter (2011)"),
]


def cached(key, seconds, fetch):
    entry = CachedResource.objects.filter(key=key, expires_at__gt=time.time()).first()
    if entry:
        return entry.value
    value = fetch()
    CachedResource.objects.update_or_create(key=key, defaults={"value": value, "expires_at": time.time() + seconds})
    return value


def get_json(url, params=None):
    try:
        response = requests.get(url, params=params, timeout=(5, 20),
            headers={"User-Agent": "AnimeQuizClassroom/1.0 (+https://github.com/SimplePotat/anime-quiz-classroom)"})
        if response.status_code == 429:
            raise GameError("The music provider is rate limiting requests. Wait a minute and try again.")
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GameError("The music provider could not be reached. Check the host's internet connection or use a local catalog.") from exc


def mal_list(username):
    def fetch():
        entries = []
        for offset in range(0, 50001, 300):
            data = get_json(f"https://myanimelist.net/animelist/{quote(username, safe='')}/load.json",
                            {"status": 7, "offset": offset})
            if not isinstance(data, list):
                raise GameError(f"MAL list for {username} is private, missing, or temporarily unavailable.")
            entries.extend(data)
            if len(data) < 300:
                return entries
        raise GameError(f"MAL list for {username} is too large to import.")
    return cached(f"mal:{username.casefold()}", 7 * 86400, fetch)


def anime_data(mal_id, title):
    def fetch():
        matches = get_json(API + "/search", {"q": title, "fields[search]": "anime"}).get("search", {}).get("anime", [])
        for match in matches[:5]:
            anime = get_json(API + "/anime/" + quote(match["slug"], safe=""), {"include": INCLUDE})["anime"]
            ids = [str(r["external_id"]) for r in anime.get("resources", []) if r["site"] == "MyAnimeList"]
            if str(mal_id) in ids:
                return anime
        return {}
    return cached(f"anime:{mal_id}", 30 * 86400, fetch)


def songs_from_anime(anime, title, options):
    songs = []
    aliases = [title, *[a["text"] for a in anime.get("animesynonyms", []) if a.get("text")]]
    for theme in anime.get("animethemes", []):
        if theme.get("type") not in options["types"]:
            continue
        videos = [v for entry in theme.get("animethemeentries", [])
                  if not entry.get("nsfw") and (options["allowSpoilers"] or not entry.get("spoiler"))
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
    close_old_connections()
    try:
        def progress(message, value):
            with room_state() as s:
                if s["loadId"] != load_id or s["phase"] != "loading":
                    raise InterruptedError()
                s.update(message=message, loadProgress=value)

        warnings, titles = [], set()
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
                entries = mal_list(username)
                for entry in entries:
                    if entry.get("status") in options["statuses"]:
                        anime_map[entry["anime_id"]] = entry["anime_title"]
                        titles.add(entry["anime_title"])
            if not anime_map:
                raise GameError("No anime matched the selected MAL list statuses. Check the players' public lists.")
            candidates = list(anime_map.items())
            RANDOM.shuffle(candidates)
            songs = []
            for i, (mal_id, title) in enumerate(candidates[:80]):
                progress(f"Finding themes · {title}", min(95, 15 + round(80 * i / min(len(candidates), 80))))
                try:
                    songs.extend(songs_from_anime(anime_data(mal_id, title), title, options))
                except GameError as exc:
                    if "rate limiting" in str(exc):
                        raise
                    warnings.append(title)
                if len(songs) >= max(30, options["rounds"] * 3):
                    break
                time.sleep(0.25)
        songs = list({song["id"]: song for song in songs}.values())
        if not songs:
            raise GameError("No playable songs matched these settings. Try another source or enable both OP and ED.")
        titles.update(title for song in songs for title in [song["anime"], *song.get("aliases", [])] if title)
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
