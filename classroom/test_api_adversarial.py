"""Fresh generated API inputs and byte-range boundaries, independent of UI tests."""
import copy
import csv
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from . import engine, json_data, views
from .models import GameResult, Profile, Room
from .tests import song


class SharedJsonBoundaryTests(SimpleTestCase):
    def test_container_depth_limit_is_exact_for_empty_and_populated_values(self):
        for limit in (1, 3, 32):
            accepted = ["[" * limit + "0" + "]" * limit,
                        "[" * limit + "]" * limit,
                        '{"x":' * limit + '0' + '}' * limit,
                        '{"x":' * (limit - 1) + '{}' + '}' * (limit - 1)]
            rejected = ["[" * (limit + 1) + "0" + "]" * (limit + 1),
                        "[" * (limit + 1) + "]" * (limit + 1),
                        '{"x":' * (limit + 1) + '0' + '}' * (limit + 1),
                        '{"x":' * limit + '{}' + '}' * limit]
            for payload in accepted:
                with self.subTest(limit=limit, accepted=payload[:40]):
                    self.assertEqual(json_data.loads(payload, max_depth=limit), json.loads(payload))
            for payload in rejected:
                with self.subTest(limit=limit, rejected=payload[:40]), self.assertRaises(ValueError):
                    json_data.loads(payload, max_depth=limit)

    def test_valid_unicode_pairs_finite_exponents_and_scalars_remain_supported(self):
        self.assertEqual(json_data.loads(r'{"song":"\ud83c\udfb6", "進撃":"の巨人"}'),
                         {"song": "🎶", "進撃": "の巨人"})
        for payload in ("1e308", "-1e308", "1e-308", "1e-999", "null", "true", "123", '"text"'):
            with self.subTest(payload=payload):
                self.assertEqual(json_data.loads(payload, max_depth=0), json.loads(payload))
        for payload in ("[]", "{}"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                json_data.loads(payload, max_depth=0)

    def test_nonfinite_and_surrogate_values_are_rejected_at_root_and_nested(self):
        for payload in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999", r'"\ud800"',
                        r'{"\udfff":0}', r'{"valid":["\ud800"]}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                json_data.loads(payload)


@override_settings(CLASSROOM_PREWARM_ENABLED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdversarialApiTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()
        self.host, self.player = Client(), Client()
        self.post(self.host, "/api/auth", {"action": "host", "key": settings.HOST_KEY})
        self.post(self.player, "/api/auth", {"action": "register", "name": "Fuzz player", "password": "test-pass"})
        self.profile_id = str(Profile.objects.get(name="Fuzz player").pk)

    def post(self, client, path, data, status=200):
        response = client.post(path, json.dumps(data), content_type="application/json")
        self.assertEqual(response.status_code, status, response.content[:400])
        return response

    def snapshot(self):
        return (Room.objects.get(pk=1).state, list(Profile.objects.values()), list(GameResult.objects.values()))

    def start(self):
        with engine.room_state() as state:
            state.update(phase="ready", queue=[song("adversarial-round")])
        self.post(self.host, "/api/host", {"action": "start"})
        self.post(self.host, "/api/host", {"action": "begin", "roundId": "adversarial-round"})

    def test_deep_json_is_a_controlled_400_on_every_json_endpoint(self):
        payload = b'{"extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
        self.assertLess(len(payload), settings.DATA_UPLOAD_MAX_MEMORY_SIZE)
        before = self.snapshot()
        for client, path in ((Client(), "/api/auth"), (self.host, "/api/host"),
                             (self.player, "/api/player"), (self.player, "/api/logout")):
            with self.subTest(path=path):
                response = client.post(path, payload, content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response["Cache-Control"], "no-store")
                self.assertEqual(self.snapshot(), before)

    def test_non_json_numbers_are_rejected_even_in_unused_fields(self):
        before = self.snapshot()
        for constant in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(constant=constant):
                response = self.player.post("/api/player", '{"action":"ready","extra":' + constant + '}',
                                            content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_escaped_surrogate_guess_is_rejected_before_it_can_break_csv(self):
        self.start()
        before = self.snapshot()
        for invalid in ("\ud800", "\udfff", "Guess \ud800", "\ud800abc\udfff"):
            with self.subTest(value=repr(invalid)):
                self.post(self.player, "/api/player", {"action": "guess", "roundId": "adversarial-round",
                                                        "text": invalid}, status=400)
                self.assertEqual(self.snapshot(), before)
        self.post(self.player, "/api/player", {"action": "guess", "roundId": "adversarial-round", "text": "進撃の巨人 🎶"})
        self.post(self.host, "/api/host", {"action": "reveal"})
        response = self.host.get("/api/results.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("進撃の巨人 🎶", response.content.decode())

    def test_invalid_unicode_anywhere_in_json_is_rejected_without_changes(self):
        before = self.snapshot()
        for extra in ({"\ud800": "value"}, {"extra": {"key": ["\udfff"]}}, {"extra": "bad\udc00"}):
            with self.subTest(extra=repr(extra)):
                self.post(self.player, "/api/player", {"action": "ready", **extra}, status=400)
                self.assertEqual(self.snapshot(), before)

    def test_saved_legacy_surrogate_guess_exports_without_mutating_saved_result(self):
        self.start()
        self.post(self.player, "/api/player", {"action": "guess", "roundId": "adversarial-round", "text": "Attack on Titan"})
        self.post(self.host, "/api/host", {"action": "reveal"})
        self.post(self.host, "/api/host", {"action": "next"})
        # Prior releases accepted escaped lone surrogates and JSONField could
        # persist them. They must not make a valid CSV export request fail.
        result = GameResult.objects.get()
        result.result["history"][0]["results"][self.profile_id]["guess"] = "=legacy \ud800 and \udfff"
        result.save(update_fields=["result"])
        before = self.snapshot()
        response = self.host.get("/api/results.csv")
        self.assertEqual(response.status_code, 200)
        row = next(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(row["Guess"], "'=legacy � and �")
        self.assertEqual(self.snapshot(), before)

    def test_truncated_and_non_object_json_are_rejected(self):
        before = self.snapshot()
        payloads = [b'{', b'{"action":', b'{"action":"ready",}', b'[]', b'null', b'true', b'123',
                    b'"ready"', b'{"action":"ready"} trailing', b'\xff', b'{"action":"ready","x":01}']
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.player.post("/api/player", payload, content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_generated_wrong_field_types_cannot_mutate_settings_or_profiles(self):
        before = self.snapshot()
        values = (None, True, False, -1, 0, 1.5, [], {}, ["nested"])
        with patch("classroom.library.start_prepare") as loader:
            for field in ("rounds", "guessSeconds", "revealSeconds", "source", "types", "statuses",
                          "autoAdvance", "fullReveal", "randomStart", "preferOpenings", "allowOpeningsFallback"):
                for value in values:
                    # True/False are valid for boolean settings; 0 is valid for no numeric settings here.
                    if field in ("autoAdvance", "fullReveal", "randomStart", "preferOpenings", "allowOpeningsFallback") and type(value) is bool:
                        continue
                    with self.subTest(field=field, value=value):
                        self.post(self.host, "/api/host", {"action": "prepare", "settings": {field: value}}, status=400)
                        self.assertEqual(self.snapshot(), before)
            loader.assert_not_called()
        for field in ("name", "mal", "avatar"):
            for value in values:
                if field == "avatar" and type(value) is int and value == 0:
                    continue
                with self.subTest(profile_field=field, value=value):
                    self.post(self.player, "/api/player", {"action": "profile", "name": "Fuzz player", **{field: value}}, status=400)
                    self.assertEqual(self.snapshot(), before)

    def test_mutation_endpoints_reject_wrong_methods_without_changes(self):
        before = self.snapshot()
        for path, client in (("/api/auth", self.player), ("/api/player", self.player),
                             ("/api/host", self.host), ("/api/logout", self.player)):
            for method in ("get", "put", "patch", "delete", "head", "options"):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path, content_type="application/json")
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(self.snapshot(), before)


@override_settings(CLASSROOM_PREWARM_ENABLED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MediaProtocolMatrixTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()
        self.host = Client()
        response = self.host.post("/api/auth", json.dumps({"action": "host", "key": settings.HOST_KEY}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.directory = tempfile.TemporaryDirectory(prefix="amq-range-matrix-")
        self.addCleanup(self.directory.cleanup)
        override = override_settings(DATA_DIR=Path(self.directory.name))
        override.enable()
        self.addCleanup(override.disable)
        base = Path(self.directory.name) / "media"
        base.mkdir()
        (base / "test.wav").write_bytes(b"0123456789")
        with engine.room_state() as state:
            state.update(phase="buffering", index=0, queue=[song("range-round")])
        self.url = "/api/media/range-round"

    def test_valid_range_matrix_matches_exact_slices_for_get_and_head(self):
        for range_header, expected in ((None, b"0123456789"), ("bytes=0-0", b"0"), ("bytes=0-9", b"0123456789"),
                                       ("bytes=2-5", b"2345"), ("bytes=9-", b"9"), ("bytes=-1", b"9"),
                                       ("bytes=-10", b"0123456789"), ("bytes=-20", b"0123456789"),
                                       ("bytes=0-20", b"0123456789"), ("bytes=0002-0005", b"2345")):
            for method in ("get", "head"):
                with self.subTest(range=range_header, method=method):
                    headers = {"HTTP_RANGE": range_header} if range_header else {}
                    response = getattr(self.host, method)(self.url, **headers)
                    self.assertEqual(response.status_code, 206 if range_header else 200)
                    self.assertEqual(int(response["Content-Length"]), len(expected))
                    self.assertEqual(b"".join(response.streaming_content), expected if method == "get" else b"")

    def test_unsatisfiable_and_malformed_ranges_are_416(self):
        for value in ("bytes=10-", "bytes=99-100", "bytes=5-2", "bytes=-0", "bytes=-", "bytes=", "bytes=a-b",
                      "items=0-1", "bytes=0-1,4-5", "bytes=+1-2", "bytes=0--1"):
            with self.subTest(range=value):
                response = self.host.get(self.url, HTTP_RANGE=value)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response["Content-Range"], "bytes */10")

    def test_large_decimal_range_values_are_bounded_without_integer_parser_failures(self):
        cases = [("bytes=0-" + "9" * 5000, 206, b"0123456789"),
                 ("bytes=-" + "9" * 5000, 206, b"0123456789"),
                 ("bytes=" + "9" * 5000 + "-", 416, b""),
                 ("bytes=" + "0" * 5000 + "2-5", 206, b"2345")]
        for value, status, expected in cases:
            with self.subTest(prefix=value[:20]):
                response = self.host.get(self.url, HTTP_RANGE=value)
                self.assertEqual(response.status_code, status)
                if status == 206:
                    self.assertEqual(b"".join(response.streaming_content), expected)

    def test_media_rejects_mutation_methods_before_ticking_room(self):
        before = copy.deepcopy(Room.objects.get(pk=1).state)
        for method in ("post", "put", "patch", "delete", "options"):
            with self.subTest(method=method):
                response = getattr(self.host, method)(self.url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], "GET, HEAD")
                self.assertEqual(Room.objects.get(pk=1).state, before)

    def test_empty_media_has_zero_length_and_no_satisfiable_ranges(self):
        Path(self.directory.name, "media/test.wav").write_bytes(b"")
        response = self.host.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], "0")
        self.assertEqual(b"".join(response.streaming_content), b"")
        for value in ("bytes=0-", "bytes=0-0", "bytes=-1", "bytes=-0"):
            with self.subTest(range=value):
                response = self.host.get(self.url, HTTP_RANGE=value)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response["Content-Range"], "bytes */0")
