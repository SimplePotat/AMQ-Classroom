"""Security regressions for the retained upstream UI (no worker services needed)."""
import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils.html import escape

from quiz import animethemes, mal


ROOT = Path(__file__).resolve().parent.parent


def load_legacy_views():
    # The classroom install intentionally excludes Redis/lyrics worker packages.
    # Stub only that service boundary while exercising the real view/template.
    spec = importlib.util.spec_from_file_location("quiz._security_views", ROOT / "quiz/views.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"quiz.tasks": SimpleNamespace(
        GetUserThemesTask=Mock(), find_cached_lyrics=Mock(return_value=None)
    )}):
        spec.loader.exec_module(module)
    return module


@override_settings(TEMPLATES=[{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [ROOT / "quiz/templates"],
}])
class LegacyEscapingTests(SimpleTestCase):
    def setUp(self):
        self.views = load_legacy_views()
        self.theme = {
            "id": 1, "slug": "OP1", "type": "OP", "anime_title": "Example",
            "song": {"title": "Example song", "artists": []},
            "animethemeentries": [{"spoiler": False, "nsfw": False,
                "videos": [{"link": "https://v.animethemes.moe/example.webm"}]}],
        }

    def request(self, **params):
        request = RequestFactory().get("/", params)
        request.session = {}
        return self.views.UserThemesView.as_view()(request)

    def test_username_alert_is_escaped_in_full_and_partial_page(self):
        attack = '<img src=x onerror="alert(document.cookie)">'

        def themes(user, statuses):
            if user == attack:
                raise mal.MyanimelistException("Not found")
            return [copy.deepcopy(self.theme)]

        with patch.object(self.views.UserThemesView, "_get_user_themes", side_effect=themes):
            for partial in ("yes", "no"):
                response = self.request(user=attack, player_only=partial)
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                self.assertNotIn(attack, body)
                self.assertIn(str(escape(attack)), body)

    def test_cached_lyrics_are_escaped_while_breaks_are_preserved(self):
        attack = '<svg onload="alert(1)"></svg>'
        with patch.object(self.views.UserThemesView, "_get_user_themes", return_value=[self.theme]), \
                patch.object(self.views, "find_cached_lyrics", return_value=f"First<br />{attack}\nLast"):
            body = self.request().content.decode()
        self.assertNotIn(attack, body)
        self.assertIn(f"First<br>{escape(attack)}<br>Last", body)

    def test_temporary_error_message_is_escaped(self):
        attack = '<img src=x onerror="alert(1)">'
        with patch.object(self.views.UserThemesView, "_get_user_themes",
                          side_effect=self.views.TaskStatus(attack)):
            response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(attack, response.content.decode())
        self.assertIn(str(escape(attack)), response.content.decode())


class LegacyProviderTests(SimpleTestCase):
    def test_mal_requests_have_timeout_and_do_not_follow_redirects(self):
        response = Mock(status_code=200)
        response.json.return_value = []
        with patch.object(mal.requests, "get", return_value=response) as get:
            self.assertEqual(mal.get_mal_part("test_user", offset=0), [])
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_username_cannot_change_mal_request_path_or_query(self):
        with patch.object(mal.requests, "get") as get:
            for username in ("../admin", "user?offset=0", "x" * 1000):
                with self.assertRaises(mal.MyanimelistException):
                    mal.get_mal_part(username)
            get.assert_not_called()

    def test_mal_network_failure_and_redirect_are_controlled_errors(self):
        with patch.object(mal.requests, "get", side_effect=requests.Timeout):
            with self.assertRaises(mal.MyanimelistException):
                mal.get_mal_part("test_user")
        with patch.object(mal.requests, "get", return_value=Mock(status_code=302)):
            with self.assertRaises(mal.MyanimelistException):
                mal.get_mal_part("test_user")

    def test_mal_pagination_has_an_upper_bound(self):
        with patch.object(mal, "get_mal_part", return_value=[{}] * 300) as get:
            with self.assertRaises(mal.MyanimelistException):
                mal.get_raw_mal("test_user")
        self.assertEqual(get.call_count, 100)

    def test_animethemes_requests_have_timeout_and_do_not_follow_redirects(self):
        response = Mock(status_code=200)
        response.json.return_value = {"anime": {}}
        with patch.object(animethemes.SESSION, "get", return_value=response) as get:
            animethemes._query_api("/anime/test", {})
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        with patch.object(animethemes.SESSION, "get", side_effect=requests.Timeout):
            with self.assertRaises(animethemes.AnimeThemesDown):
                animethemes._query_api("/anime/test", {})
