"""Bounded metadata transport shared by preparation and lobby prefetching."""
import json
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from .engine import GameError

JSON_HOSTS = frozenset({"api.animethemes.moe", "myanimelist.net", "openings.moe"})
MEDIA_PROBE_HOSTS = frozenset({"v.animethemes.moe", "openings.moe"})
MAX_JSON_BYTES = 4 * 1024 * 1024
LOCAL = threading.local()
RATE_LOCK = threading.Lock()
NEXT_REQUEST = {}
COOLDOWN = {}


class RateLimited(GameError):
    def __init__(self):
        super().__init__("The music provider is rate limiting requests. Wait a minute and try again.")


class ResponseTooLarge(GameError):
    def __init__(self):
        super().__init__("The metadata provider response is too large.")


@dataclass
class Work:
    check: object = None
    background: bool = False
    remaining: int | None = None


@contextmanager
def work_context(check=None, background=False, request_budget=None):
    previous = getattr(LOCAL, "work", None)
    LOCAL.work = Work(check, background, request_budget)
    try:
        yield
    finally:
        LOCAL.work = previous


def checkpoint():
    work = getattr(LOCAL, "work", None)
    if work and work.check:
        work.check()


def http_session():
    # Requests sessions have mutable headers/cookies. Each worker owns its pool.
    if not getattr(LOCAL, "session", None):
        LOCAL.session = requests.Session()
        LOCAL.session.headers["User-Agent"] = "AnimeQuizClassroom/1.0 (+https://github.com/SimplePotat/AMQ-Classroom)"
    return LOCAL.session


def close_session():
    session = getattr(LOCAL, "session", None)
    if session is not None:
        session.close()
        del LOCAL.session


def media_playable(url):
    """A tiny range probe tells preparation the video host will serve this file."""
    try:
        target = urlparse(url)
    except ValueError:
        return False
    if target.scheme != "https" or not target.hostname or target.hostname not in MEDIA_PROBE_HOSTS:
        return True
    if target.username or target.password or target.port not in (None, 443):
        return False
    checkpoint()
    try:
        with http_session().get(url, headers={"Range": "bytes=0-0"}, timeout=(3, 5),
                                stream=True, allow_redirects=False) as response:
            return response.status_code in (200, 206, 416)
    except requests.RequestException:
        return False


def wait_for_provider(host):
    work = getattr(LOCAL, "work", None)
    while True:
        checkpoint()
        if work and work.remaining is not None and work.remaining <= 0:
            raise InterruptedError("Metadata request budget exhausted")
        with RATE_LOCK:
            now = time.monotonic()
            if COOLDOWN.get(host, 0) > now:
                raise RateLimited()
            delay = NEXT_REQUEST.get(host, 0) - now
            if delay <= 0:
                # At most 60/minute per provider, with a slower background pace.
                NEXT_REQUEST[host] = now + (2 if work and work.background else 1)
                if work and work.remaining is not None:
                    work.remaining -= 1
                return
        # Do not reserve future slots: preparation can take the next free slot.
        time.sleep(min(delay, 0.1))


def retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError):
            seconds = 60
    return max(1, seconds) if math.isfinite(seconds) else 60


def get_json(url, params=None):
    # Never follow redirects or provider pagination URLs to another service.
    try:
        target = urlparse(url)
        allowed = (target.scheme == "https" and target.hostname in JSON_HOSTS
                   and target.port in (None, 443) and not target.username and not target.password)
    except ValueError:
        allowed = False
    if not allowed:
        raise GameError("The metadata provider address is not allowed.")
    wait_for_provider(target.hostname)
    try:
        with http_session().get(url, params=params, timeout=(5, 20), stream=True,
                                allow_redirects=False) as response:
            if response.status_code == 429 or response.headers.get("X-RateLimit-Remaining") == "0":
                with RATE_LOCK:
                    COOLDOWN[target.hostname] = time.monotonic() + retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429:
                raise RateLimited()
            if 300 <= response.status_code < 400:
                raise GameError("The metadata provider returned an unexpected redirect.")
            response.raise_for_status()
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > MAX_JSON_BYTES:
                raise ResponseTooLarge()
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(body) + len(chunk) > MAX_JSON_BYTES:
                    raise ResponseTooLarge()
                body.extend(chunk)
            return json.loads(body)
    except (requests.RequestException, ValueError) as exc:
        raise GameError("The music provider could not be reached. Check the host's internet connection or use a local catalog.") from exc
