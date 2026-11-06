"""Playback-driven reveals and host-exclusive round controls."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from . import engine, views
from .models import GameResult, Room
from .tests import song


class FullRevealSettingsTests(SimpleTestCase):
    def test_full_reveals_are_opt_in_and_require_a_boolean(self):
        self.assertIs(engine.validate_settings({})["fullReveal"], False)
        self.assertIs(engine.validate_settings({"fullReveal": True})["fullReveal"], True)
        for value in ("false", 0, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaisesMessage(engine.GameError, "fullReveal"):
                engine.validate_settings({"fullReveal": value})


@override_settings(CLASSROOM_PREWARM_ENABLED=False)
class FullRevealTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()
        self.host, self.player = Client(), Client()
        self.post(self.host, "/api/auth", {"action": "host", "key": settings.HOST_KEY})
        self.post(self.player, "/api/auth", {"action": "register", "name": "Alice", "password": "test-pass"})

    def post(self, client, path, data, status=200):
        response = client.post(path, json.dumps(data), content_type="application/json")
        self.assertEqual(response.status_code, status, response.content)
        return response

    def control(self, action, status=200, **data):
        return self.post(self.host, "/api/host", {"action": action, **data}, status)

    def start(self, count=2, auto=True, full=True):
        options = engine.validate_settings({"fullReveal": full, "autoAdvance": auto})
        with engine.room_state() as state:
            state.update(phase="ready", settings=options,
                         queue=[song(f"full-{i}", startAt=17) for i in range(count)])
        self.control("start")
        self.control("begin", roundId="full-0")
        self.post(self.player, "/api/player", {"action": "guess", "roundId": "full-0", "text": "Attack on Titan"})

    def test_full_reveal_waits_for_playback_even_after_old_timer_would_expire(self):
        self.start()
        self.control("reveal")
        state = Room.objects.get(pk=1).state
        self.assertIsNone(state["deadline"])
        # A stale timer must not bypass the playback requirement either.
        state["deadline"] = time.time() - 100
        Room.objects.filter(pk=1).update(state=state)
        for _ in range(3):
            state = self.player.get("/api/state").json()
            self.assertEqual(state["phase"], "reveal")
            self.assertEqual(state["roundId"], "full-0")
            self.assertEqual(state["players"][0]["score"], 1)
        self.assertEqual(len(Room.objects.get(pk=1).state["history"]), 1)

    def test_host_completion_advances_once_and_stale_event_cannot_skip_next_round(self):
        self.start()
        self.control("reveal")
        self.control("reveal-ended", roundId="full-0")
        state = self.host.get("/api/state").json()
        self.assertEqual((state["phase"], state["roundId"]), ("buffering", "full-1"))
        self.control("begin", roundId="full-1")
        self.control("reveal-ended", status=409, roundId="full-0")
        state = self.host.get("/api/state").json()
        self.assertEqual((state["phase"], state["roundId"]), ("guess", "full-1"))
        self.assertEqual(state["players"][0]["score"], 1)

    def test_manual_advance_stays_with_host_after_video_finishes(self):
        self.start(auto=False)
        self.control("reveal")
        self.control("reveal-ended", roundId="full-0")
        self.control("reveal-ended", roundId="full-0")
        state = self.host.get("/api/state").json()
        self.assertEqual(state["phase"], "reveal")
        self.assertTrue(state["revealEnded"])
        self.assertEqual(state["players"][0]["score"], 1)
        self.control("next")
        self.assertEqual(self.host.get("/api/state").json()["roundId"], "full-1")

    def test_full_reveal_pause_resume_and_completion_have_no_timer(self):
        self.start()
        self.control("reveal")
        self.control("pause")
        state = self.host.get("/api/state").json()
        self.assertTrue(state["paused"])
        self.assertIsNone(state["deadline"])
        self.assertIsNone(state["remaining"])
        self.control("reveal-ended", status=409, roundId="full-0")
        self.control("pause")
        self.control("reveal-ended", roundId="full-0")
        self.assertEqual(self.host.get("/api/state").json()["roundId"], "full-1")

    def test_restart_pauses_full_reveal_and_host_can_resume(self):
        self.start(auto=False)
        self.control("reveal")
        engine.recover_after_restart()
        state = self.host.get("/api/state").json()
        self.assertEqual(state["phase"], "reveal")
        self.assertTrue(state["paused"])
        self.assertIsNone(state["remaining"])
        self.control("pause")
        self.control("reveal-ended", roundId="full-0")
        self.assertTrue(self.host.get("/api/state").json()["revealEnded"])
        self.assertEqual(len(Room.objects.get(pk=1).state["history"]), 1)

    def test_host_can_advance_early_or_void_with_correct_scores(self):
        for action, expected_score in [("next", 1), ("void", 0)]:
            with self.subTest(action=action):
                self.start()
                self.control("reveal")
                self.control(action)
                state = self.host.get("/api/state").json()
                self.assertEqual(state["roundId"], "full-1")
                self.assertEqual(state["players"][0]["score"], expected_score)
                self.assertEqual(state["history"][0]["void"], action == "void")

    def test_player_and_spectator_skip_attempts_cannot_mutate_even_an_expired_round(self):
        self.start()
        spectator = Client()
        self.post(spectator, "/api/auth", {"action": "register", "name": "Spectator", "password": "test-pass"})
        state = Room.objects.get(pk=1).state
        state["deadline"] = time.time() - 100
        Room.objects.filter(pk=1).update(state=state)
        before = Room.objects.get(pk=1).state
        for client in (self.player, spectator):
            for action in ("skip", "void", "next", "reveal-ended"):
                for path in ("/api/player", "/api/host"):
                    with self.subTest(path=path, action=action):
                        self.post(client, path, {"action": action, "roundId": "full-0"}, status=403)
        self.post(Client(), "/api/host", {"action": "void"}, status=403)
        self.assertEqual(Room.objects.get(pk=1).state, before)

    def test_player_cannot_end_full_reveal_or_advance_it(self):
        self.start()
        self.control("reveal")
        before = Room.objects.get(pk=1).state
        for path in ("/api/player", "/api/host"):
            for action in ("void", "next", "reveal-ended"):
                self.post(self.player, path, {"action": action, "roundId": "full-0"}, status=403)
        self.assertEqual(Room.objects.get(pk=1).state, before)

    def test_concurrent_completion_events_finish_game_once(self):
        self.start(count=1)
        self.control("reveal")
        def complete(_):
            close_old_connections()
            try:
                with engine.room_state() as state:
                    try:
                        engine.control(state, "reveal-ended", {"roundId": "full-0"})
                    except engine.GameError as exc:
                        self.assertEqual(exc.status, 409)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(complete, range(8)))
        self.assertEqual(GameResult.objects.count(), 1)
        self.assertEqual(Room.objects.get(pk=1).state["phase"], "finished")

    def test_disabled_setting_keeps_normal_reveal_timer(self):
        self.start(full=False)
        self.control("reveal")
        self.control("reveal-ended", status=409, roundId="full-0")
        state = Room.objects.get(pk=1).state
        self.assertIsNotNone(state["deadline"])
        state["deadline"] = time.time() - 1
        Room.objects.filter(pk=1).update(state=state)
        self.assertEqual(self.player.get("/api/state").json()["roundId"], "full-1")

    def test_upgrade_defaults_to_existing_timed_reveals(self):
        with engine.room_state() as state:
            state["settings"].pop("fullReveal")
        engine.recover_after_restart()
        self.assertIs(Room.objects.get(pk=1).state["settings"]["fullReveal"], False)
