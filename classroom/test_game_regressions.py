"""Additional API atomicity, validation, and scoring lifecycle regressions."""
import copy
import json
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TransactionTestCase, override_settings

from . import engine, views
from .models import GameResult, Profile, Room
from .tests import song


@override_settings(CLASSROOM_PREWARM_ENABLED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class GameLifecycleRegressionTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()
        loader = patch("classroom.library.start_prepare")
        loader.start()
        self.addCleanup(loader.stop)
        self.host, self.alice, self.bob = Client(), Client(), Client()
        self.post(self.host, "/api/auth", {"action": "host", "key": settings.HOST_KEY})
        for client, name in ((self.alice, "Alice"), (self.bob, "Bob")):
            self.post(client, "/api/auth", {"action": "register", "name": name, "password": "test-pass",
                                            "mal": name.lower(), "avatar": 2})
        self.alice_profile = Profile.objects.get(name="Alice")
        self.alice_id = str(self.alice_profile.pk)
        self.bob_id = str(Profile.objects.get(name="Bob").pk)

    def post(self, client, path, payload, status=200):
        response = client.post(path, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, status, response.content[:500])
        return response

    def state(self):
        return Room.objects.get(pk=1).state

    def control(self, action, status=200, **fields):
        return self.post(self.host, "/api/host", {"action": action, **fields}, status=status)

    def start(self, count=1):
        with engine.room_state() as state:
            state.update(phase="ready", queue=[song(f"regression-{i}") for i in range(count)])
            state["settings"].update(fullReveal=True, autoAdvance=False)
        self.control("start")
        self.control("begin", roundId="regression-0")

    def test_failed_profile_rejoin_does_not_partially_save_profile(self):
        profile_before = (self.alice_profile.name, self.alice_profile.name_key,
                          self.alice_profile.mal_username, self.alice_profile.avatar)
        with engine.room_state() as state:
            template = copy.deepcopy(state["players"][self.bob_id])
            state["players"] = {f"other-{i}": copy.deepcopy(template) for i in range(100)}
        state_before = self.state()
        self.post(self.alice, "/api/player", {"action": "profile", "name": "Renamed Alice",
                                              "mal": "changed_mal", "avatar": 7}, status=409)
        self.alice_profile.refresh_from_db()
        self.assertEqual((self.alice_profile.name, self.alice_profile.name_key,
                          self.alice_profile.mal_username, self.alice_profile.avatar), profile_before)
        self.assertEqual(self.state(), state_before)

    def test_duplicate_profile_edit_preserves_profile_and_roster(self):
        before = self.state()
        self.post(self.alice, "/api/player", {"action": "profile", "name": "BOB",
                                              "mal": "changed_mal", "avatar": 7}, status=409)
        self.alice_profile.refresh_from_db()
        self.assertEqual(self.alice_profile.name, "Alice")
        self.assertEqual(self.alice_profile.mal_username, "alice")
        self.assertEqual(self.state(), before)

    def test_mal_statuses_require_integer_codes_and_rejected_settings_do_not_mutate_room(self):
        before = self.state()
        for statuses in ([True], [1.0], [2.0], [1, True], ["2"], [[2]], [{"status": 2}]):
            with self.subTest(statuses=statuses):
                self.control("prepare", status=400, settings={"source": "mal", "statuses": statuses})
                self.assertEqual(self.state(), before)

    def test_profile_edit_during_play_preserves_guess_and_score_identity(self):
        self.start()
        self.post(self.alice, "/api/player", {"action": "guess", "roundId": "regression-0", "text": "Attack on Titan"})
        self.post(self.alice, "/api/player", {"action": "profile", "name": "New Alice", "mal": "new_mal", "avatar": 7})
        self.control("reveal")
        state = self.state()
        self.assertEqual(state["players"][self.alice_id]["name"], "New Alice")
        self.assertEqual(state["players"][self.alice_id]["score"], 1)
        self.assertEqual(state["history"][0]["results"][self.alice_id], {"guess": "Attack on Titan", "correct": True})
        self.control("next")
        self.assertEqual(GameResult.objects.get().result["players"][self.alice_id]["score"], 1)

    def test_completed_game_is_immutable_to_repeated_round_commands(self):
        self.start()
        self.post(self.alice, "/api/player", {"action": "guess", "roundId": "regression-0", "text": "Attack on Titan"})
        self.control("reveal")
        self.control("next")
        before = self.state()
        saved_result = copy.deepcopy(GameResult.objects.get().result)
        for action in ("start", "begin", "next", "reveal-ended", "pause", "void", "correct", "reveal"):
            with self.subTest(action=action):
                self.control(action, status=400 if action == "reveal" else 409,
                             roundId="regression-0", playerId=self.alice_id)
                self.assertEqual(self.state(), before)
                self.assertEqual(GameResult.objects.count(), 1)
                self.assertEqual(GameResult.objects.get().result, saved_result)

    def test_repeated_skip_with_previous_round_id_cannot_skip_the_next_song(self):
        self.start(count=3)
        self.control("void", roundId="regression-0")
        self.assertEqual(self.state()["index"], 1)
        before = self.state()
        self.control("void", status=409, roundId="regression-0")
        self.assertEqual(self.state(), before)
        self.control("begin", roundId="regression-1")
        self.control("void", roundId="regression-1")
        self.assertEqual(self.state()["index"], 2)

    def test_delayed_host_controls_cannot_change_a_later_round(self):
        self.start(count=2)
        self.control("reveal", roundId="regression-0")
        self.control("next", roundId="regression-0")
        self.control("begin", roundId="regression-1")
        before = self.state()
        for action in ("pause", "reveal", "void"):
            with self.subTest(action=action):
                self.control(action, status=409, roundId="regression-0")
                self.assertEqual(self.state(), before)
        self.control("pause", roundId="regression-1")
        self.assertTrue(self.state()["paused"])
        self.control("pause", roundId="regression-1")
        self.control("reveal", roundId="regression-1")
        before = self.state()
        for action in ("next", "correct"):
            with self.subTest(action=action):
                self.control(action, status=409, roundId="regression-0", playerId=self.alice_id)
                self.assertEqual(self.state(), before)
        self.control("correct", roundId="regression-1", playerId=self.alice_id)
        self.assertEqual(self.state()["players"][self.alice_id]["score"], 1)
        self.control("next", roundId="regression-1")
        self.assertEqual(self.state()["phase"], "finished")

    def test_multiple_round_corrections_and_voids_keep_scores_equal_to_results(self):
        self.start(count=8)
        expected = {self.alice_id: 0, self.bob_id: 0}
        for index in range(8):
            round_id = f"regression-{index}"
            if index:
                self.control("begin", roundId=round_id)
            for client, text in ((self.alice, "Attack on Titan"), (self.bob, "Close answer")):
                self.post(client, "/api/player", {"action": "guess", "roundId": round_id, "text": text})
            self.control("reveal")
            expected[self.alice_id] += 1
            self.control("correct", playerId=self.bob_id)
            expected[self.bob_id] += 1
            # Re-reviewing an answer returns exactly to the prior score.
            self.control("correct", playerId=self.alice_id)
            self.control("correct", playerId=self.alice_id)
            if index % 2:
                self.control("void")
                expected[self.alice_id] -= 1
                expected[self.bob_id] -= 1
            else:
                self.control("next")
            state = self.state()
            self.assertEqual({key: player["score"] for key, player in state["players"].items()}, expected)
            self.assertEqual(len(state["history"]), index + 1)
        result = GameResult.objects.get().result
        self.assertEqual({key: player["score"] for key, player in result["players"].items()}, expected)
        self.assertEqual([record["void"] for record in result["history"]], [False, True] * 4)
