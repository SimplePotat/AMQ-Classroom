#!/usr/bin/env python3
"""Isolated HTTP regression: 100 simulated LAN clients and a real server restart.

Run with the classroom virtualenv. Uses temporary synthetic media and SQLite;
no existing profiles, listening servers, or external providers are touched.
"""
import csv
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie


ROOT = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self, port, ip="127.0.0.1"):
        self.port, self.ip, self.cookies = port, ip, {}

    def request(self, path, body=None, *, status=200, headers=None, method=None):
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()), **(headers or {})}
        if body is not None:
            headers.update({"Content-Type": "application/json", "X-CSRFToken": self.cookies.get("csrftoken", ""),
                            "Origin": f"http://127.0.0.1:{self.port}"})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60, source_address=(self.ip, 0))
        try:
            connection.request(method or ("POST" if body is not None else "GET"), path,
                               json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            data = response.read()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    self.cookies.update({k: v.value for k, v in SimpleCookie(value).items()})
            assert response.status == status, (path, response.status, data[:200])
            return data, dict(response.getheaders())
        finally:
            connection.close()

    def api(self, path="/api/state", body=None, **kwargs):
        return json.loads(self.request(path, body, **kwargs)[0])


def main():
    with tempfile.TemporaryDirectory(prefix="classroom-stress-") as directory:
        data = Path(directory)
        (data / "media").mkdir()
        with wave.open(str(data / "media" / "synthetic.wav"), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 8000)
        (data / "catalog.json").write_text(json.dumps([
            {"anime": f"Synthetic show {i}", "song": f"Synthetic song {i}", "type": "OP",
             "media": "synthetic.wav", "aliases": ["Synthetic correct answer"]} for i in range(2)
        ]))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        key = secrets.token_urlsafe(24)
        env = {**os.environ, "CLASSROOM_DATA_DIR": directory, "CLASSROOM_PREWARM": "0", "CLASSROOM_HOST_KEY": key}
        server = None

        def stop():
            nonlocal server
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                server = None

        def start():
            nonlocal server
            server = subprocess.Popen([sys.executable, "run_classroom.py", "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=log)
            probe = Client(port)
            for _ in range(150):
                assert server.poll() is None, "Server exited during startup"
                try:
                    probe.request("/")
                    return
                except (OSError, http.client.HTTPException):
                    time.sleep(0.1)
            raise AssertionError("Server did not start")

        with (data / "server.log").open("w+") as log:
            try:
                start()
                host = Client(port)
                host.request("/host")
                host.api("/api/auth", {"action": "host", "key": key})
                players = [Client(port, f"127.0.0.{i + 2}") for i in range(100)]

                def register(item):
                    i, client = item
                    client.request("/")
                    client.api("/api/auth", {"action": "register", "name": f"Load Player {i:03}",
                                            "password": "synthetic-test-passcode", "mal": ""})
                    client.api("/api/player", {"action": "ready"})

                with ThreadPoolExecutor(max_workers=32) as workers:
                    list(workers.map(register, enumerate(players)))
                    state = host.api()
                    assert len(state["players"]) == 100
                    assert all(p["ready"] for p in state["players"])
                    overflow = Client(port, "127.0.0.102")
                    overflow.request("/")
                    overflow.api("/api/auth", {"action": "register", "name": "Overflow player", "password": "test-pass"}, status=409)
                    with sqlite3.connect(data / "classroom.sqlite3") as db:
                        assert db.execute("select count(*) from classroom_profile").fetchone()[0] == 100
                    host.api("/api/host", {"action": "prepare", "settings": {
                        "source": "local", "rounds": 2, "guessSeconds": 90, "randomStart": False}})
                    for _ in range(150):
                        state = host.api()
                        if state["phase"] == "ready":
                            break
                        time.sleep(0.1)
                    assert state["phase"] == "ready", state.get("message")
                    host.api("/api/host", {"action": "start"})
                    state = host.api()
                    round_id = state["roundId"]
                    host.api("/api/host", {"action": "begin", "roundId": round_id})
                    media, headers = host.request(state["mediaUrl"], headers={"Range": "bytes=0-15"}, status=206)
                    assert len(media) == 16 and headers["Content-Range"].startswith("bytes 0-15/")
                    players[0].request(state["mediaUrl"], status=403)

                    def guess(item):
                        i, client = item
                        client.api("/api/player", {"action": "guess", "roundId": round_id,
                                                  "text": "Synthetic correct answer" if i % 2 == 0 else "Wrong"})
                        s = client.api()
                        assert s["answer"] is None and "mediaUrl" not in s
                        assert len(s["players"]) == 100

                    list(workers.map(guess, enumerate(players)))
                    host.api("/api/host", {"action": "reveal", "roundId": round_id})
                    revealed = list(workers.map(lambda client: client.api(), players))
                    assert all(sum(p["score"] for p in s["players"]) == 50 for s in revealed)
                    host.api("/api/host", {"action": "next", "roundId": round_id})
                    state = host.api()
                    next_id = state["roundId"]
                    host.api("/api/host", {"action": "begin", "roundId": next_id})
                    players[0].api("/api/player", {"action": "guess", "roundId": round_id, "text": "Old"}, status=409)
                    for action in ("void", "next", "reveal-ended"):
                        players[0].api("/api/host", {"action": action, "roundId": next_id}, status=403)
                        players[0].api("/api/player", {"action": action, "roundId": next_id}, status=403)
                    list(workers.map(lambda client: client.api("/api/player", {
                        "action": "guess", "roundId": next_id, "text": "Synthetic correct answer"}), players))
                    stop()
                    start()
                    recovered = list(workers.map(lambda client: client.api(), players))
                    assert all(s["phase"] == "guess" and s["paused"] and s["roundId"] == next_id
                               and s["myGuess"] == "Synthetic correct answer" for s in recovered)
                    host.api("/api/host", {"action": "pause", "roundId": next_id})
                    host.api("/api/host", {"action": "reveal", "roundId": next_id})
                    assert sum(p["score"] for p in host.api()["players"]) == 150
                    host.api("/api/host", {"action": "void", "roundId": next_id})
                    finished = list(workers.map(lambda client: client.api(), players))
                    assert all(s["phase"] == "finished" and sum(p["score"] for p in s["players"]) == 50 for s in finished)
                    with sqlite3.connect(data / "classroom.sqlite3") as db:
                        assert db.execute("select count(*) from classroom_gameresult").fetchone()[0] == 1
                    csv_data = host.request("/api/results.csv")[0].decode()
                    assert len(list(csv.DictReader(io.StringIO(csv_data)))) == 200
                log.flush()
                log.seek(0)
                assert "Traceback" not in log.read(), "Unexpected server traceback"
                print("PASS: 100 simulated LAN sessions; parallel registration, ready, guesses and polling; roster limit; "
                      "answer/media privacy; stale guesses; host-only controls; byte ranges; exact scoring; "
                      "real process restart with saved sessions/answers; void; single result; 200 CSV rows.")
            finally:
                stop()


if __name__ == "__main__":
    main()
