"""Authoritative room state. One Waitress process, many threads; SQLite persists it.

Clients never choose a score, clock, answer, or round. All transitions are serialized
and committed atomically. Polling also ticks the clock, so no browser owns timing.
"""
import copy
import secrets
import threading
import time
import unicodedata
from contextlib import contextmanager

from django.db import transaction
from .models import GameResult, Room

LOCK = threading.RLock()
DEFAULTS = {
    "rounds": 10, "guessSeconds": 20, "revealSeconds": 12,
    "types": ["OP", "ED"], "statuses": [1, 2], "source": "starter",
    "autoAdvance": False, "randomStart": True, "allowSpoilers": False,
}


class GameError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value)).casefold()
                   if c.isalnum())


def initial():
    return {"phase": "lobby", "settings": copy.deepcopy(DEFAULTS), "players": {},
            "queue": [], "index": -1, "guesses": {}, "history": [], "titles": [],
            "deadline": None, "paused": False, "remaining": None,
            "message": "", "loadProgress": 0, "loadId": "", "gameId": ""}


def recover_after_restart():
    """Pause before ticking an expired persisted deadline after downtime."""
    with LOCK, transaction.atomic():
        room, _ = Room.objects.get_or_create(pk=1, defaults={"state": initial()})
        s = room.state
        if s["phase"] == "loading":
            s.update(phase="lobby", message="Server restarted. Prepare the song pool again.")
        if s["phase"] in ("guess", "reveal") and not s["paused"]:
            s.update(paused=True, remaining=max(1, (s["deadline"] or time.time()) - time.time()), deadline=None)
        room.save(update_fields=["state"])


@contextmanager
def room_state():
    with LOCK, transaction.atomic():
        room, _ = Room.objects.get_or_create(pk=1, defaults={"state": initial()})
        before = copy.deepcopy(room.state)
        tick(room.state)
        yield room.state
        if room.state != before:
            room.save(update_fields=["state"])


def current(s):
    return s["queue"][s["index"]] if 0 <= s["index"] < len(s["queue"]) else None


def active_players(s):
    return {key: p for key, p in s["players"].items() if not p.get("spectator")}


def reveal(s):
    if s["phase"] != "guess":
        raise GameError("There is no open round to reveal.")
    song = current(s)
    accepted = {normalize(a) for a in [song["anime"], *song.get("aliases", [])] if a}
    results = {}
    for key, player in active_players(s).items():
        guess = s["guesses"].get(key, {}).get("text", "")
        correct = bool(guess) and normalize(guess) in accepted
        results[key] = {"guess": guess, "correct": correct}
        player["score"] += int(correct)
    s["history"].append({"roundId": song["roundId"], "anime": song["anime"],
                         "song": song["song"], "results": results, "void": False})
    s.update(phase="reveal", paused=False, remaining=None,
             deadline=time.time() + s["settings"]["revealSeconds"])


def finish(s):
    s.update(phase="finished", deadline=None, paused=False, remaining=None)
    result = {"gameId": s["gameId"], "players": copy.deepcopy(s["players"]),
              "history": copy.deepcopy(s["history"]), "settings": s["settings"]}
    GameResult.objects.create(result=result)


def advance(s):
    if s["index"] + 1 >= len(s["queue"]):
        finish(s)
    else:
        s["index"] += 1
        s.update(phase="buffering", guesses={}, deadline=None, paused=False,
                 remaining=None, message="Waiting for the host's audio…")


def tick(s):
    if s.get("paused") or not s.get("deadline") or time.time() < s["deadline"]:
        return
    if s["phase"] == "guess":
        reveal(s)
    elif s["phase"] == "reveal" and s["settings"]["autoAdvance"]:
        advance(s)


def join(s, profile):
    key = str(profile.pk)
    if key not in s["players"]:
        if len(s["players"]) >= 100:
            raise GameError("This room is full (100 players).", 409)
        s["players"][key] = {"score": 0, "ready": False,
            "spectator": s["phase"] in ("buffering", "guess", "reveal", "finished")}
    player = s["players"][key]
    player.update(name=profile.name, mal=profile.mal_username, avatar=profile.avatar,
                  lastSeen=time.time())


def validate_settings(data):
    if not isinstance(data, dict):
        raise GameError("Invalid room settings.")
    settings = copy.deepcopy(DEFAULTS)
    for name, low, high in [("rounds", 1, 100), ("guessSeconds", 5, 90), ("revealSeconds", 5, 60)]:
        value = data.get(name, settings[name])
        if type(value) is not int or not low <= value <= high:
            raise GameError(f"{name} must be between {low} and {high}.")
        settings[name] = value
    for name, allowed in [("types", ["OP", "ED"]), ("statuses", [1, 2, 3, 4, 6])]:
        value = data.get(name, settings[name])
        if not isinstance(value, list) or not value or any(v not in allowed for v in value):
            raise GameError(f"Select at least one valid {name} option.")
        settings[name] = list(dict.fromkeys(value))
    settings["source"] = data.get("source", "starter")
    if settings["source"] not in ["starter", "mal", "local"]:
        raise GameError("Choose a valid song source.")
    for name in ["autoAdvance", "randomStart", "allowSpoilers"]:
        value = data.get(name, settings[name])
        if type(value) is not bool:
            raise GameError(f"Invalid {name} setting.")
        settings[name] = value
    return settings


def control(s, action, data):
    phase = s["phase"]
    if action == "start":
        if phase != "ready" or not s["queue"]:
            raise GameError("Prepare a song pool first.", 409)
        if not s["players"]:
            raise GameError("At least one player must join first.", 409)
        for p in s["players"].values():
            p.update(score=0, spectator=False)
        s.update(index=-1, history=[], gameId=secrets.token_hex(12))
        advance(s)
    elif action == "begin":
        if phase != "buffering" or data.get("roundId") != current(s)["roundId"]:
            raise GameError("This audio belongs to a different round.", 409)
        s.update(phase="guess", deadline=time.time() + s["settings"]["guessSeconds"], message="")
    elif action == "reveal":
        reveal(s)
    elif action == "next":
        if phase != "reveal":
            raise GameError("Reveal this round first.", 409)
        advance(s)
    elif action == "pause":
        if phase not in ("guess", "reveal"):
            raise GameError("No timed round is running.", 409)
        if s["paused"]:
            s.update(deadline=time.time() + s["remaining"], paused=False, remaining=None)
        else:
            s.update(remaining=max(0, s["deadline"] - time.time()), deadline=None, paused=True)
    elif action == "void":
        if phase not in ("buffering", "guess", "reveal"):
            raise GameError("No round to skip.", 409)
        if phase == "reveal":
            record = s["history"][-1]
            if not record["void"]:
                for key, result in record["results"].items():
                    if key in s["players"]:
                        s["players"][key]["score"] -= int(result["correct"])
                record["void"] = True
        else:
            song = current(s)
            s["history"].append({"roundId": song["roundId"], "anime": song["anime"],
                                 "song": song["song"], "results": {}, "void": True})
        advance(s)
    elif action == "correct":
        if phase != "reveal":
            raise GameError("Answers can be reviewed during the reveal.", 409)
        key = str(data.get("playerId", ""))
        results = s["history"][-1]["results"]
        if key not in results or key not in s["players"]:
            raise GameError("Player not found.", 404)
        result = results[key]
        result["correct"] = not result["correct"]
        result["reviewed"] = True
        s["players"][key]["score"] += 1 if result["correct"] else -1
    elif action == "reset":
        players, settings = s["players"], s["settings"]
        s.clear()
        s.update(initial(), players=players, settings=settings)
        for p in players.values():
            p.update(score=0, ready=False, spectator=False)
    elif action == "remove":
        if phase not in ("lobby", "ready", "finished"):
            raise GameError("Players can be removed between games.", 409)
        s["players"].pop(str(data.get("playerId", "")), None)
    else:
        raise GameError("Unknown host command.")


def guess(s, profile_id, text, round_id):
    key = str(profile_id)
    if s["phase"] != "guess" or s["paused"]:
        raise GameError("Answers are closed or the round is paused.", 409)
    if not current(s) or current(s)["roundId"] != round_id:
        raise GameError("That round has ended. Please answer the current song.", 409)
    if key not in active_players(s):
        raise GameError("You will be able to play in the next game.", 403)
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
        raise GameError("Enter an anime title (up to 200 characters).")
    # Players may revise until the deadline. Only the last received answer is scored.
    s["guesses"][key] = {"text": text.strip(), "at": time.time()}


def public_state(s, profile_id=None, host=False):
    now, key = time.time(), str(profile_id)
    song = current(s)
    revealed = s["phase"] in ("reveal", "finished")
    latest = s["history"][-1] if s["history"] and revealed else {}
    players = []
    ranked = sorted(s["players"].items(), key=lambda item: (-item[1]["score"], item[1]["name"].casefold()))
    rank, prior = 0, None
    for i, (pid, p) in enumerate(ranked):
        if p["score"] != prior:
            rank, prior = i + 1, p["score"]
        players.append({"id": pid, "name": p["name"], "mal": p["mal"], "avatar": p["avatar"],
            "score": p["score"], "rank": rank, "ready": p["ready"], "spectator": p.get("spectator", False),
            "online": now - p["lastSeen"] < 35, "submitted": pid in s["guesses"],
            "result": latest.get("results", {}).get(pid)})
    out = {"phase": s["phase"], "settings": s["settings"], "players": players,
        "round": s["index"] + 1, "total": len(s["queue"]),
        "roundId": song["roundId"] if song else None,
        "deadline": s["deadline"], "serverTime": now, "paused": s["paused"],
        "remaining": s["remaining"], "message": s["message"], "loadProgress": s["loadProgress"],
        "titles": s["titles"], "myGuess": s["guesses"].get(key, {}).get("text", ""),
        "answer": {k: song.get(k) for k in ("anime", "song", "artist", "type", "year")} if song and revealed else None,
        "history": [{"anime": h["anime"], "song": h["song"], "void": h["void"]} for h in s["history"]],
    }
    if host and song:
        out["mediaUrl"] = f"/api/media/{song['roundId']}"
        out["startAt"] = song.get("startAt", 0)
    return out
