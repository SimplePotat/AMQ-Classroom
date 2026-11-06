"""AnimeThemes entry tags must control the exact video variants in the pool."""
import copy
import json
import time
from unittest.mock import patch

from django.conf import settings
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from . import engine, library
from .models import CachedResource, Room


def tagged_anime(theme_type="OP"):
    return {"id": 1, "name": "Tagged fixture", "year": 2020, "animesynonyms": [],
            "resources": [{"site": "MyAnimeList", "external_id": 1}],
            "animethemes": [
                {"id": i, "type": theme_type, "song": {"title": f"Song {i}", "artists": []},
                 "animethemeentries": [{"nsfw": nsfw, "spoiler": spoiler, "videos": [{
                     "link": f"https://v.animethemes.moe/Fixture-{i}.webm", "resolution": 480,
                 }]}]}
                for i, (nsfw, spoiler) in enumerate([(False, False), (True, False), (False, True), (True, True)], 1)
            ]}


FILTER_CASES = [(False, False, {"at-1"}), (True, False, {"at-1", "at-2"}),
                (False, True, {"at-1", "at-3"}), (True, True, {"at-1", "at-2", "at-3", "at-4"})]


class AnimeThemesTagTests(SimpleTestCase):
    def test_both_filters_default_to_enabled(self):
        options = engine.validate_settings({})
        self.assertIs(options["allowNsfw"], False)
        self.assertIs(options["allowSpoilers"], False)
        self.assertEqual([song["id"] for song in library.songs_from_anime(tagged_anime(), "Fixture", options)], ["at-1"])

    def test_independent_flags_apply_to_both_openings_and_endings(self):
        for theme_type in ("OP", "ED"):
            for allow_nsfw, allow_spoilers, expected in FILTER_CASES:
                with self.subTest(theme_type=theme_type, nsfw=allow_nsfw, spoilers=allow_spoilers):
                    options = engine.validate_settings({"types": [theme_type], "allowNsfw": allow_nsfw,
                                                        "allowSpoilers": allow_spoilers})
                    songs = library.songs_from_anime(tagged_anime(theme_type), "Fixture", options)
                    self.assertEqual({song["id"] for song in songs}, expected)

    def test_tagged_versions_are_removed_before_choosing_video_resolution(self):
        for tag, setting in [("nsfw", "allowNsfw"), ("spoiler", "allowSpoilers")]:
            with self.subTest(tag=tag):
                fixture = tagged_anime()
                fixture["animethemes"] = fixture["animethemes"][:1]
                entry = fixture["animethemes"][0]["animethemeentries"][0]
                entry[tag] = True
                safe_entry = {"nsfw": False, "spoiler": False, "videos": [{
                    "link": "https://v.animethemes.moe/Safe-Version.webm", "resolution": 720,
                }]}
                fixture["animethemes"][0]["animethemeentries"].append(safe_entry)
                options = engine.validate_settings({})
                safe = library.songs_from_anime(fixture, "Fixture", options)
                self.assertEqual(safe[0]["media"], safe_entry["videos"][0]["link"])
                options[setting] = True
                permitted = library.songs_from_anime(fixture, "Fixture", options)
                self.assertEqual(permitted[0]["media"], entry["videos"][0]["link"])

    def test_legacy_options_still_exclude_nsfw(self):
        options = {"types": ["OP"], "allowSpoilers": False}
        self.assertEqual([song["id"] for song in library.songs_from_anime(tagged_anime(), "Fixture", options)], ["at-1"])

    def test_content_settings_require_actual_booleans(self):
        for setting in ("allowNsfw", "allowSpoilers"):
            for value in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(setting=setting, value=value), self.assertRaisesMessage(engine.GameError, setting):
                    engine.validate_settings({setting: value})


@override_settings(CLASSROOM_PREWARM_ENABLED=False)
class ContentFilterPoolTests(TransactionTestCase):
    def test_starter_and_cached_mal_pools_respect_every_filter_combination(self):
        fixture = tagged_anime()
        original = copy.deepcopy(fixture)
        CachedResource.objects.create(key="mal:filter_fixture", expires_at=time.time() + 100,
                                      value=[{"anime_id": 1, "anime_title": "Fixture", "status": 2}])
        cached = CachedResource.objects.create(key="anime:mal:1", value=[fixture], expires_at=time.time() + 100)
        with patch("classroom.library.get_json", side_effect=AssertionError("Already cached")), patch(
                "classroom.library.Path.read_text", return_value=json.dumps([fixture])), patch(
                "classroom.metadata.requests.Session.get") as probe:
            probe.return_value.__enter__.return_value.status_code = 206
            for source in ("starter", "mal"):
                for allow_nsfw, allow_spoilers, expected in FILTER_CASES:
                    with self.subTest(source=source, nsfw=allow_nsfw, spoilers=allow_spoilers):
                        load_id = f"filters-{source}-{allow_nsfw}-{allow_spoilers}"
                        with engine.room_state() as state:
                            state.update(phase="loading", loadId=load_id)
                        options = engine.validate_settings({"source": source, "allowNsfw": allow_nsfw,
                                                            "allowSpoilers": allow_spoilers})
                        library.prepare(load_id, options, ["filter_fixture"])
                        state = Room.objects.get(pk=1).state
                        self.assertEqual(state["phase"], "ready", state["message"])
                        queued = {song["id"] for song in state["queue"]}
                        self.assertEqual(len(queued), 1)
                        self.assertTrue(queued <= expected, (queued, expected))
        cached.refresh_from_db()
        self.assertEqual(cached.value, [original])

    def test_host_filter_preferences_are_saved_and_survive_restart(self):
        host = Client()
        response = host.post("/api/auth", json.dumps({"action": "host", "key": settings.HOST_KEY}),
                             content_type="application/json")
        self.assertEqual(response.status_code, 200)
        with patch("classroom.library.start_prepare") as start:
            response = host.post("/api/host", json.dumps({"action": "prepare", "settings": {
                "allowNsfw": True, "allowSpoilers": False,
            }}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIs(start.call_args.args[1]["allowNsfw"], True)
        engine.recover_after_restart()
        saved = host.get("/api/state").json()["settings"]
        self.assertIs(saved["allowNsfw"], True)
        self.assertIs(saved["allowSpoilers"], False)

    def test_upgrade_adds_nsfw_filter_and_preserves_previous_spoiler_preference(self):
        with engine.room_state() as state:
            state["settings"].pop("allowNsfw")
            state["settings"]["allowSpoilers"] = True
        engine.recover_after_restart()
        options = Room.objects.get(pk=1).state["settings"]
        self.assertIs(options["allowNsfw"], False)
        self.assertIs(options["allowSpoilers"], True)
