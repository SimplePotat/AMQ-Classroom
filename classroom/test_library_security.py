"""Regression coverage for untrusted metadata and independent player lists."""
import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TransactionTestCase

from . import engine, library, metadata
from .models import Room


def provider_response(status=200, chunks=(b'{"ok":true}',), length=None):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.headers = {} if length is None else {"Content-Length": str(length)}
    response.iter_content.return_value = iter(chunks)
    return response


class MetadataTransportSecurityTests(SimpleTestCase):
    def setUp(self):
        limiter = patch("classroom.metadata.wait_for_provider")
        limiter.start()
        self.addCleanup(limiter.stop)
        self.addCleanup(metadata.close_session)

    def test_only_exact_https_metadata_providers_are_requested(self):
        for url in ("http://myanimelist.net/list", "https://localhost/list",
                    "https://api.animethemes.moe.attacker.example/",
                    "https://api.animethemes.moe:8443/", "https://user@myanimelist.net/",
                    "https://127.0.0.1/", "https://api.animethemes.moe:bad/"):
            with self.subTest(url=url), patch("classroom.metadata.requests.Session.get") as request:
                with self.assertRaises(engine.GameError):
                    library.get_json(url)
                request.assert_not_called()

    def test_redirects_are_rejected_without_following_them(self):
        response = provider_response(status=302)
        response.headers["Location"] = "http://127.0.0.1/private"
        with patch("classroom.metadata.requests.Session.get", return_value=response) as request:
            with self.assertRaisesMessage(engine.GameError, "redirect"):
                library.get_json(library.API + "/search")
        self.assertIs(request.call_args.kwargs["allow_redirects"], False)
        response.iter_content.assert_not_called()
        response.__exit__.assert_called_once()

    def test_declared_oversized_responses_are_rejected_before_reading(self):
        response = provider_response(length=metadata.MAX_JSON_BYTES + 1)
        with patch("classroom.metadata.requests.Session.get", return_value=response):
            with self.assertRaisesMessage(engine.GameError, "too large"):
                library.get_json(library.API + "/search")
        response.iter_content.assert_not_called()
        response.__exit__.assert_called_once()

    def test_stream_limit_applies_without_a_content_length(self):
        response = provider_response(chunks=(b"12345", b"67890", b"extra"))
        with patch("classroom.metadata.MAX_JSON_BYTES", 9), patch(
                "classroom.metadata.requests.Session.get", return_value=response):
            with self.assertRaisesMessage(engine.GameError, "too large"):
                library.get_json(library.API + "/search")
        response.__exit__.assert_called_once()

    def test_small_streams_parse_json_and_close_the_response(self):
        response = provider_response(chunks=(b'{"ok":', b"true}"))
        with patch("classroom.metadata.requests.Session.get", return_value=response) as request:
            self.assertEqual(library.get_json(library.API + "/search"), {"ok": True})
        self.assertIs(request.call_args.kwargs["stream"], True)
        response.__exit__.assert_called_once()


class GroupListSecurityTests(TransactionTestCase):
    def prepare(self, lists, **settings):
        with engine.room_state() as state:
            state.update(phase="loading", loadId="security-test")
        options = engine.validate_settings({"source": "mal", "randomStart": False, **settings})
        song = {"id": "theme", "anime": "Playable title", "aliases": ["Playable alias"],
                "song": "Test song", "artist": "Test artist", "type": "OP", "media": "test.webm"}
        with patch("classroom.library.mal_list", side_effect=lists), patch(
                "classroom.library.anime_data_batch", side_effect=lambda ids: {i: [{}] for i in ids}), patch(
                "classroom.library.songs_from_anime", return_value=[song]), patch("classroom.library.time.sleep"):
            library.prepare("security-test", options, [f"player_{i}" for i in range(len(lists))])
        return Room.objects.get(pk=1).state

    def test_one_unavailable_list_does_not_abort_other_players_pool(self):
        state = self.prepare([engine.GameError("List is private"), [
            {"anime_id": 1, "anime_title": "Good title", "status": 2}]])
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(len(state["queue"]), 1)
        self.assertIn("player_0", state["message"])
        self.assertIn("Lists skipped", state["message"])
        self.assertNotIn("player_0", json.dumps(engine.public_state(state)))

    def test_all_unavailable_lists_leave_a_clear_host_error(self):
        state = self.prepare([engine.GameError("List is private"), engine.GameError("Missing")])
        self.assertEqual(state["phase"], "lobby")
        self.assertEqual(state["queue"], [])
        self.assertIn("player_0, player_1", state["message"])

    def test_rate_limit_stops_requests_instead_of_querying_more_players(self):
        state = self.prepare([engine.GameError("The music provider is rate limiting requests.")])
        self.assertEqual(state["phase"], "lobby")
        self.assertIn("rate limiting", state["message"])

    def test_combined_pool_and_autocomplete_are_bounded(self):
        lists = [[{"anime_id": i, "anime_title": f"Title {i}", "status": 2} for i in range(1, 4)],
                 [{"anime_id": i, "anime_title": f"Title {i}", "status": 2} for i in range(4, 7)]]
        with patch("classroom.library.MAX_CANDIDATES", 3):
            state = self.prepare(lists)
        self.assertEqual(state["phase"], "ready")
        self.assertLessEqual(len(state["titles"]), 3)
        self.assertIn("Playable title", state["titles"])
        self.assertIn("Playable alias", state["titles"])
        self.assertIn("limited to 3 anime", state["message"])

    def test_malformed_list_entries_cannot_abort_valid_players(self):
        state = self.prepare([[None, "bad", {}, {"status": 2, "anime_id": [], "anime_title": "Bad"}],
                              [{"anime_id": 1, "anime_title": "Good title", "status": 2}]])
        self.assertEqual(state["phase"], "ready")

    def test_provider_cannot_return_an_unbounded_mal_page(self):
        with patch("classroom.library.get_json", return_value=[{}] * 301):
            with self.assertRaisesMessage(engine.GameError, "too large"):
                library.mal_list("test_user")

    def test_cached_lists_only_retain_bounded_quiz_metadata(self):
        page = [{"anime_id": 1, "anime_title": "Cowboy Bebop", "status": 2, "unused": "private notes"},
                {"anime_id": [], "anime_title": "x" * 501, "status": {"bad": True}}]
        with patch("classroom.library.get_json", return_value=page):
            entries = library.mal_list("test_user")
        self.assertEqual(entries, [{"anime_id": 1, "anime_title": "Cowboy Bebop", "status": 2},
                                   {"anime_id": None, "anime_title": None, "status": None}])
