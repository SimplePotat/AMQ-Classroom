"""Refresh the small starter metadata catalog. Does not download music/video."""
import json
import sys
import time
from pathlib import Path
import requests

SLUGS = ["cowboy_bebop", "death_note", "fullmetal_alchemist_brotherhood", "shingeki_no_kyojin", "naruto", "one_piece", "one_punch_man", "kimetsu_no_yaiba", "toradora", "haikyuu", "steinsgate", "hunter_x_hunter_2011"]
catalog = []
for slug in SLUGS:
    response = requests.get("https://api.animethemes.moe/anime/" + slug, params={"include": "animethemes.animethemeentries.videos,animethemes.song.artists,animesynonyms,resources"}, timeout=30)
    response.raise_for_status()
    anime = response.json()["anime"]
    catalog.append({key: anime[key] for key in ("id", "name", "year", "animesynonyms", "animethemes", "resources")})
    print(anime["name"], flush=True)
    time.sleep(0.4)
target = Path(__file__).resolve().parents[1] / "classroom" / "starter.json"
target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
print(f"Saved {len(catalog)} anime to {target}")
