"""Seeded, independent score-ledger checks across long mixed game histories."""
import copy
import random
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from . import engine
from .models import GameResult, Room


@override_settings(CLASSROOM_PREWARM_ENABLED=False)
class SeededGameModelTests(TransactionTestCase):
    SEEDS = tuple(range(8))
    GAMES_PER_MODE = 2
    ROUNDS_PER_GAME = 10

    def setUp(self):
        self.clock = 10_000.0
        clock = patch("classroom.engine.time", SimpleNamespace(time=lambda: self.clock))
        clock.start()
        self.addCleanup(clock.stop)
        self.next_player = 0

    def snapshot(self):
        return Room.objects.get(pk=1).state

    def tick(self):
        with engine.room_state() as state:
            return copy.deepcopy(state)

    def control(self, action, **data):
        with engine.room_state() as state:
            engine.control(state, action, data)

    def submit(self, player_id, text, round_id):
        with engine.room_state() as state:
            engine.guess(state, player_id, text, round_id)

    def join(self):
        self.next_player += 1
        player = SimpleNamespace(pk=self.next_player, name=f"Player {self.next_player}",
                                 mal_username="", avatar=self.next_player % 8)
        with engine.room_state() as state:
            engine.join(state, player)
        return str(player.pk)

    def reject(self, operation, status=None):
        before = self.snapshot()
        with self.assertRaises(engine.GameError) as rejected:
            operation()
        if status is not None:
            self.assertEqual(rejected.exception.status, status)
        self.assertEqual(self.snapshot(), before, "Rejected operation partly changed the saved room")

    def check_ledger(self, scores, history):
        state = self.snapshot()
        self.assertEqual({key: p["score"] for key, p in state["players"].items()}, scores)
        self.assertEqual(state["history"], history)
        self.assertTrue(all(score >= 0 for score in scores.values()))
        # Recompute totals from the independent expected history, not from
        # either the production score updates or the actual stored results.
        for key, score in scores.items():
            total = sum(int(h["results"].get(key, {}).get("correct", False))
                        for h in history if not h["void"])
            self.assertEqual(score, total)

    def pause_and_recover(self, rng, phase):
        before = self.snapshot()
        self.control("pause", roundId=engine.current(before)["roundId"])
        paused = self.snapshot()
        self.assertTrue(paused["paused"])
        self.assertIsNone(paused["deadline"])
        self.clock += rng.randint(100, 1000)
        self.assertEqual(self.tick(), paused, "A paused clock advanced the game")
        engine.recover_after_restart()
        self.assertEqual(self.snapshot(), paused, "Restart changed an already paused round")
        self.control("pause", roundId=engine.current(paused)["roundId"])
        resumed = self.snapshot()
        self.assertEqual(resumed["phase"], phase)
        self.assertFalse(resumed["paused"])
        self.assertEqual(resumed["players"], before["players"])
        self.assertEqual(resumed["history"], before["history"])

    def run_match(self, rng, *, full, auto, label):
        self.control("reset")
        # Keep each match a classroom-sized sample while continually churning
        # identities between matches; accumulating every spectator would test
        # the separate roster cap rather than score/recovery transitions.
        for removable in list(self.snapshot()["players"])[4:]:
            self.control("remove", playerId=removable)
        if len(self.snapshot()["players"]) < 3:
            for _ in range(3 - len(self.snapshot()["players"])):
                self.join()
        # Exercise removal/rejoin between games without carrying old scores.
        if rng.choice((False, True)):
            removable = next(iter(self.snapshot()["players"]))
            self.control("remove", playerId=removable)
            self.join()
        queue = [{"id": f"theme-{i}", "roundId": f"{label}-{i}", "anime": "Galaxy 7",
                  "aliases": ["Seven Stars"], "song": f"Song {i}", "artist": "Synthetic",
                  "type": "OP", "media": "synthetic.webm", "startAt": 13}
                 for i in range(self.ROUNDS_PER_GAME)]
        with engine.room_state() as state:
            state.update(phase="ready", queue=queue)
            state["settings"].update(fullReveal=full, autoAdvance=auto, guessSeconds=90, revealSeconds=5)
        self.control("start")
        active = set(self.snapshot()["players"])
        scores = dict.fromkeys(active, 0)
        history = []
        correct_texts = ("Galaxy 7", "galaxy7", "ＧＡＬＡＸＹ ７", " Seven Stars ")
        wrong_texts = ("Wrong", "Galaxy 8", "Seven Star")
        for index, song in enumerate(queue):
            round_id = song["roundId"]
            with self.subTest(round=index):
                self.assertEqual(self.snapshot()["phase"], "buffering")
                self.reject(lambda: self.control("begin", roundId="an older game"), 409)
                if index in (1, 5):
                    spectator = self.join()
                    scores[spectator] = 0
                    self.assertTrue(self.snapshot()["players"][spectator]["spectator"])
                if rng.randrange(8) == 0:
                    self.control("void", roundId=round_id)
                    history.append({"roundId": round_id, "anime": "Galaxy 7", "song": song["song"],
                                    "void": True, "results": {}})
                    self.check_ledger(scores, history)
                    continue

                self.control("begin", roundId=round_id)
                guesses = {}
                expected_correct = {}
                for key in sorted(active):
                    for _ in range(rng.randrange(4)):
                        correct = rng.choice((False, True))
                        text = rng.choice(correct_texts if correct else wrong_texts)
                        self.submit(key, text, round_id)
                        guesses[key], expected_correct[key] = text.strip(), correct
                for key in set(scores) - active:
                    self.reject(lambda: self.submit(key, "Galaxy 7", round_id), 403)
                first = next(iter(active))
                self.reject(lambda: self.submit(first, "Galaxy 7", "stale-round"), 409)
                bad_text = rng.choice((None, [], {}, 12, "", "   ", "x" * 201))
                self.reject(lambda: self.submit(first, bad_text, round_id))
                public = engine.public_state(self.snapshot(), first)
                self.assertIsNone(public["answer"])
                self.assertNotIn("mediaUrl", public)
                self.assertTrue(all(p["result"] is None for p in public["players"]))
                if rng.choice((False, True)):
                    self.pause_and_recover(rng, "guess")
                if rng.randrange(4) == 0:
                    engine.recover_after_restart()
                    self.assertTrue(self.snapshot()["paused"])
                    self.reject(lambda: self.submit(first, "Galaxy 7", round_id), 409)
                    self.control("pause", roundId=round_id)

                if rng.choice((False, True)):
                    # A rejected guess may trigger an in-transaction expiry;
                    # neither its score nor history may survive the rollback.
                    self.clock = self.snapshot()["deadline"] + 1
                    self.reject(lambda: self.submit(first, "Galaxy 7", round_id), 409)
                    self.tick()
                else:
                    self.control("reveal", roundId=round_id)
                results = {key: {"guess": guesses.get(key, ""), "correct": expected_correct.get(key, False)}
                           for key in active}
                history.append({"roundId": round_id, "anime": "Galaxy 7", "song": song["song"],
                                "void": False, "results": results})
                for key, result in results.items():
                    scores[key] += int(result["correct"])
                self.check_ledger(scores, history)
                for _ in range(rng.randrange(5)):
                    key = rng.choice(sorted(active))
                    previous = results[key]["correct"]
                    self.control("correct", roundId=round_id, playerId=key)
                    results[key].update(correct=not previous, reviewed=True)
                    scores[key] += -1 if previous else 1
                    self.check_ledger(scores, history)
                for key in set(scores) - active:
                    self.reject(lambda: self.control("correct", roundId=round_id, playerId=key), 404)
                if rng.choice((False, True)):
                    self.pause_and_recover(rng, "reveal")
                if full:
                    self.assertIsNone(self.snapshot()["deadline"])
                    self.clock += rng.randint(50, 500)
                    self.assertEqual(self.tick()["phase"], "reveal")
                ending = rng.choice(("void", "next", "complete"))
                if ending == "void":
                    self.control("void", roundId=round_id)
                    history[-1]["void"] = True
                    for key, result in results.items():
                        scores[key] -= int(result["correct"])
                elif ending == "next":
                    self.control("next", roundId=round_id)
                elif full:
                    self.control("reveal-ended", roundId=round_id)
                    if not auto:
                        self.assertEqual(self.snapshot()["phase"], "reveal")
                        self.assertTrue(self.snapshot()["revealEnded"])
                        self.control("reveal-ended", roundId=round_id)
                        self.control("next", roundId=round_id)
                else:
                    self.clock = self.snapshot()["deadline"] + 1
                    self.tick()
                    if not auto:
                        self.assertEqual(self.snapshot()["phase"], "reveal")
                        self.control("next", roundId=round_id)
                self.check_ledger(scores, history)
                self.reject(lambda: self.control("reveal-ended", roundId=round_id), 409)
        state = self.snapshot()
        self.assertEqual(state["phase"], "finished")
        self.assertIsNone(state["deadline"])
        saved = GameResult.objects.get(result__gameId=state["gameId"]).result
        self.assertEqual(saved["history"], history)
        self.assertEqual({key: p["score"] for key, p in saved["players"].items()}, scores)
        count = GameResult.objects.count()
        for _ in range(5):
            self.tick()
        self.assertEqual(GameResult.objects.count(), count)
        for action in ("start", "pause", "begin", "reveal", "next", "correct", "void"):
            self.reject(lambda: self.control(action, roundId=queue[-1]["roundId"], playerId=first))

    def test_seeded_matches_keep_independent_score_ledger_and_recovery_invariants(self):
        for seed in self.SEEDS:
            rng = random.Random(seed)
            for full in (False, True):
                for auto in (False, True):
                    for game in range(self.GAMES_PER_MODE):
                        label = f"seed-{seed}-{full}-{auto}-{game}"
                        with self.subTest(seed=seed, full=full, auto=auto, game=game):
                            self.run_match(rng, full=full, auto=auto, label=label)
