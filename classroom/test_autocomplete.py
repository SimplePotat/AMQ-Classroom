"""Autocomplete reads the populated database without disclosing list owners."""
import time
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase, override_settings

from . import engine, library, title_catalog
from .models import CachedResource, OpeningsResource, Profile, Room


@override_settings(CLASSROOM_PREWARM_ENABLED=False)
class AutocompleteTests(TestCase):
    def setUp(self):
        title_catalog.invalidate()
        self.addCleanup(title_catalog.invalidate)
        profile = Profile.objects.create(name="Player", name_key="player", password="unused")
        session = self.client.session
        session["profile_id"] = profile.pk
        session.save()

    def cache(self, key, value, expired=False):
        return CachedResource.objects.create(key=key, value=value,
            expires_at=time.time() + (-100 if expired else 100))

    def test_reads_all_cached_mal_lists_and_aliases_including_legacy_and_expired_names(self):
        self.cache("mal:private_owner", [
            {"anime_id": 1, "anime_title": "Cowboy Bebop", "status": 6},
            {"anime_id": 2, "anime_title": "Another Show", "status": 4},
        ], expired=True)
        self.cache("mal:someone_else", [{"anime_id": 1, "anime_title": "cowboy bebop", "status": 1}])
        self.cache("anime:mal:16498", [{"name": "Shingeki no Kyojin", "animesynonyms": [
            {"text": "Attack on Titan"}, {"text": "進撃の巨人"}], "media": "private-video"}])
        self.cache("anime:9253", {"name": "Steins;Gate", "animesynonyms": [{"text": "STEINS;GATE"}]})
        with patch("classroom.library.get_json", side_effect=AssertionError("Autocomplete made a provider request")):
            response = self.client.get("/api/anime-titles")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"titles": ["Another Show", "Attack on Titan", "Cowboy Bebop",
            "Shingeki no Kyojin", "Steins;Gate", "進撃の巨人"]})
        for private in ("private_owner", "someone_else", "private-video", "anime_id", "status"):
            self.assertNotIn(private, response.content.decode())

    def test_ignores_unrelated_caches_and_never_uses_the_current_answer(self):
        self.cache("other:secret", {"name": "Not an anime"})
        self.cache("anime:slug", {"name": "Not MAL-linked"})
        self.cache("anime:mal:0", {"name": "Invalid ID"})
        OpeningsResource.objects.create(key="openings:catalog", value={"name": "Other provider"}, expires_at=0)
        s = engine.initial()
        s.update(phase="guess", deadline=time.time() - 10,
                 queue=[{"anime": "Unrevealed answer"}], index=0, titles=["Pool only"])
        Room.objects.create(pk=1, state=s)
        self.assertEqual(self.client.get("/api/anime-titles").json(), {"titles": []})
        self.assertEqual(Room.objects.get(pk=1).state, s, "Title requests must not tick the game")

    def test_requires_session_and_get_method(self):
        self.assertEqual(Client().get("/api/anime-titles").status_code, 401)
        for method in ("post", "put", "delete", "head"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)("/api/anime-titles").status_code, 405)
        host = Client()
        self.assertEqual(host.post("/api/auth", {"action": "host", "key": settings.HOST_KEY},
                                  content_type="application/json").status_code, 200)
        self.assertEqual(host.get("/api/anime-titles").status_code, 200)

    def test_corrupt_cache_records_are_skipped_without_losing_healthy_titles(self):
        self.cache("mal:corrupt", [None, 8, "wrong", {"anime_id": True, "anime_title": "Invalid bool ID"},
            {"anime_id": -1, "anime_title": "Invalid negative ID"},
            *[{"anime_id": 1, "anime_title": value} for value in
              (None, [], {}, "", "---", "x" * 201, "bad\ud800", "bad\nname")],
            {"anime_id": 1, "anime_title": "  Valid Title  "}])
        self.cache("mal:wrong", {"anime_title": "Wrong container"})
        self.cache("anime:mal:1", [None, "bad", {"name": 123, "animesynonyms": "bad"},
            {"name": "Healthy Anime", "animesynonyms": [None, "wrong", {"text": "Healthy Alias"}]}])
        self.assertEqual(title_catalog.titles(), ["Healthy Alias", "Healthy Anime", "Valid Title"])

    def test_snapshot_is_bounded_and_shared_between_clients(self):
        self.cache("mal:list", [{"anime_id": i + 1, "anime_title": f"Title {i}"} for i in range(20)])
        with patch.object(title_catalog, "MAX_TITLES", 5):
            with self.assertNumQueries(1):
                self.assertEqual(len(title_catalog.titles()), 5)
            with self.assertNumQueries(0):
                self.assertEqual(len(title_catalog.titles()), 5)

    def test_successful_list_and_theme_imports_invalidate_the_snapshot(self):
        self.assertEqual(title_catalog.titles(), [])
        with patch("classroom.library.get_json", return_value=[{"anime_id": 1, "anime_title": "New title", "status": 2}]):
            library.mal_list("new_player")
        self.assertEqual(title_catalog.titles(), ["New title"])
        with patch("classroom.library._fetch_anime_batch", return_value={1: [
            {"name": "New title", "animesynonyms": [{"text": "New alias"}]}]}):
            library._load_anime_batch([1])
        self.assertEqual(title_catalog.titles(), ["New alias", "New title"])

    def test_database_changes_appear_after_snapshot_expiry(self):
        with patch("classroom.title_catalog.time.monotonic", return_value=100):
            self.assertEqual(title_catalog.titles(), [])
        self.cache("mal:imported", [{"anime_id": 1, "anime_title": "Imported later"}])
        with patch("classroom.title_catalog.time.monotonic", return_value=100 + title_catalog.CACHE_SECONDS):
            self.assertEqual(title_catalog.titles(), ["Imported later"])
