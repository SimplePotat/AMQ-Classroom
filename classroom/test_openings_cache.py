"""Opening-provider opt-in, independent caches, and upgrade preservation."""
import copy
import json
import time
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from . import engine, library, metadata
from .models import CachedResource, OpeningsResource, Room
from .test_metadata_cache import anime, batch_response, mal_entries, openings_payload, openings_record


class OpeningsSettingTests(SimpleTestCase):
    def test_fallback_is_opt_in_and_independent_of_openings_first(self):
        self.assertIs(engine.validate_settings({})["allowOpeningsFallback"], False)
        self.assertIs(engine.validate_settings({"preferOpenings": True})["allowOpeningsFallback"], False)
        self.assertIs(engine.validate_settings({"allowOpeningsFallback": True})["allowOpeningsFallback"], True)
        for value in (0, 1, "false", "true", None, [], {}):
            with self.subTest(value=value), self.assertRaisesMessage(engine.GameError, "allowOpeningsFallback"):
                engine.validate_settings({"allowOpeningsFallback": value})


@override_settings(CLASSROOM_PREWARM_ENABLED=False)
class OpeningsIsolationTests(TransactionTestCase):
    def prepare(self, **changes):
        options = engine.validate_settings({"source": "mal", "types": ["OP"], **changes})
        with engine.room_state() as state:
            state.update(phase="loading", loadId="provider-test", settings=options, queue=[])
        with patch("classroom.library.mal_list", return_value=mal_entries(1)):
            library.prepare("provider-test", options, ["alice"])
        return Room.objects.get(pk=1).state

    def respond(self, url, params=None):
        if url == library.API + "/anime":
            return batch_response(url, params)
        if url == "https://openings.moe/api/list.php":
            return [openings_record("Opening1-title-1", "Title 1")]
        if url == "https://openings.moe/api/details.php":
            return openings_payload(params["uid"])
        raise AssertionError(f"Unexpected provider: {url}")

    def test_metadata_failure_does_not_read_even_a_warm_openings_cache_by_default(self):
        OpeningsResource.objects.create(key="openings:catalog", expires_at=time.time() + 100,
                                       value={"title1": [openings_record("Opening1-title-1", "Title 1")]})
        before = list(OpeningsResource.objects.values())
        with patch("classroom.library.get_json", side_effect=engine.GameError("Provider unavailable")) as request, patch(
                "classroom.library.songs_from_openings") as fallback:
            state = self.prepare()
        self.assertEqual(state["phase"], "lobby")
        self.assertIn("Provider unavailable", state["message"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], library.API + "/anime")
        fallback.assert_not_called()
        self.assertEqual(list(OpeningsResource.objects.values()), before)

    def test_dead_video_never_uses_fallback_unless_enabled(self):
        with patch("classroom.library.get_json", side_effect=self.respond) as request, patch(
                "classroom.metadata.media_playable", return_value=False) as probe, patch(
                "classroom.library.songs_from_openings") as fallback:
            state = self.prepare()
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(state["queue"][0]["id"], "at-1")
        fallback.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(request.call_count, 1)
        self.assertFalse(OpeningsResource.objects.exists())

    def test_opted_in_video_swap_keeps_both_provider_urls_and_can_switch_back(self):
        original = CachedResource.objects.create(key="anime:mal:1", value=[anime(1)], expires_at=time.time() + 100)
        before = copy.deepcopy(original.value), original.expires_at
        with patch("classroom.library.get_json", side_effect=self.respond) as request, patch(
                "classroom.metadata.media_playable", return_value=False):
            state = self.prepare(allowOpeningsFallback=True)
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(state["queue"][0]["media"], "https://openings.moe/video/Opening1-title-1.webm")
        self.assertEqual(request.call_count, 2)
        original.refresh_from_db()
        self.assertEqual((original.value, original.expires_at), before)
        self.assertEqual(OpeningsResource.objects.count(), 2)
        self.assertFalse(CachedResource.objects.filter(key__startswith="openings:").exists())

        with patch("classroom.library.get_json") as request, patch(
                "classroom.library.songs_from_openings") as fallback, patch("classroom.metadata.media_playable") as probe:
            state = self.prepare()
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(state["queue"][0]["media"], "https://v.animethemes.moe/1.webm")
        request.assert_not_called()
        fallback.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(OpeningsResource.objects.count(), 2)

    def test_openings_hit_does_not_replace_an_anime_themes_miss_or_its_expiry(self):
        original = CachedResource.objects.create(key="anime:mal:1", value=[], expires_at=time.time() + 100)
        expiry = original.expires_at
        with patch("classroom.library.get_json", side_effect=self.respond) as request:
            songs = library.songs_from_openings([(1, "Title 1")], engine.validate_settings({}), 1)
            self.assertTrue(songs)
            request.reset_mock()
            self.assertEqual(library.anime_data_batch([1]), {1: []})
            self.assertEqual(library.songs_from_openings([(1, "Title 1")], engine.validate_settings({}), 1), songs)
            request.assert_not_called()
        original.refresh_from_db()
        self.assertEqual((original.value, original.expires_at), ([], expiry))

    def test_expiring_openings_data_cannot_expire_or_overwrite_anime_themes_data(self):
        original = CachedResource.objects.create(key="anime:mal:1", value=[anime(1)], expires_at=time.time() + 100)
        before = original.value, original.expires_at
        OpeningsResource.objects.create(key="openings:uid:Opening1-title-1", value={"file": "stale"},
                                       expires_at=time.time() - 1)
        with patch("classroom.library.get_json", side_effect=self.respond) as request:
            self.assertEqual(library.openings_details("Opening1-title-1")["file"], "Opening1-title-1")
            request.assert_called_once()
        original.refresh_from_db()
        self.assertEqual((original.value, original.expires_at), before)
        self.assertGreater(OpeningsResource.objects.get(key="openings:uid:Opening1-title-1").expires_at, time.time())

    def test_rate_limiting_does_not_trigger_fallback_even_when_selected(self):
        with patch("classroom.library.get_json", side_effect=metadata.RateLimited()), patch(
                "classroom.library.songs_from_openings") as fallback:
            state = self.prepare(allowOpeningsFallback=True)
        self.assertEqual(state["phase"], "lobby")
        self.assertIn("rate limiting", state["message"])
        fallback.assert_not_called()

    def test_starter_video_fallback_also_requires_the_checkbox(self):
        fixture = anime(1)
        fixture["name"] = "Title 1"
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch("classroom.library.Path.read_text", return_value=json.dumps([fixture])), patch(
                    "classroom.library.get_json", side_effect=self.respond) as request, patch(
                    "classroom.metadata.media_playable", return_value=False):
                state = self.prepare(source="starter", allowOpeningsFallback=enabled)
            self.assertEqual(state["phase"], "ready", state["message"])
            self.assertEqual(state["queue"][0]["id"], "om-Opening1-title-1" if enabled else "at-1")
            self.assertEqual(request.call_count, 2 if enabled else 0)

    def test_selected_fallback_cannot_make_local_mode_contact_a_provider(self):
        local = {"id": "local-1", "anime": "Local", "aliases": [], "song": "Song", "type": "OP", "media": "local.webm"}
        with patch("classroom.library.local_catalog", return_value=[local]), patch(
                "classroom.library.get_json") as request, patch("classroom.metadata.media_playable") as probe:
            state = self.prepare(source="local", preferOpenings=True, allowOpeningsFallback=True)
        self.assertEqual(state["phase"], "ready")
        request.assert_not_called()
        probe.assert_not_called()

    def test_host_choice_survives_restart_and_older_rooms_default_to_no_fallback(self):
        host = Client()
        response = host.post("/api/auth", json.dumps({"action": "host", "key": settings.HOST_KEY}),
                             content_type="application/json")
        self.assertEqual(response.status_code, 200)
        with patch("classroom.library.start_prepare") as start:
            response = host.post("/api/host", json.dumps({"action": "prepare", "settings": {
                "source": "mal", "allowOpeningsFallback": True,
            }}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIs(start.call_args.args[1]["allowOpeningsFallback"], True)
        engine.recover_after_restart()
        self.assertIs(host.get("/api/state").json()["settings"]["allowOpeningsFallback"], True)
        with engine.room_state() as state:
            state["settings"].pop("allowOpeningsFallback")
            state["settings"]["preferOpenings"] = True
        engine.recover_after_restart()
        options = Room.objects.get(pk=1).state["settings"]
        self.assertIs(options["allowOpeningsFallback"], False)
        self.assertIs(options["preferOpenings"], True)


class OpeningsCacheMigrationTests(TransactionTestCase):
    old = [("classroom", "0001_initial")]
    new = [("classroom", "0002_openingsresource")]

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_upgrade_and_rollback_keep_both_caches_intact(self):
        self.addCleanup(self.migrate, self.new)
        apps = self.migrate(self.old)
        old_cache = apps.get_model("classroom", "CachedResource")
        entries = {"anime:mal:1": [anime(1)], "anime:mal:2": [], "mal:alice": mal_entries(2),
                   "openings:catalog": {"title1": [openings_record("Opening1-title-1", "Title 1")]},
                   "openings:uid:Opening1-title-1": openings_payload("Opening1-title-1")["data"]}
        expiry = time.time() + 100
        for key, value in entries.items():
            old_cache.objects.create(key=key, value=value, expires_at=expiry)
        self.migrate(self.new)
        for key, value in entries.items():
            model = OpeningsResource if key.startswith("openings:") else CachedResource
            row = model.objects.get(key=key)
            self.assertEqual((row.value, row.expires_at), (value, expiry))
        self.assertFalse(CachedResource.objects.filter(key__startswith="openings:").exists())
        self.assertFalse(OpeningsResource.objects.exclude(key__startswith="openings:").exists())
        apps = self.migrate(self.old)
        self.assertEqual(dict(apps.get_model("classroom", "CachedResource").objects.values_list("key", "value")), entries)
        self.migrate(self.new)
        self.assertEqual(OpeningsResource.objects.count(), 2)
        self.assertEqual(CachedResource.objects.count(), 3)
