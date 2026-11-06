import copy
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings

from . import engine, library, views
from .models import CachedResource, GameResult, Profile, Room


def song(round_id="round-one", **changes):
    return {"id": "test-song", "roundId": round_id, "anime": "Shingeki no Kyojin",
        "aliases": ["Attack on Titan", "進撃の巨人"], "song": "A secret song name", "artist": "Test artist",
        "type": "OP", "year": 2013, "media": "test.wav", "startAt": 0, **changes}


class ClassroomTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()
        self.host, self.alice, self.bob = Client(), Client(), Client()
        self.post(self.host, "/api/auth", {"action": "host", "key": settings.HOST_KEY})
        self.register(self.alice, "Alice", "alice_mal")
        self.register(self.bob, "Bob", "bob_mal")
        self.alice_id = str(Profile.objects.get(name="Alice").pk)
        self.bob_id = str(Profile.objects.get(name="Bob").pk)

    def post(self, client, path, data, expected=200):
        response = client.post(path, json.dumps(data), content_type="application/json")
        self.assertEqual(response.status_code, expected, response.content.decode()[:400])
        return response.json()

    def register(self, client, name, mal=""):
        return self.post(client, "/api/auth", {"action": "register", "name": name,
            "password": "test-passcode", "mal": mal, "avatar": 2})

    def seed(self, count=2):
        with engine.room_state() as s:
            s.update(phase="ready", queue=[song(f"round-{i}") for i in range(count)],
                     titles=["Attack on Titan", "Cowboy Bebop"])

    def control(self, action, **data):
        return self.post(self.host, "/api/host", {"action": action, **data})

    def start(self, count=2):
        self.seed(count)
        self.control("start")
        self.control("begin", roundId="round-0")

    def answer(self, client, text, expected=200, round_id="round-0"):
        return self.post(client, "/api/player", {"action": "guess", "text": text, "roundId": round_id}, expected)

    def state(self, client=None):
        return (client or self.alice).get("/api/state").json()

    def test_profiles_survive_logout_and_rejoin_with_saved_mal(self):
        self.post(self.alice, "/api/logout", {"role": "player"})
        self.assertIsNone(self.state()["me"])
        self.post(self.alice, "/api/auth", {"action": "login", "name": "alice", "password": "test-passcode"})
        self.assertEqual(self.state()["me"]["mal"], "alice_mal")
        self.assertEqual(Profile.objects.count(), 2)
        self.assertNotEqual(Profile.objects.get(name="Alice").password, "test-passcode")

    def test_duplicate_profile_and_wrong_passcode_do_not_overwrite(self):
        self.post(Client(), "/api/auth", {"action": "register", "name": "ALICE", "password": "other"}, 409)
        self.post(Client(), "/api/auth", {"action": "login", "name": "Alice", "password": "wrong"}, 403)
        self.assertEqual(Profile.objects.get(name="Alice").mal_username, "alice_mal")

    def test_guesses_and_song_metadata_are_private_until_reveal(self):
        self.start()
        self.answer(self.alice, "Attack on Titan")
        bob = self.state(self.bob)
        self.assertIsNone(bob["answer"])
        self.assertNotIn("mediaUrl", bob)
        self.assertNotIn("A secret song name", json.dumps(bob))
        self.assertEqual(bob["myGuess"], "")
        self.assertTrue(next(p for p in bob["players"] if p["id"] == self.alice_id)["submitted"])
        self.assertIsNone(next(p for p in bob["players"] if p["id"] == self.alice_id)["result"])
        self.assertEqual(self.bob.get("/api/media/round-0").status_code, 403)

    def test_aliases_punctuation_and_revisions_score_once(self):
        self.start()
        self.answer(self.alice, "Cowboy Bebop")
        self.answer(self.alice, "  ATTACK—ON TITAN!!  ")
        self.answer(self.bob, "進撃の巨人")
        self.control("reveal")
        for _ in range(4):
            scores = [p["score"] for p in self.state()["players"]]
            self.assertEqual(scores, [1, 1])
        self.post(self.host, "/api/host", {"action": "reveal"}, 400)
        self.assertEqual(len(Room.objects.get(pk=1).state["history"]), 1)

    def test_deadline_closes_answers_on_server(self):
        self.start()
        self.answer(self.alice, "Attack on Titan")
        with engine.room_state() as s:
            s["deadline"] = time.time() - 1
        self.answer(self.bob, "Attack on Titan", 409)
        state = self.state()
        self.assertEqual(state["phase"], "reveal")
        scores = {p["name"]: p["score"] for p in state["players"]}
        self.assertEqual(scores, {"Alice": 1, "Bob": 0})

    def test_pausing_freezes_time_and_blocks_guesses(self):
        self.start()
        self.control("pause")
        s = self.state()
        self.assertTrue(s["paused"])
        self.assertIsNone(s["deadline"])
        self.answer(self.alice, "Attack on Titan", 409)
        self.control("pause")
        resumed = self.state()
        self.assertAlmostEqual(resumed["deadline"] - resumed["serverTime"], s["remaining"], delta=0.3)
        self.answer(self.alice, "Attack on Titan")

    def test_late_join_is_spectator_then_plays_next_game(self):
        self.start()
        late = Client()
        self.register(late, "Latecomer")
        self.answer(late, "Attack on Titan", 403)
        self.assertTrue(next(p for p in self.state(late)["players"] if p["name"] == "Latecomer")["spectator"])
        self.control("reset")
        self.start()
        self.answer(late, "Attack on Titan")

    def test_stale_guess_or_audio_event_cannot_affect_next_round(self):
        self.start()
        self.control("reveal")
        self.control("next")
        self.post(self.host, "/api/host", {"action": "begin", "roundId": "round-0"}, 409)
        self.control("begin", roundId="round-1")
        self.answer(self.alice, "Attack on Titan", 409)
        self.answer(self.alice, "Attack on Titan", round_id="round-1")

    def test_host_can_correct_and_void_a_round_without_negative_scores(self):
        self.start()
        self.answer(self.alice, "Attack on Titan")
        self.answer(self.bob, "Almost")
        self.control("reveal")
        self.control("correct", playerId=self.bob_id)
        self.assertEqual([p["score"] for p in self.state()["players"]], [1, 1])
        self.control("void")
        self.assertEqual([p["score"] for p in self.state()["players"]], [0, 0])
        self.assertTrue(Room.objects.get(pk=1).state["history"][0]["void"])

    def test_completed_game_and_profiles_persist_across_new_clients(self):
        self.start(1)
        self.answer(self.alice, "Attack on Titan")
        self.control("reveal")
        self.control("next")
        self.assertEqual(self.state()["phase"], "finished")
        self.assertEqual(GameResult.objects.count(), 1)
        self.assertEqual(GameResult.objects.get().result["players"][self.alice_id]["score"], 1)
        returning = Client()
        self.post(returning, "/api/auth", {"action": "login", "name": "Alice", "password": "test-passcode"})
        self.assertEqual(self.state(returning)["phase"], "finished")

    def test_concurrent_clock_ticks_finalize_game_once(self):
        self.start(1)
        self.answer(self.alice, "Attack on Titan")
        self.control("reveal")
        with engine.room_state() as s:
            s["settings"]["autoAdvance"] = True
            s["deadline"] = time.time() - 1
        def tick():
            close_old_connections()
            try:
                with engine.room_state() as s:
                    return s["phase"]
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=8) as pool:
            phases = list(pool.map(lambda _: tick(), range(16)))
        self.assertEqual(set(phases), {"finished"})
        self.assertEqual(GameResult.objects.count(), 1)

    def test_restart_pauses_before_an_expired_round_is_scored(self):
        self.start()
        self.answer(self.alice, "Attack on Titan")
        with engine.room_state() as s:
            s["deadline"] = time.time() - 120
        engine.recover_after_restart()
        s = self.state()
        self.assertEqual(s["phase"], "guess")
        self.assertTrue(s["paused"])
        self.assertEqual(s["remaining"], 1)
        self.assertEqual(s["history"], [])
        self.assertEqual(s["myGuess"], "Attack on Titan")

    def test_host_commands_require_host_auth_and_csrf(self):
        self.post(self.alice, "/api/host", {"action": "reset"}, 403)
        self.assertEqual(self.alice.get("/api/results.csv").status_code, 403)
        strict = Client(enforce_csrf_checks=True)
        response = strict.post("/api/auth", json.dumps({"action": "host", "key": settings.HOST_KEY}), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        strict.get("/")
        response = strict.post("/api/auth", json.dumps({"action": "host", "key": settings.HOST_KEY}), content_type="application/json", HTTP_X_CSRFTOKEN=strict.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 200)

    def test_settings_and_payload_validation(self):
        for changes in [{"rounds": 0}, {"rounds": True}, {"types": []}, {"source": "unknown"}, {"guessSeconds": 1000}]:
            with self.assertRaises(engine.GameError):
                engine.validate_settings({**engine.DEFAULTS, **changes})
        self.assertEqual(self.host.get("/api/host").status_code, 405)
        self.assertEqual(self.host.post("/api/host", "[]", content_type="application/json").status_code, 400)

    def test_local_media_supports_seeking_and_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(DATA_DIR=Path(directory)):
            base = Path(directory) / "media"
            base.mkdir()
            (base / "test.wav").write_bytes(b"0123456789")
            self.start()
            response = self.host.get("/api/media/round-0", HTTP_RANGE="bytes=2-5")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join(response.streaming_content), b"2345")
            self.assertEqual(response["Content-Range"], "bytes 2-5/10")
            response = self.host.get("/api/media/round-0", HTTP_RANGE="bytes=-3")
            self.assertEqual(b"".join(response.streaming_content), b"789")
            self.assertEqual(self.host.get("/api/media/round-0", HTTP_RANGE="bytes=99-").status_code, 416)
            with self.assertRaises(engine.GameError):
                library.media_path("../.host-key")

    def test_csv_neutralizes_spreadsheet_formulas(self):
        self.start(1)
        self.answer(self.alice, '=HYPERLINK("https://example.test")')
        self.control("reveal")
        csv = self.host.get("/api/results.csv").content.decode()
        self.assertIn("'=HYPERLINK", csv)

    def test_starter_preparation_has_real_metadata_and_no_repeated_themes(self):
        with engine.room_state() as s:
            s.update(phase="loading", loadId="test-load")
        library.prepare("test-load", engine.validate_settings({"rounds": 25}), [])
        s = Room.objects.get(pk=1).state
        self.assertEqual(s["phase"], "ready", s["message"])
        self.assertEqual(len(s["queue"]), 25)
        self.assertEqual(len({song["id"] for song in s["queue"]}), 25)
        self.assertTrue(all(song["media"].startswith("https://v.animethemes.moe/") for song in s["queue"]))
        self.assertIn("Attack on Titan", s["titles"])

    def test_mal_pagination_and_cache(self):
        page = [{"anime_id": n, "anime_title": f"Anime {n}", "status": 2} for n in range(300)]
        with patch("classroom.library.get_json", side_effect=[page, [{"anime_id": 999}]]) as fetch:
            self.assertEqual(len(library.mal_list("test_user")), 301)
            self.assertEqual(len(library.mal_list("test_user")), 301)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args_list[1].args[1]["offset"], 300)

    def test_missing_mal_does_not_silently_use_somebody_elses_list(self):
        with engine.room_state() as s:
            s.update(phase="loading", loadId="no-users")
        library.prepare("no-users", engine.validate_settings({"source": "mal"}), [])
        s = Room.objects.get(pk=1).state
        self.assertEqual(s["phase"], "lobby")
        self.assertIn("MAL username", s["message"])
        self.assertEqual(s["queue"], [])

    def test_reset_cancels_stale_song_loader(self):
        self.control("reset")
        library.prepare("stale", engine.validate_settings({}), [])
        self.assertEqual(Room.objects.get(pk=1).state["phase"], "lobby")

    def test_local_catalog_keeps_only_allowed_file_paths(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(DATA_DIR=Path(directory)):
            Path(directory, "catalog.json").write_text(json.dumps([song(media="../secret.wav")]))
            with self.assertRaises(engine.GameError):
                library.local_catalog()

    def test_qr_and_page_work_without_external_browser_assets(self):
        self.assertEqual(self.alice.get("/").status_code, 200)
        self.assertEqual(self.host.get("/host").status_code, 200)
        self.assertIn(b"<svg", self.alice.get("/api/qr").content)
        self.assertEqual(self.alice.get("/assets/secret").status_code, 404)
