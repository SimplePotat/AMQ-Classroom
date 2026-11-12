import csv
import io
import json
import mimetypes
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError
from django.middleware.csrf import rotate_token
from .security import is_host, grant_host
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from . import engine, json_data, library, prefetch
from .models import Profile, GameResult

ATTEMPTS = defaultdict(deque)
AUTH_LOCK = threading.Lock()


def api(fn):
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        try:
            result = fn(request, *args, **kwargs)
            if isinstance(result, dict):
                result = JsonResponse(result)
            result["Cache-Control"] = "no-store"
            return result
        except engine.GameError as exc:
            result = JsonResponse({"error": str(exc)}, status=exc.status)
            result["Cache-Control"] = "no-store"
            return result
        except (ValueError, TypeError, json.JSONDecodeError):
            result = JsonResponse({"error": "Invalid request data."}, status=400)
            result["Cache-Control"] = "no-store"
            return result
    return wrapped


def body(request):
    if request.method != "POST":
        raise engine.GameError("Use POST for this action.", 405)
    if request.content_type != "application/json":
        raise engine.GameError("Send a JSON request.", 415)
    data = json_data.loads(request.body or b"{}")
    if not isinstance(data, dict):
        raise engine.GameError("Expected a JSON object.")
    return data


def require_host(request):
    if not is_host(request):
        raise engine.GameError("Sign in with the host key first.", 403)


def profile_for(request):
    return Profile.objects.filter(pk=request.session.get("profile_id")).first()


def require_profile(request):
    profile = profile_for(request)
    if not profile:
        raise engine.GameError("Sign in to your player profile first.", 401)
    return profile


def throttle(request, kind, limit=30, window=300):
    now = time.time()
    key = (kind, request.META.get("REMOTE_ADDR", "unknown"))
    with AUTH_LOCK:
        for stale in list(ATTEMPTS):
            if not ATTEMPTS[stale] or ATTEMPTS[stale][-1] < now - 300:
                del ATTEMPTS[stale]
        queue = ATTEMPTS[key]
        while queue and queue[0] < now - window:
            queue.popleft()
        if len(queue) >= limit:
            raise engine.GameError("Too many attempts. Please wait before trying again.", 429)
        queue.append(now)


def validate_profile(data):
    name = data.get("name", "")
    mal = data.get("mal", "")
    avatar = data.get("avatar", 0)
    if not isinstance(name, str) or len(name) > 100 or not 2 <= len(name.strip()) <= 24 or any(ord(c) < 32 for c in name):
        raise engine.GameError("Choose a player name with 2–24 characters.")
    name = name.strip()
    if not engine.normalize(name):
        raise engine.GameError("Use letters or numbers in your player name.")
    if not isinstance(mal, str) or (mal and not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", mal)):
        raise engine.GameError("Enter a MAL username, without its profile URL.")
    if type(avatar) is not int or not 0 <= avatar <= 7:
        raise engine.GameError("Choose a valid avatar.")
    return name, mal, avatar


def join_url(request):
    import os
    return os.environ.get("CLASSROOM_JOIN_URL", request.build_absolute_uri("/")).rstrip("/")


@ensure_csrf_cookie
def index(request):
    response = render(request, "classroom/index.html")
    response["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def asset(request, filename):
    if filename not in {"app.js", "style.css", "mark.svg"}:
        return HttpResponse(status=404)
    return FileResponse((Path(__file__).parent / "static" / filename).open("rb"), content_type=mimetypes.guess_type(filename)[0])


@api
def state(request):
    if request.method != "GET":
        raise engine.GameError("Use GET.", 405)
    profile = profile_for(request)
    host = is_host(request)
    if not profile and not host:
        return {"host": False, "me": None, "joinUrl": join_url(request), "sourceUrl": settings.SOURCE_URL}
    with engine.room_state() as s:
        if profile and str(profile.pk) in s["players"]:
            player = s["players"][str(profile.pk)]
            if time.time() - player["lastSeen"] > 10:
                player["lastSeen"] = time.time()
        out = engine.public_state(s, profile.pk if profile else None, host)
    out.update(host=host, me={"id": str(profile.pk), "name": profile.name, "mal": profile.mal_username,
                            "avatar": profile.avatar} if profile else None,
               joinUrl=join_url(request), sourceUrl=settings.SOURCE_URL)
    return out


@api
def auth(request):
    data = body(request)
    throttle(request, "auth")
    if data.get("action") == "host":
        key = data.get("key", "")
        if not isinstance(key, str) or len(key) > 256 or not secrets.compare_digest(key.encode(), settings.HOST_KEY.encode()):
            raise engine.GameError("That host key is not correct. Use the key printed by the server.", 403)
        request.session.cycle_key()
        request.session.pop("profile_id", None)
        request.session.pop("host", None)
        grant_host(request)
        rotate_token(request)
    else:
        if data.get("action") not in ("register", "login"):
            raise engine.GameError("Choose register or sign in.")
        name, mal, avatar = validate_profile(data)
        password = data.get("password", "")
        if not isinstance(password, str) or not 4 <= len(password) <= 64:
            raise engine.GameError("Use a passcode with 4–64 characters.")
        name_key = engine.normalize(name)
        if data["action"] == "register":
            with engine.room_state() as s:
                if len(s["players"]) >= 100:
                    raise engine.GameError("This room is full (100 players).", 409)
            # Hash outside the room lock, then atomically register and join. A
            # concurrent final join must not leave an account behind on failure.
            password_hash = make_password(password)
            try:
                with engine.room_state() as s:
                    if len(s["players"]) >= 100:
                        raise engine.GameError("This room is full (100 players).", 409)
                    profile = Profile.objects.create(name=name, name_key=name_key, mal_username=mal,
                        avatar=avatar, password=password_hash)
                    engine.join(s, profile)
            except IntegrityError:
                raise engine.GameError("That player name is already registered. Sign in or choose another.", 409)
        else:
            profile = Profile.objects.filter(name_key=name_key).first()
            if not profile:
                make_password(password)
                raise engine.GameError("Player name or passcode is incorrect.", 403)
            if not check_password(password, profile.password):
                raise engine.GameError("Player name or passcode is incorrect.", 403)
            with engine.room_state() as s:
                engine.join(s, profile)
        request.session.cycle_key()
        request.session.pop("host", None)
        request.session.pop("host_grant", None)
        request.session["profile_id"] = profile.pk
        rotate_token(request)
        prefetch.schedule(profile.pk)
    return {"ok": True}


@api
def player(request):
    data = body(request)
    profile = require_profile(request)
    throttle(request, "player", limit=120, window=60)
    action = data.get("action")
    if action in ("skip", "void", "next", "reveal-ended"):
        raise engine.GameError("Only the host can skip or advance songs.", 403)
    if action == "profile":
        name, mal, avatar = validate_profile(data)
    with engine.room_state() as s:
        if action == "profile":
            # A failed rejoin must roll back the saved profile as well as the
            # roster, for example when a removed player's room has filled up.
            profile.name, profile.name_key = name, engine.normalize(name)
            profile.mal_username, profile.avatar = mal, avatar
            try:
                profile.save()
            except IntegrityError:
                raise engine.GameError("That player name is already in use.", 409)
        if action in ("join", "profile"):
            engine.join(s, profile)
        elif action == "guess":
            engine.guess(s, profile.pk, data.get("text"), data.get("roundId"))
        elif action == "ready":
            if s["phase"] not in ("lobby", "loading", "ready"):
                raise engine.GameError("The game has already started.", 409)
            engine.join(s, profile)
            p = s["players"][str(profile.pk)]
            p["ready"] = not p["ready"]
        elif action == "leave":
            if s["phase"] not in ("lobby", "loading", "ready", "finished"):
                raise engine.GameError("Wait until the game ends to leave the roster.", 409)
            s["players"].pop(str(profile.pk), None)
        elif action != "profile":
            raise engine.GameError("Unknown player action.")
    if action in ("join", "profile", "ready"):
        prefetch.schedule(profile.pk)
    return {"ok": True}


@api
def host(request):
    require_host(request)
    data = body(request)
    job = None
    with engine.room_state() as s:
        if data.get("action") == "prepare":
            if s["phase"] not in ("lobby", "ready", "finished"):
                raise engine.GameError("Finish or reset the current game first.", 409)
            options = engine.validate_settings(data.get("settings", {}))
            load_id = secrets.token_hex(8)
            usernames = list(dict.fromkeys(p["mal"] for p in s["players"].values() if p["mal"]))
            s.update(settings=options, phase="loading", message="Building your song pool…", loadProgress=0,
                     loadId=load_id, queue=[], index=-1, deadline=None, paused=False, history=[])
            job = (load_id, options, usernames)
        else:
            engine.control(s, data.get("action"), data)
    if job:
        library.start_prepare(*job)
    return {"ok": True}


@api
def logout(request):
    data = body(request)
    if data.get("role") == "host":
        request.session.pop("host", None)
        request.session.pop("host_grant", None)
    else:
        request.session.pop("profile_id", None)
    request.session.cycle_key()
    rotate_token(request)
    return {"ok": True}


@api
def media(request, round_id):
    require_host(request)
    if request.method not in ("GET", "HEAD"):
        response = JsonResponse({"error": "Use GET or HEAD."}, status=405)
        response["Allow"] = "GET, HEAD"
        return response
    with engine.room_state() as s:
        song = engine.current(s)
        if not song or song["roundId"] != round_id:
            raise engine.GameError("This round is no longer active.", 404)
        name = song["media"]
    if name.startswith("https://"):
        response = HttpResponse(status=302)
        response["Location"] = name
        return response
    path = library.media_path(name)
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("Range")
    if range_header:
        match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_header)
        if not match or not any(match.groups()):
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        left, right = match.groups()
        def bounded_index(value):
            # Valid HTTP decimal ranges can exceed Python's integer-string
            # limit. Values above the file size have the same range behavior.
            value = value.lstrip("0") or "0"
            return size if len(value) > len(str(size)) else min(int(value), size)
        if left:
            start, end = bounded_index(left), min(bounded_index(right), size - 1) if right else size - 1
        else:
            start = max(0, size - bounded_index(right))
        if start > end or start >= size:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        status = 206

    def chunks():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    response = StreamingHttpResponse(chunks() if request.method != "HEAD" else (), status=status,
                                     content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = end - start + 1
    if status == 206:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response


@api
def qr(request):
    svg = qrcode.make(join_url(request), image_factory=qrcode.image.svg.SvgPathImage, border=2)
    output = io.BytesIO()
    svg.save(output)
    return HttpResponse(output.getvalue(), content_type="image/svg+xml")


@api
def results(request):
    require_host(request)
    with engine.room_state() as s:
        if s["phase"] not in ("reveal", "finished"):
            raise engine.GameError("Results are available after a round is revealed.", 409)
        if s["phase"] == "finished":
            completed = GameResult.objects.filter(result__gameId=s["gameId"]).order_by("-pk").first()
            if completed:
                s = completed.result
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Player", "MAL", "Score", "Round", "Anime", "Song", "Guess", "Correct", "Voided"])
        def safe(value):
            # Old JSONField records may contain lone surrogates accepted by
            # earlier releases. Repair only the export, preserving saved data.
            text = re.sub(r"[\ud800-\udfff]", "\ufffd", str(value))
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        for key, p in s["players"].items():
            for i, record in enumerate(s["history"]):
                result = record["results"].get(key, {})
                writer.writerow([safe(p["name"]), safe(p["mal"]), p["score"], i + 1,
                    safe(record["anime"]), safe(record["song"]), safe(result.get("guess", "")),
                    result.get("correct", False), record["void"]])
    response = HttpResponse(out.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="anime-quiz-results.csv"'
    return response
