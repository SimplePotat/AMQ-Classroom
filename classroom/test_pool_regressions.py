"""Preparation must count playable shows and keep explicit provider preferences."""
import copy
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from . import engine, library, metadata
from .models import Room
from .test_metadata_cache import anime, mal_entries, openings_payload, openings_record, page


@override_settings(CLASSROOM_PREWARM_ENABLED=False)
class UniquePoolTests(TransactionTestCase):
    def prepare(self, entries, response, **changes):
        options = engine.validate_settings({"source": "mal", "types": ["OP"], "rounds": 2, **changes})
        with engine.room_state() as state:
            state.update(phase="loading", loadId="pool-regression")
        with patch("classroom.library.mal_list", return_value=entries), patch(
                "classroom.library.get_json", side_effect=response), patch(
                "classroom.library.RANDOM.shuffle", side_effect=lambda seq: None):
            library.prepare("pool-regression", options, ["alice"])
        return Room.objects.get(pk=1).state

    def test_many_themes_on_one_show_cannot_stop_later_anime_batches(self):
        requested = []
        def response(url, params):
            ids = [int(value) for value in params["filter[external_id]"].split(",")]
            requested.extend(ids)
            records = [anime(mal_id) for mal_id in ids]
            for record in records:
                if record["id"] == 1:
                    theme = record["animethemes"][0]
                    record["animethemes"] = [{**copy.deepcopy(theme), "id": 1000 + i} for i in range(40)]
            return page(*records)
        with patch("classroom.library.ANIME_BATCH_SIZE", 1):
            state = self.prepare(mal_entries(12), response, rounds=10)
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(len(state["queue"]), 10)
        self.assertEqual(len({song["anime"] for song in state["queue"]}), 10)
        self.assertIn(10, requested)

    def test_openings_candidates_cover_distinct_shows_before_repeating_versions(self):
        catalog = [openings_record(f"Opening{i}-title-1", "Title 1") for i in range(1, 40)]
        catalog.append(openings_record("Opening1-title-2", "Title 2"))
        def response(url, params=None):
            return catalog if "list.php" in url else openings_payload(params["uid"])
        with patch("classroom.library.get_json", side_effect=response), patch(
                "classroom.library.RANDOM.shuffle", side_effect=lambda seq: None):
            songs = library.songs_from_openings([(1, "Title 1"), (2, "Title 2")], engine.validate_settings({}), 2)
        self.assertEqual({song["anime"] for song in songs}, {"Title 1", "Title 2"})

    def test_openings_first_does_not_look_up_shows_already_supplied_by_openings(self):
        requested = []
        def response(url, params=None):
            if "api.animethemes.moe" in url:
                ids = [int(value) for value in params["filter[external_id]"].split(",")]
                requested.extend(ids)
                records = [anime(mal_id) for mal_id in ids]
                for record in records:
                    record["name"] = f"Title {record['id']}"
                return page(*records)
            if "list.php" in url:
                return [openings_record("Opening1-title-1", "Title 1")]
            return openings_payload(params["uid"])
        state = self.prepare(mal_entries(2), response, preferOpenings=True)
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(requested, [2])
        self.assertEqual({song["id"] for song in state["queue"]}, {"om-Opening1-title-1", "at-2"})

    def test_all_shows_from_openings_skip_anime_themes_even_with_few_versions(self):
        def response(url, params=None):
            if "api.animethemes.moe" in url:
                raise AssertionError("Every candidate show already has a preferred-provider song")
            if "list.php" in url:
                return [openings_record(f"Opening1-title-{i}", f"Title {i}") for i in (1, 2)]
            return openings_payload(params["uid"])
        state = self.prepare(mal_entries(2), response, preferOpenings=True)
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(len(state["queue"]), 2)
        self.assertTrue(all(song["id"].startswith("om-") for song in state["queue"]))

    def test_unavailable_preferred_provider_can_still_fill_the_pool_from_anime_themes(self):
        def response(url, params=None):
            if "openings.moe" in url:
                raise engine.GameError("Openings provider unavailable")
            return page(*(anime(int(value)) for value in params["filter[external_id]"].split(",")))
        state = self.prepare(mal_entries(2), response, preferOpenings=True)
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(len(state["queue"]), 2)
        self.assertTrue(all(song["id"].startswith("at-") for song in state["queue"]))

    def test_one_mal_show_with_two_provider_titles_appears_only_once(self):
        first, second = anime(1), anime(1, 2)
        second["name"] = "Different English title for the same show"
        state = self.prepare(mal_entries(1), lambda *args: page(first, second))
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(len(state["queue"]), 1)

    def test_openings_detail_rate_limit_stops_instead_of_switching_providers(self):
        requested = []
        def response(url, params=None):
            requested.append(url)
            if "api.animethemes.moe" in url:
                return page(anime(1))
            if "list.php" in url:
                return [openings_record("Opening1-title-1", "Title 1")]
            raise metadata.RateLimited()
        state = self.prepare(mal_entries(1), response, preferOpenings=True)
        self.assertEqual(state["phase"], "lobby")
        self.assertIn("rate limiting", state["message"])
        self.assertFalse(any("api.animethemes.moe" in url for url in requested))

    def test_rate_limit_during_opted_in_fallback_is_reported_for_lookup_and_video_failures(self):
        for failed_lookup in (True, False):
            def response(url, params=None):
                if "api.animethemes.moe" in url:
                    if failed_lookup:
                        raise engine.GameError("AnimeThemes unavailable")
                    return page(anime(1))
                if "list.php" in url:
                    return [openings_record("Opening1-title-1", "Title 1")]
                raise metadata.RateLimited()
            with self.subTest(failed_lookup=failed_lookup), patch("classroom.metadata.media_playable", return_value=False):
                state = self.prepare(mal_entries(1), response, allowOpeningsFallback=True)
            self.assertEqual(state["phase"], "lobby")
            self.assertIn("rate limiting", state["message"])


class MediaProbeRegressionTests(SimpleTestCase):
    def test_malformed_media_ports_are_unavailable_instead_of_raising(self):
        with patch("classroom.metadata.http_session") as request:
            for url in ("https://v.animethemes.moe:bad/file.webm", "https://v.animethemes.moe:70000/file.webm"):
                with self.subTest(url=url):
                    self.assertIs(metadata.media_playable(url), False)
            request.assert_not_called()
