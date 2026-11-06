"""Best-effort, bounded lobby warming. Foreground preparation always wins."""
import logging
import threading
import time
from collections import OrderedDict

from django.conf import settings
from django.db import close_old_connections

from . import library, metadata
from .engine import GameError
from .models import Room

LOG = logging.getLogger(__name__)
MAX_PENDING = 100
MAX_TITLES = 40
REQUEST_BUDGET = 8
JOB_SECONDS = 60
COOLDOWN_SECONDS = 300
LOCK = threading.Lock()
PENDING = OrderedDict()
RECENT = OrderedDict()
WORKER = None


def schedule(profile_id):
    """Called after a successful join/profile save, outside the room transaction."""
    global WORKER
    if not settings.CLASSROOM_PREWARM_ENABLED:
        return
    key = str(profile_id)
    # A read-only snapshot avoids ticking the quiz clock from background work.
    s = Room.objects.filter(pk=1).values_list("state", flat=True).first()
    player = s["players"].get(key) if s else None
    if not s or s["phase"] != "lobby" or s["settings"]["source"] == "local" or not player or not player["mal"]:
        return
    username = player["mal"]
    with LOCK:
        if key in PENDING:
            PENDING[key] = username
        else:
            now = time.monotonic()
            # Bound repeated profile edits as well as multiple players sharing a list.
            keys = [("profile", key), ("mal", username.casefold())]
            if len(PENDING) >= MAX_PENDING or any(now - RECENT.get(k, -float("inf")) < COOLDOWN_SECONDS for k in keys):
                return
            for k in keys:
                RECENT[k] = now
                RECENT.move_to_end(k)
            while len(RECENT) > 2 * MAX_PENDING:
                RECENT.popitem(last=False)
            PENDING[key] = username
        if WORKER is None:
            WORKER = threading.Thread(target=_run, daemon=True, name="lobby-cache")
            try:
                WORKER.start()
            except RuntimeError:
                WORKER = None
                LOG.exception("Could not start lobby cache worker")


def _warm(profile_id, username):
    deadline = time.monotonic() + JOB_SECONDS

    def check():
        if not settings.CLASSROOM_PREWARM_ENABLED or time.monotonic() >= deadline:
            raise InterruptedError()
        s = Room.objects.filter(pk=1).values_list("state", flat=True).first()
        player = s["players"].get(profile_id) if s else None
        if (not s or s["phase"] != "lobby" or s["settings"]["source"] == "local" or not player
                or player["mal"].casefold() != username.casefold()):
            raise InterruptedError()
        return s["settings"]["statuses"][:]

    statuses = check()
    with metadata.work_context(check=check, background=True, request_budget=REQUEST_BUDGET):
        entries = library.mal_list(username)
        candidates = list(dict(library.list_candidates(entries, statuses)))
        library.RANDOM.shuffle(candidates)
        for offset in range(0, min(len(candidates), MAX_TITLES), library.ANIME_BATCH_SIZE):
            metadata.checkpoint()
            library.anime_data_batch(candidates[offset:min(offset + library.ANIME_BATCH_SIZE, MAX_TITLES)])


def _run():
    global WORKER
    try:
        while True:
            with LOCK:
                if not PENDING:
                    WORKER = None
                    return
                profile_id, username = PENDING.popitem(last=False)
            close_old_connections()
            try:
                _warm(profile_id, username)
            except InterruptedError:
                pass
            except GameError as exc:
                LOG.info("Lobby cache warming stopped: %s", exc)
            except Exception:
                LOG.exception("Lobby cache warming failed")
            finally:
                close_old_connections()
    finally:
        metadata.close_session()
        close_old_connections()
        with LOCK:
            if WORKER is threading.current_thread():
                WORKER = None
