"""Independent provider corruption and interrupted-stream regressions."""
import copy
from unittest.mock import patch

from django.test import SimpleTestCase

from . import engine, library, metadata
from .test_library_security import provider_response
from .test_metadata_cache import anime


class ProviderFaultTests(SimpleTestCase):
    def test_excessively_nested_provider_json_is_a_controlled_error(self):
        payload = b'[' * 2000 + b'0' + b']' * 2000
        with patch("classroom.metadata.wait_for_provider"), patch("classroom.metadata.http_session") as session:
            response = provider_response(chunks=(payload,))
            session.return_value.get.return_value = response
            with self.assertRaises(engine.GameError):
                metadata.get_json(library.API + "/anime")
            response.__exit__.assert_called_once()

    def test_non_finite_provider_numbers_are_rejected_but_text_is_preserved(self):
        for token in (b'NaN', b'Infinity', b'-Infinity', b'1e9999'):
            with self.subTest(token=token), patch("classroom.metadata.wait_for_provider"), patch(
                    "classroom.metadata.http_session") as session:
                session.return_value.get.return_value = provider_response(chunks=(b'{"value":' + token + b'}',))
                with self.assertRaises(engine.GameError):
                    metadata.get_json(library.API + "/anime")
        with patch("classroom.metadata.wait_for_provider"), patch("classroom.metadata.http_session") as session:
            session.return_value.get.return_value = provider_response(chunks=(b'{"title":"Infinity"}',))
            self.assertEqual(metadata.get_json(library.API + "/anime"), {"title": "Infinity"})

    def test_cancelled_response_stream_closes_before_reading_the_remaining_body(self):
        cancelled = False
        consumed = []
        def check():
            if cancelled:
                raise InterruptedError()
        def chunks():
            nonlocal cancelled
            consumed.append(1)
            yield b'{"anime":'
            cancelled = True
            consumed.append(2)
            yield b'[]}'
            consumed.append(3)
            yield b' '
        response = provider_response()
        response.iter_content.return_value = chunks()
        with metadata.work_context(check=check), patch("classroom.metadata.wait_for_provider"), patch(
                "classroom.metadata.http_session") as session:
            session.return_value.get.return_value = response
            with self.assertRaises(InterruptedError):
                metadata.get_json(library.API + "/anime")
        self.assertEqual(consumed, [1, 2])
        response.__exit__.assert_called_once()

    def test_malformed_optional_metadata_cannot_discard_a_playable_theme(self):
        options = engine.validate_settings({})
        fixture = anime(1)
        fixture["animesynonyms"] = [None, "broken", {"text": {"bad": 1}}, {"text": "Valid alias"}]
        fixture["animethemes"][0]["song"] = {"title": None, "artists": [None, {}, {"name": ["bad"]}, {"name": "Valid artist"}]}
        original = copy.deepcopy(fixture)
        songs = library.songs_from_anime(fixture, "List title", options)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["aliases"], ["List title", "Valid alias"])
        self.assertEqual(songs[0]["song"], "Unknown title")
        self.assertEqual(songs[0]["artist"], "Valid artist")
        self.assertEqual(fixture, original)

    def test_corrupt_theme_and_video_records_do_not_hide_a_healthy_video(self):
        for broken in (None, "broken", {"link": None}, {"link": "https://v.animethemes.moe:bad/v.webm"},
                       {"link": "https://v.animethemes.moe/bad.webm", "resolution": "HD"},
                       {"link": "https://v.animethemes.moe/bad.webm", "resolution": 10 ** 400},
                       {"link": "https://v.animethemes.moe/bad.webm", "size": {"bytes": 1}}):
            with self.subTest(broken=broken):
                fixture = anime(1)
                fixture["animethemes"].insert(0, None)
                entry = fixture["animethemes"][1]["animethemeentries"][0]
                fixture["animethemes"][1]["animethemeentries"].insert(0, None)
                entry["videos"].insert(0, broken)
                songs = library.songs_from_anime(fixture, "List title", engine.validate_settings({}))
                self.assertEqual([song["media"] for song in songs], ["https://v.animethemes.moe/1.webm"])

    def test_malformed_openings_song_labels_do_not_break_an_eligible_record(self):
        for song in (None, "broken", {"title": ["bad"], "artist": {"bad": 1}}):
            with self.subTest(song=song), patch("classroom.library.openings_catalog", return_value={
                    "fixture": [{"uid": "Opening1-fixture", "source": "Fixture", "song": song}]}), patch(
                    "classroom.library.openings_details", return_value={"file": "fixture"}):
                songs = library.songs_from_openings([(1, "Fixture")], engine.validate_settings({}), 1)
                self.assertEqual(len(songs), 1)
                self.assertEqual(songs[0]["song"], "Unknown title")
                self.assertEqual(songs[0]["artist"], "Artist not listed")
