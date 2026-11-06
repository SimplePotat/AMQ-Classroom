"""Regression tests for the LAN trust boundary, credentials and private data."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TransactionTestCase, override_settings

from . import engine, views
from .models import Profile


@override_settings(ALLOWED_HOSTS=["testserver"], CLASSROOM_PREWARM_ENABLED=False)
class SecurityRegressionTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()

    def post(self, client, path, data, **headers):
        if "csrftoken" in client.cookies:
            headers.setdefault("HTTP_X_CSRFTOKEN", client.cookies["csrftoken"].value)
        return client.post(path, json.dumps(data), content_type="application/json", **headers)

    def host_login(self, client):
        client.get("/")
        response = self.post(client, "/api/auth", {"action": "host", "key": settings.HOST_KEY})
        self.assertEqual(response.status_code, 200, response.content)

    def register(self, client, name="Alice", mal="alice_private_link"):
        client.get("/")
        response = self.post(client, "/api/auth", {
            "action": "register", "name": name, "password": "security-test-passcode", "mal": mal,
        })
        self.assertEqual(response.status_code, 200, response.content)

    def seed_media(self):
        with engine.room_state() as state:
            state.update(phase="buffering", index=0, queue=[{
                "id": "test", "roundId": "security-round", "anime": "Test anime", "aliases": [],
                "song": "Test song", "artist": "Test artist", "type": "OP", "year": 2024,
                "media": "https://v.animethemes.moe/Test.webm", "startAt": 0,
            }])

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_dns_rebinding_hostname_is_rejected_on_pages_assets_and_api(self):
        client = Client(enforce_csrf_checks=True)
        # A rebinding site could otherwise fetch its own CSRF cookie/token and
        # present a matching Origin after its hostname resolves to the LAN IP.
        client.get("/", HTTP_HOST="127.0.0.1:8009")
        token = client.cookies["csrftoken"].value
        for path in ["/", "/host", "/assets/app.js", "/api/state", "/api/qr"]:
            with self.subTest(path=path):
                response = client.get(path, HTTP_HOST="attacker.example:8009")
                self.assertEqual(response.status_code, 400)
        response = self.post(client, "/api/auth", {
            "action": "register", "name": "Rebound", "password": "1234",
        }, HTTP_HOST="attacker.example:8009", HTTP_ORIGIN="http://attacker.example:8009",
            HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 400)

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_literal_lan_addresses_and_localhost_still_work(self):
        for host in ["localhost:8009", "127.0.0.1:8009", "192.168.1.50:8009", "[::1]:8009"]:
            with self.subTest(host=host):
                self.assertEqual(Client().get("/", HTTP_HOST=host).status_code, 200)

    @override_settings(ALLOWED_HOSTS=["classroom.school.test"])
    def test_named_hosts_require_explicit_configuration(self):
        self.assertEqual(Client().get("/", HTTP_HOST="classroom.school.test:8009").status_code, 200)
        self.assertEqual(Client().get("/", HTTP_HOST="attacker.example:8009").status_code, 400)

    def test_host_key_rotation_revokes_existing_control_and_media_access(self):
        client = Client()
        self.host_login(client)
        self.seed_media()
        self.assertEqual(client.get("/api/media/security-round").status_code, 302)
        with override_settings(HOST_KEY="a-different-host-key"):
            self.assertFalse(client.get("/api/state").json()["host"])
            self.assertEqual(client.get("/api/media/security-round").status_code, 403)
            self.assertEqual(self.post(client, "/api/host", {"action": "reset"}).status_code, 403)
            self.assertEqual(client.get("/api/results.csv").status_code, 403)

    def test_expired_host_grant_cannot_load_media_or_control_game(self):
        client = Client()
        self.host_login(client)
        self.seed_media()
        session = client.session
        session["host_grant"]["expires"] = time.time() - 1
        session.save()
        self.assertFalse(client.get("/api/state").json()["host"])
        self.assertEqual(client.get("/api/media/security-round").status_code, 403)
        self.assertEqual(self.post(client, "/api/host", {"action": "reset"}).status_code, 403)

    def test_old_boolean_host_sessions_are_not_privileged(self):
        client = Client()
        session = client.session
        session["host"] = True
        session.save()
        self.assertFalse(client.get("/api/state").json()["host"])
        self.assertEqual(self.post(client, "/api/host", {"action": "reset"}).status_code, 403)

    def test_signing_in_as_player_drops_existing_host_authority(self):
        client = Client()
        self.host_login(client)
        self.register(client)
        state = client.get("/api/state").json()
        self.assertEqual(state["me"]["name"], "Alice")
        self.assertFalse(state["host"])
        self.assertEqual(self.post(client, "/api/host", {"action": "reset"}).status_code, 403)

    def test_host_login_drops_player_identity(self):
        client = Client()
        self.register(client)
        self.host_login(client)
        state = client.get("/api/state").json()
        self.assertTrue(state["host"])
        self.assertIsNone(state["me"])
        self.assertEqual(self.post(client, "/api/player", {"action": "ready"}).status_code, 401)

    def test_login_and_logout_rotate_csrf_and_revoke_old_session_cookie(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/")
        before_login = client.cookies["csrftoken"].value
        self.host_login(client)
        after_login = client.cookies["csrftoken"].value
        self.assertNotEqual(before_login, after_login)
        response = self.post(client, "/api/host", {"action": "reset"}, HTTP_X_CSRFTOKEN=before_login)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(client, "/api/host", {"action": "reset"}).status_code, 200)
        old_session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(self.post(client, "/api/logout", {"role": "host"}).status_code, 200)
        self.assertNotEqual(old_session, client.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertNotEqual(after_login, client.cookies["csrftoken"].value)
        copied_cookie = Client()
        copied_cookie.cookies[settings.SESSION_COOKIE_NAME] = old_session
        self.assertFalse(copied_cookie.get("/api/state").json()["host"])
        self.assertEqual(self.post(copied_cookie, "/api/host", {"action": "reset"}).status_code, 403)

    def test_anonymous_state_does_not_disclose_roster_or_game_details(self):
        player = Client()
        self.register(player)
        response = Client().get("/api/state")
        self.assertEqual(response.status_code, 200)
        state = response.json()
        for key in ["players", "phase", "history", "titles", "roundId", "mediaUrl"]:
            self.assertNotIn(key, state)
        self.assertNotIn("alice_private_link", response.content.decode())

    def test_player_sees_own_mal_link_but_not_other_players_links(self):
        alice, bob = Client(), Client()
        self.register(alice)
        self.register(bob, "Bob", "bob_private_link")
        response = alice.get("/api/state")
        state = response.json()
        self.assertEqual(state["me"]["mal"], "alice_private_link")
        self.assertNotIn("bob_private_link", response.content.decode())
        self.assertTrue(all("mal" not in player for player in state["players"]))

    def test_oversized_json_is_rejected_before_processing(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/")
        response = self.post(client, "/api/auth", {"payload": "x" * (settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["Cache-Control"], "no-store")


    @override_settings(ALLOWED_HOSTS=["*", "classroom.school.test"])
    def test_mixed_wildcard_configuration_does_not_disable_rebinding_boundary(self):
        for host in ["127.0.0.1:8009", "localhost:8009", "classroom.school.test:8009"]:
            with self.subTest(host=host):
                self.assertEqual(Client().get("/", HTTP_HOST=host).status_code, 200)
        self.assertEqual(Client().get("/api/state", HTTP_HOST="attacker.example:8009").status_code, 400)

    def test_authentication_limit_ignores_spoofed_forwarding_headers_and_recovers(self):
        client = Client()
        now = time.time()
        with patch("classroom.views.time.time", return_value=now) as clock:
            for _ in range(30):
                self.assertEqual(self.post(client, "/api/auth", {"action": "host", "key": "wrong"}).status_code, 403)
            rejected = self.post(client, "/api/auth", {"action": "host", "key": settings.HOST_KEY},
                                 HTTP_X_FORWARDED_FOR="192.168.1.42")
            self.assertEqual(rejected.status_code, 429)
            self.assertEqual(rejected["Cache-Control"], "no-store")
            self.assertEqual(self.post(client, "/api/auth", {"action": "host", "key": "wrong"},
                                       REMOTE_ADDR="192.168.1.43").status_code, 403)
            clock.return_value = now + 301
            self.assertEqual(self.post(client, "/api/auth", {"action": "host", "key": settings.HOST_KEY}).status_code, 200)

    def test_player_mutations_are_rate_limited_independently_of_authentication(self):
        client = Client()
        self.register(client)
        now = time.time()
        with patch("classroom.views.time.time", return_value=now) as clock:
            for _ in range(120):
                self.assertEqual(self.post(client, "/api/player", {"action": "ready"}).status_code, 200)
            rejected = self.post(client, "/api/player", {"action": "ready"}, HTTP_X_FORWARDED_FOR="192.168.1.42")
            self.assertEqual(rejected.status_code, 429)
            clock.return_value = now + 61
            self.assertEqual(self.post(client, "/api/player", {"action": "ready"}).status_code, 200)

    def test_matching_token_does_not_allow_cross_origin_mutation(self):
        client = Client(enforce_csrf_checks=True)
        self.register(client)
        for path, data in [
            ("/api/auth", {"action": "host", "key": settings.HOST_KEY}),
            ("/api/player", {"action": "profile", "name": "Attacker", "mal": ""}),
            ("/api/host", {"action": "reset"}),
            ("/api/logout", {"role": "player"}),
        ]:
            with self.subTest(path=path):
                response = self.post(client, path, data, HTTP_ORIGIN="https://attacker.example")
                self.assertEqual(response.status_code, 403)
                self.assertIn(b"CSRF", response.content)
        self.assertEqual(client.get("/api/state").json()["me"]["name"], "Alice")

    def test_full_roster_does_not_create_unusable_profiles(self):
        with engine.room_state() as state:
            state["players"] = {str(i): {} for i in range(100)}
        response = self.post(Client(), "/api/auth", {
            "action": "register", "name": "Overflow", "password": "security-test-passcode",
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Profile.objects.count(), 0)

    def test_room_filling_during_password_hash_does_not_create_or_sign_in_profile(self):
        client = Client()
        self.host_login(client)
        host_grant = client.session["host_grant"]
        make_password = views.make_password

        def fill_room_while_hashing(password):
            # Another request can take the final seat while the expensive hash
            # runs outside the room transaction. The final join must be atomic.
            with engine.room_state() as state:
                state["players"] = {str(i): {} for i in range(100)}
            return make_password(password)

        with patch("classroom.views.make_password", side_effect=fill_room_while_hashing):
            response = self.post(client, "/api/auth", {
                "action": "register", "name": "Racing player", "password": "security-test-passcode",
            })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Profile.objects.count(), 0)
        self.assertNotIn("profile_id", client.session)
        self.assertEqual(client.session["host_grant"], host_grant)

    def test_failed_join_during_login_preserves_current_identity(self):
        self.register(Client())
        client = Client()
        self.host_login(client)
        host_grant = client.session["host_grant"]
        with engine.room_state() as state:
            state["players"] = {f"other-{i}": {} for i in range(100)}
        response = self.post(client, "/api/auth", {
            "action": "login", "name": "Alice", "password": "security-test-passcode",
        })
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("profile_id", client.session)
        self.assertEqual(client.session["host_grant"], host_grant)

    def test_final_csv_survives_player_renaming_and_leaving(self):
        host, player = Client(), Client()
        self.host_login(host)
        self.register(player)
        self.seed_media()
        with engine.room_state() as state:
            state["gameId"] = "csv-security-game"
        self.assertEqual(self.post(host, "/api/host", {"action": "begin", "roundId": "security-round"}).status_code, 200)
        self.assertEqual(self.post(player, "/api/player", {"action": "guess", "roundId": "security-round", "text": "Test anime"}).status_code, 200)
        self.assertEqual(self.post(host, "/api/host", {"action": "reveal"}).status_code, 200)
        self.assertEqual(self.post(host, "/api/host", {"action": "next"}).status_code, 200)
        original = host.get("/api/results.csv").content
        self.assertIn(b"Alice,alice_private_link,1", original)
        self.assertEqual(self.post(player, "/api/player", {"action": "profile", "name": "Changed name", "mal": "new_mal"}).status_code, 200)
        self.assertEqual(self.post(player, "/api/player", {"action": "leave"}).status_code, 200)
        self.assertEqual(host.get("/api/results.csv").content, original)

    @skipUnless(os.name == "posix", "POSIX permission bits are not meaningful on Windows")
    def test_database_secrets_and_journal_remain_private_under_default_umask(self):
        # A subprocess prevents this check from changing the test runner's umask
        # or using the live classroom's database and credentials.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "data"
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            for name in [".host-key", ".django-key", "classroom.sqlite3"]:
                path = directory / name
                path.write_text("" if name.endswith("sqlite3") else "isolated-test-secret")
                path.chmod(0o644)
            script = textwrap.dedent("""
                import json, os
                os.umask(0o022)
                import django
                django.setup()
                from django.conf import settings
                from django.core.management import call_command
                from django.db import connection
                call_command('migrate', verbosity=0)
                with connection.cursor() as cursor:
                    cursor.execute('CREATE TABLE permission_probe (value INTEGER)')
                    cursor.execute('BEGIN IMMEDIATE')
                    cursor.execute('INSERT INTO permission_probe VALUES (1)')
                    modes = {path.name: path.stat().st_mode & 0o777 for path in settings.DATA_DIR.iterdir()}
                    modes['data_directory'] = settings.DATA_DIR.stat().st_mode & 0o777
                    cursor.execute('ROLLBACK')
                print(json.dumps(modes))
            """)
            env = {**os.environ, "CLASSROOM_DATA_DIR": str(directory), "DJANGO_SETTINGS_MODULE": "classroom.settings"}
            env.pop("CLASSROOM_HOST_KEY", None)
            env.pop("CLASSROOM_SECRET_KEY", None)
            result = subprocess.run([sys.executable, "-c", script], env=env,
                cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=30, check=True)
            modes = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(modes["data_directory"], 0o700)
            for name in [".host-key", ".django-key", "classroom.sqlite3", "classroom.sqlite3-journal"]:
                self.assertEqual(modes[name], 0o600, name)
