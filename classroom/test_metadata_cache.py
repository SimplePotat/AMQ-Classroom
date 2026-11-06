"""Caching, provider limits, cancellation, and optional lobby warming."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest.mock import MagicMock, Mock, patch

from django.db import close_old_connections
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from . import engine, library, metadata, prefetch, views
from .models import CachedResource, Profile, Room
from .test_library_security import provider_response


def anime(mal_id, record_id=None):
    return {"id": record_id or mal_id, "name": f"Anime {mal_id}", "year": 2020,
            "resources": [{"site": "MyAnimeList", "external_id": mal_id}],
            "animesynonyms": [{"text": f"Alias {mal_id}"}],
            "animethemes": [{"id": record_id or mal_id, "type": "OP",
                            "song": {"title": f"Song {mal_id}", "artists": [{"name": "Artist"}]},
                            "animethemeentries": [{"nsfw": False, "spoiler": False, "videos": [
                                {"link": f"https://v.animethemes.moe/{mal_id}.webm", "resolution": 480}]}]}]}


def page(*records, next_page=None):
    return {"anime": list(records), "links": {"next": next_page}}


def batch_response(url, params):
    return page(*(anime(int(i)) for i in params["filter[external_id]"].split(",")))


def mal_entries(count):
    return [{"anime_id": i, "anime_title": f"Title {i}", "status": 2} for i in range(1, count + 1)]


class AnimeBatchCacheTests(TransactionTestCase):
    def test_forty_cold_ids_use_four_requests_and_warm_ids_use_none(self):
        with patch("classroom.library.get_json", side_effect=batch_response) as fetch:
            cold = library.anime_data_batch(range(1, 41))
            self.assertEqual(fetch.call_count, 4)
            for call in fetch.call_args_list:
                self.assertEqual(call.args[0], library.API + "/anime")
                self.assertEqual(call.args[1]["filter[has]"], "resources")
                self.assertEqual(call.args[1]["filter[site]"], "MyAnimeList")
                self.assertIn("resources", call.args[1]["include"].split(","))
            fetch.reset_mock()
            with patch("classroom.metadata.wait_for_provider", side_effect=AssertionError("Cache hit made HTTP request")):
                warm = library.anime_data_batch(range(40, 0, -1))
            self.assertEqual(cold, warm)
            fetch.assert_not_called()

    def test_unordered_results_multiple_records_and_other_site_ids(self):
        first, second = anime(1), anime(2)
        second["resources"].append({"site": "AniList", "external_id": 1})
        with patch("classroom.library.get_json", return_value=page(second, first, anime(1, record_id=99))):
            found = library.anime_data_batch([1, 2, 1, 3])
        self.assertEqual([a["id"] for a in found[1]], [1, 99])
        self.assertEqual([a["id"] for a in found[2]], [2])
        self.assertEqual(found[3], [])
        self.assertEqual(CachedResource.objects.count(), 3)

    def test_pagination_uses_fixed_endpoint_and_checks_all_pages_before_missing_cache(self):
        with patch("classroom.library.get_json", side_effect=[
            page(anime(1), next_page="http://127.0.0.1/private"), page(anime(2)),
        ]) as fetch:
            result = library.anime_data_batch([1, 2, 3])
        self.assertTrue(result[2])
        self.assertEqual(result[3], [])
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [library.API + "/anime"] * 2)
        self.assertEqual([c.args[1]["page[number]"] for c in fetch.call_args_list], [1, 2])

    def test_failed_second_page_cannot_cache_partial_or_negative_results(self):
        with patch("classroom.library.get_json", side_effect=[page(anime(1), next_page="more"), engine.GameError("Offline")]):
            with self.assertRaises(engine.GameError):
                library.anime_data_batch([1, 2])
        self.assertFalse(CachedResource.objects.exists())

    def test_malformed_or_unrelated_records_are_not_cached(self):
        for payload in ({}, {"anime": []}, page({}), page(anime(9)), page(*[anime(1, i) for i in range(1, 17)])):
            with self.subTest(payload=payload), patch("classroom.library.get_json", return_value=payload):
                with self.assertRaises(engine.GameError):
                    library.anime_data_batch([1])
                self.assertFalse(CachedResource.objects.exists())

    def test_pagination_is_bounded(self):
        with patch("classroom.library.MAX_ANIME_PAGES", 2), patch("classroom.library.get_json", side_effect=[
            page(anime(1, 10), next_page="more"), page(anime(1, 11), next_page="more"),
        ]) as fetch:
            with self.assertRaisesMessage(engine.GameError, "too many anime pages"):
                library.anime_data_batch([1])
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(CachedResource.objects.exists())

    def test_oversized_batch_splits_without_raising_the_transport_limit(self):
        def respond(url, params):
            if "," in params["filter[external_id]"]:
                raise metadata.ResponseTooLarge()
            return batch_response(url, params)
        with patch("classroom.library.get_json", side_effect=respond) as fetch:
            found = library.anime_data_batch([1, 2])
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(found[1] and found[2])

    def test_legacy_hits_survive_but_old_false_misses_and_expired_data_are_retried(self):
        for key, value, expiry in [("anime:1", anime(1), time.time() + 100),
                                   ("anime:2", {}, time.time() + 100),
                                   ("anime:mal:3", [anime(3)], time.time() - 1)]:
            CachedResource.objects.create(key=key, value=value, expires_at=expiry)
        with patch("classroom.library.get_json", side_effect=batch_response) as fetch:
            found = library.anime_data_batch([1, 2, 3])
        self.assertEqual(fetch.call_args.args[1]["filter[external_id]"], "2,3")
        self.assertEqual(found[1], [anime(1)])

    def test_confirmed_misses_have_shorter_ttl_than_hits(self):
        before = time.time()
        with patch("classroom.library.get_json", return_value=page(anime(1))):
            library.anime_data_batch([1, 2])
        for mal_id, ttl in [(1, library.ANIME_TTL), (2, library.MISSING_ANIME_TTL)]:
            expiry = CachedResource.objects.get(key=f"anime:mal:{mal_id}").expires_at
            self.assertGreaterEqual(expiry, before + ttl)
            self.assertLessEqual(expiry, time.time() + ttl)

    def test_concurrent_cache_misses_fetch_once(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(url, params):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test fetch was not released")
            return batch_response(url, params)
        def load():
            try:
                return library.anime_data_batch([1, 2])
            finally:
                close_old_connections()
        with patch("classroom.library.get_json", side_effect=fetch) as request, ThreadPoolExecutor(2) as workers:
            first = workers.submit(load)
            self.assertTrue(entered.wait(3))
            second = workers.submit(load)
            release.set()
            self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        self.assertEqual(request.call_count, 1)

    def test_cache_hits_do_not_wait_for_unrelated_downloads(self):
        CachedResource.objects.create(key="anime:mal:1", value=[anime(1)], expires_at=time.time() + 100)
        CachedResource.objects.create(key="mal:alice", value=mal_entries(1), expires_at=time.time() + 100)
        entered, release = threading.Event(), threading.Event()
        def hold_lock():
            with library.CACHE_LOCK:
                entered.set()
                release.wait(3)
        with ThreadPoolExecutor(2) as workers:
            holder = workers.submit(hold_lock)
            self.assertTrue(entered.wait(3))
            def read_cache():
                try:
                    return library.anime_data_batch([1]), library.mal_list("alice")
                finally:
                    close_old_connections()
            try:
                found, entries = workers.submit(read_cache).result(timeout=1)
                self.assertEqual(found[1], [anime(1)])
                self.assertEqual(entries, mal_entries(1))
            finally:
                release.set()
                holder.result(timeout=3)

    def test_prepare_batches_the_real_group_pool(self):
        with engine.room_state() as state:
            state.update(phase="loading", loadId="batch-test")
        with patch("classroom.library.mal_list", return_value=mal_entries(40)), patch(
                "classroom.library.get_json", side_effect=batch_response) as fetch:
            library.prepare("batch-test", engine.validate_settings({"source": "mal", "rounds": 100}), ["alice"])
        state = Room.objects.get(pk=1).state
        self.assertEqual(state["phase"], "ready", state["message"])
        self.assertEqual(len(state["queue"]), 40)
        self.assertEqual(fetch.call_count, 4)

    def test_cancelled_preparation_does_not_issue_the_next_batch(self):
        with engine.room_state() as state:
            state.update(phase="loading", loadId="cancel-test")
        def respond(url, params):
            with engine.room_state() as state:
                engine.control(state, "reset", {})
            return batch_response(url, params)
        with patch("classroom.library.mal_list", return_value=mal_entries(40)), patch(
                "classroom.library.get_json", side_effect=respond) as fetch:
            library.prepare("cancel-test", engine.validate_settings({"source": "mal", "rounds": 100}), ["alice"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(Room.objects.get(pk=1).state["phase"], "lobby")


class MetadataConnectionTests(SimpleTestCase):
    def setUp(self):
        metadata.close_session()
        metadata.NEXT_REQUEST.clear()
        metadata.COOLDOWN.clear()
        self.addCleanup(metadata.close_session)
        self.addCleanup(metadata.NEXT_REQUEST.clear)
        self.addCleanup(metadata.COOLDOWN.clear)

    def test_one_session_reused_per_thread_and_closed(self):
        with patch("classroom.metadata.requests.Session") as factory:
            first = metadata.http_session()
            self.assertIs(first, metadata.http_session())
            def other_thread():
                try:
                    metadata.http_session()
                    metadata.http_session()
                finally:
                    metadata.close_session()
            with ThreadPoolExecutor(1) as worker:
                worker.submit(other_thread).result(timeout=3)
            metadata.close_session()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.return_value.close.call_count, 2)

    def test_429_stops_subsequent_requests_for_the_provider(self):
        response = provider_response(status=429)
        response.headers["Retry-After"] = "120"
        with patch("classroom.metadata.time.monotonic", return_value=100), patch(
                "classroom.metadata.requests.Session.get", return_value=response) as get:
            for _ in range(2):
                with self.assertRaises(metadata.RateLimited):
                    metadata.get_json(library.API + "/anime")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(metadata.COOLDOWN["api.animethemes.moe"], 220)
        response.__exit__.assert_called_once()

    def test_retry_after_accepts_http_dates_and_bad_values(self):
        with patch("classroom.metadata.time.time", return_value=1000):
            self.assertEqual(metadata.retry_after(formatdate(1120, usegmt=True)), 120)
        for value in (None, "invalid", "inf", "nan"):
            self.assertEqual(metadata.retry_after(value), 60)

    def test_pacing_and_request_budget_apply_to_all_network_calls(self):
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch("classroom.metadata.time.monotonic", side_effect=lambda: clock[0]), patch(
                "classroom.metadata.time.sleep", side_effect=sleep), patch(
                "classroom.metadata.requests.Session.get", side_effect=lambda *a, **k: provider_response()) as get:
            with metadata.work_context(background=True, request_budget=2):
                metadata.get_json(library.API + "/anime")
                metadata.get_json(library.API + "/anime")
                with self.assertRaises(InterruptedError):
                    metadata.get_json(library.API + "/anime")
        self.assertEqual(get.call_count, 2)
        self.assertGreaterEqual(clock[0], 102)

    def test_waiting_for_a_provider_is_cancellable(self):
        metadata.NEXT_REQUEST["api.animethemes.moe"] = time.monotonic() + 10
        with metadata.work_context(check=Mock(side_effect=[None, InterruptedError()])), patch(
                "classroom.metadata.time.sleep"), patch("classroom.metadata.requests.Session.get") as get:
            with self.assertRaises(InterruptedError):
                metadata.get_json(library.API + "/anime")
        get.assert_not_called()


@override_settings(CLASSROOM_PREWARM_ENABLED=True)
class LobbyCacheTests(TransactionTestCase):
    def setUp(self):
        prefetch.PENDING.clear()
        prefetch.RECENT.clear()
        prefetch.WORKER = None
        self.addCleanup(prefetch.PENDING.clear)
        self.addCleanup(prefetch.RECENT.clear)
        self.addCleanup(setattr, prefetch, "WORKER", None)
        with engine.room_state() as state:
            state["players"] = {str(i): {"mal": f"player_{i}", "name": f"Player {i}", "ready": True}
                                for i in range(1, 4)}

    def test_join_queue_is_coalesced_and_starts_only_one_worker(self):
        with patch("classroom.prefetch.threading.Thread") as worker, patch("classroom.library.get_json") as fetch:
            for _ in range(5):
                prefetch.schedule(1)
            prefetch.schedule(2)
            with engine.room_state() as state:
                state["players"]["1"]["mal"] = "edited_name"
            prefetch.schedule(1)
        worker.assert_called_once()
        worker.return_value.start.assert_called_once()
        fetch.assert_not_called()
        self.assertEqual(dict(prefetch.PENDING), {"1": "edited_name", "2": "player_2"})

    def test_queue_and_repeat_scheduling_are_bounded(self):
        with patch("classroom.prefetch.MAX_PENDING", 2), patch("classroom.prefetch.threading.Thread"):
            for i in range(1, 4):
                prefetch.schedule(i)
            self.assertEqual(len(prefetch.PENDING), 2)
            prefetch.PENDING.clear()
            prefetch.schedule(1)
            self.assertFalse(prefetch.PENDING)

    def test_local_mode_active_games_and_disabled_setting_do_not_schedule(self):
        with patch("classroom.prefetch.threading.Thread") as worker:
            with engine.room_state() as state:
                state["settings"]["source"] = "local"
            prefetch.schedule(1)
            with engine.room_state() as state:
                state["settings"]["source"] = "mal"
                state["phase"] = "guess"
            prefetch.schedule(1)
            with engine.room_state() as state:
                state["phase"] = "lobby"
            with override_settings(CLASSROOM_PREWARM_ENABLED=False):
                prefetch.schedule(1)
        worker.assert_not_called()

    def test_warming_populates_the_same_cache_used_by_preparation(self):
        def respond(url, params):
            return mal_entries(2) if "myanimelist.net" in url else batch_response(url, params)
        before = copy.deepcopy(Room.objects.get(pk=1).state)
        with patch("classroom.library.get_json", side_effect=respond) as fetch:
            prefetch._warm("1", "player_1")
            self.assertEqual(Room.objects.get(pk=1).state, before)
            self.assertEqual(fetch.call_count, 2)
            fetch.reset_mock()
            with engine.room_state() as state:
                state.update(phase="loading", loadId="prewarmed")
            library.prepare("prewarmed", engine.validate_settings({"source": "mal"}), ["player_1"])
            fetch.assert_not_called()
        self.assertEqual(Room.objects.get(pk=1).state["phase"], "ready")

    def test_warming_indexes_every_list_entry_including_unselected_statuses(self):
        with patch("classroom.library.mal_list", return_value=mal_entries(100) + [
            {"anime_id": 1000, "anime_title": "Included", "status": 6},
        ]), patch("classroom.library.get_json", side_effect=batch_response) as fetch:
            prefetch._warm("1", "player_1")
        self.assertEqual(CachedResource.objects.count(), 101)
        self.assertEqual(fetch.call_count, 11)
        self.assertTrue(CachedResource.objects.filter(key="anime:mal:1000").exists())

    def test_preparation_interrupts_warming_before_the_next_batch(self):
        def respond(url, params):
            with engine.room_state() as state:
                state.update(phase="loading", loadId="host-started")
            return batch_response(url, params)
        with patch("classroom.library.mal_list", return_value=mal_entries(40)), patch(
                "classroom.library.get_json", side_effect=respond) as fetch:
            with self.assertRaises(InterruptedError):
                prefetch._warm("1", "player_1")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(CachedResource.objects.count(), 10)
        self.assertEqual(Room.objects.get(pk=1).state["loadId"], "host-started")

    def test_changed_username_drops_stale_work(self):
        with engine.room_state() as state:
            state["players"]["1"]["mal"] = "new_username"
        with patch("classroom.library.get_json") as fetch:
            with self.assertRaises(InterruptedError):
                prefetch._warm("1", "player_1")
        fetch.assert_not_called()

    def test_background_checks_cannot_tick_or_score_an_expired_round(self):
        with engine.room_state() as state:
            for player in state["players"].values():
                player["score"] = 0
            state.update(phase="guess", deadline=time.time() - 1, index=0, queue=[{
                "roundId": "playing", "anime": "Anime", "aliases": [], "song": "Song",
            }])
        before = Room.objects.get(pk=1).state
        with patch("classroom.library.get_json") as fetch:
            prefetch.schedule(1)
            with self.assertRaises(InterruptedError):
                prefetch._warm("1", "player_1")
        self.assertEqual(Room.objects.get(pk=1).state, before)
        fetch.assert_not_called()

    def test_interrupted_background_warming_does_not_cache_partial_mal_lists(self):
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        body = json.dumps(mal_entries(300)).encode()
        with patch.dict(metadata.NEXT_REQUEST, {}, clear=True), patch.dict(metadata.COOLDOWN, {}, clear=True), patch(
                "classroom.metadata.time.monotonic", side_effect=lambda: clock[0]), patch(
                "classroom.metadata.time.sleep", side_effect=sleep), patch("classroom.metadata.http_session") as session:
            session.return_value.get.side_effect = lambda *a, **k: provider_response(chunks=(body,))
            with self.assertRaises(InterruptedError):
                prefetch._warm("1", "player_1")
            self.assertGreater(session.return_value.get.call_count, 0)
        self.assertFalse(CachedResource.objects.exists())

    def test_background_deadline_stops_theme_lookups(self):
        before_list, list_fetched, past_scaled_deadline = 0, 61, 200
        with patch("classroom.prefetch.time.monotonic", side_effect=[
            before_list, before_list, list_fetched, past_scaled_deadline]), patch(
                "classroom.library.mal_list", return_value=mal_entries(1)), patch(
                "classroom.library.anime_data_batch") as themes:
            with self.assertRaises(InterruptedError):
                prefetch._warm("1", "player_1")
        themes.assert_not_called()

    def test_scheduled_worker_populates_cache_and_exits(self):
        threads = []
        real_thread = threading.Thread
        def make_worker(**kwargs):
            thread = real_thread(**kwargs)
            threads.append(thread)
            return thread
        def respond(url, params):
            return mal_entries(2) if "myanimelist.net" in url else batch_response(url, params)
        with patch("classroom.prefetch.threading.Thread", side_effect=make_worker), patch(
                "classroom.library.get_json", side_effect=respond):
            prefetch.schedule(1)
            threads[0].join(timeout=5)
            self.assertFalse(threads[0].is_alive())
        self.assertEqual(CachedResource.objects.count(), 3)
        self.assertIsNone(prefetch.WORKER)

    def test_worker_failure_does_not_block_the_next_job_or_change_room(self):
        prefetch.PENDING.update({"1": "player_1", "2": "player_2"})
        before = Room.objects.get(pk=1).state
        with patch("classroom.prefetch._warm", side_effect=[engine.GameError("Private list"), None]) as warm, patch(
                "classroom.metadata.close_session") as close:
            prefetch._run()
        self.assertEqual(warm.call_count, 2)
        self.assertIsNone(prefetch.WORKER)
        self.assertEqual(Room.objects.get(pk=1).state, before)
        close.assert_called_once()


@override_settings(ALLOWED_HOSTS=["testserver"], CLASSROOM_PREWARM_ENABLED=True)
class PrefetchViewHooksTests(TransactionTestCase):
    def setUp(self):
        views.ATTEMPTS.clear()

    def test_successful_auth_profile_edit_and_join_enqueue_without_fetching(self):
        client = Client()
        with patch("classroom.prefetch.schedule") as schedule, patch("classroom.library.get_json") as fetch:
            def post(path, data):
                return client.post(path, json.dumps(data), content_type="application/json")
            response = post("/api/auth", {"action": "register", "name": "Alice", "password": "test-pass", "mal": "alice"})
            self.assertEqual(response.status_code, 200)
            profile = Profile.objects.get(name="Alice")
            schedule.assert_called_once_with(profile.pk)
            response = post("/api/player", {"action": "profile", "name": "Alice", "mal": "alice_new", "avatar": 2})
            self.assertEqual(response.status_code, 200)
            response = post("/api/player", {"action": "join"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(schedule.call_count, 3)
            response = post("/api/auth", {"action": "login", "name": "Alice", "password": "wrong"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(schedule.call_count, 3)
            fetch.assert_not_called()
