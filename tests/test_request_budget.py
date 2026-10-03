"""Real subprocess admission checks and offline HTTP/query regressions."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from email.utils import formatdate
from pathlib import Path
from unittest import mock

import booth
import request_budget as budget
import smart_search


WORKER = """
import json, sys, time
import request_budget as b
request_id, maximum, interval = sys.argv[1:]
try:
    value = {"request_id":request_id, "max_requests":int(maximum), "deadline":time.time()+20}
    with b.query_context(value if request_id else None):
        stamp = b.wait(float(interval), max_wait=10)
        print(json.dumps({"ok":True, "stamp":stamp, "stats":b.statistics()}))
except b.BudgetError as e:
    print(json.dumps({"ok":False, "error":str(e)}))
"""


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "admission.sqlite3"
        self.env = mock.patch.dict(os.environ, {"BOOTH_REQUEST_BUDGET_DB": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def workers(self, count=6, request_id="", maximum=12, interval=0.08):
        processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", WORKER,
                     request_id, str(maximum), str(interval)],
                     cwd=Path(booth.__file__).resolve().parent, env=os.environ.copy(),
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                     for _ in range(count)]
        values = []
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err)
            values.append(json.loads(out))
        return values


class TestRequestBudget(StorageFixture):
    def test_six_cold_processes_share_admission_spacing(self):
        values = self.workers()
        self.assertTrue(all(v["ok"] for v in values), values)
        stamps = sorted(v["stamp"] for v in values)
        self.assertTrue(all(b-a >= 0.075 for a, b in zip(stamps, stamps[1:])), stamps)

    def test_atomic_query_cap_shared_by_six_processes(self):
        values = self.workers(request_id="same-query-123", maximum=3)
        self.assertEqual(sum(v["ok"] for v in values), 3, values)
        with budget.query_context(dict(request_id="same-query-123", max_requests=3, deadline=time.time()+20)):
            self.assertEqual(budget.statistics(), {"used": 3, "maximum": 3})

    def test_shared_cooldown_blocks_three_processes(self):
        budget.cooldown(0.3)
        started = time.monotonic()
        values = self.workers(count=3)
        self.assertTrue(all(v["ok"] for v in values), values)
        self.assertGreaterEqual(min(v["stamp"] for v in values)-started, 0.29)

    def test_explicit_retry_extension_keeps_original_deadline_and_used(self):
        value = dict(request_id="extension-123", max_requests=1, deadline=time.time()+20)
        with budget.query_context(value):
            budget.wait(0)
            with self.assertRaises(budget.BudgetError):
                budget.wait(0)
        extended = dict(value, max_requests=2, deadline=time.time()+40)
        with budget.query_context(extended):
            budget.wait(0)
            self.assertEqual(budget.statistics()["used"], 2)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT deadline FROM queries").fetchone()[0], value["deadline"])

    def test_crashed_transaction_releases_lock(self):
        budget.cooldown(0)
        code = ("import os, sqlite3; c=sqlite3.connect(os.environ['BOOTH_REQUEST_BUDGET_DB']); "
                "c.execute('BEGIN IMMEDIATE'); os._exit(7)")
        process = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(), timeout=5)
        self.assertEqual(process.returncode, 7)
        budget.wait(0, max_wait=1)

    def test_invalid_context_and_expired_query_are_rejected(self):
        valid = dict(request_id="valid-id-123", max_requests=12, deadline=time.time()+20)
        for changes in ({"request_id": "x"}, {"max_requests": True}, {"max_requests": 101},
                        {"deadline": float("nan")}, {"deadline": float("inf")}, {"deadline": time.time()+1000}):
            with self.assertRaises(budget.BudgetError):
                budget.validate_context(dict(valid, **changes))
        with budget.query_context(dict(valid, deadline=time.time()-1)):
            with self.assertRaises(budget.BudgetError):
                budget.wait(0)
        self.assertFalse(self.path.exists(), "Expired queries must fail before opening storage")

    def test_read_only_or_broken_storage_fails_closed(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("file", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BOOTH_REQUEST_BUDGET_DB": str(blocker / "db")}):
            with self.assertRaises(budget.BudgetError):
                budget.wait(0)

    def test_wall_clock_correction_preserves_monotonic_cooldown(self):
        state = budget._clock_state((900, 99, 1050, 150), wall=1100, mono=100)
        self.assertEqual(state[3], 150)
        self.assertEqual(state[2], 1150)
        reboot = budget._clock_state((900, 100, 1050, 150), wall=1020, mono=2)
        self.assertEqual(reboot[1], 2)
        self.assertEqual(reboot[3], 32)


class TestWireBudget(StorageFixture):
    def test_cache_hit_uses_no_budget_or_admission_db(self):
        context = dict(request_id="cache-hit-123", max_requests=1, deadline=time.time()+20)
        with budget.query_context(context), mock.patch.object(booth, "cache_get", return_value=(b"cached", "url")), \
                mock.patch.object(booth._OPENER, "open") as wire:
            self.assertEqual(booth.http_get("https://booth.pm/items/1", cache_ttl=100)[0], b"cached")
            self.assertEqual(budget.statistics()["used"], 0)
        wire.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_retry_after_90_is_persisted_without_early_retry(self):
        url = "https://booth.pm/items/1"
        error = urllib.error.HTTPError(url, 429, "rate limited", {"Retry-After": "90"}, io.BytesIO())
        value = dict(request_id="retry-429-123", max_requests=3, deadline=time.time()+30)
        with budget.query_context(value), mock.patch.object(booth, "cache_get", return_value=None), \
                mock.patch.object(booth._OPENER, "open", side_effect=error) as wire:
            with self.assertRaisesRegex(booth.BoothError, "冷却"):
                booth.http_get(url)
            self.assertEqual(budget.statistics()["used"], 1)
        self.assertEqual(wire.call_count, 1)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            until = conn.execute("SELECT cooldown_wall FROM admission").fetchone()[0]
        self.assertGreater(until-time.time(), 89)

    def test_retry_header_invalid_values_fall_back(self):
        with mock.patch.object(booth.random, "uniform", return_value=0):
            for value in ("-1", "NaN", "inf", "90.5", "garbage"):
                self.assertEqual(booth._retry_delay(0, {"Retry-After": value}), 1.5)
            self.assertEqual(booth._retry_delay(0, {"Retry-After": "90"}), 90)
            with mock.patch.object(booth.time, "time", return_value=1000):
                self.assertEqual(booth._retry_delay(0, {"Retry-After": formatdate(1090, usegmt=True)}), 90)

    def test_redirects_consume_separate_wire_tokens(self):
        url = "https://booth.pm/items/1"
        error = urllib.error.HTTPError(url, 302, "redirect", {"Location": "/items/2"}, io.BytesIO())
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"ok"
        response.headers = {}
        response.status = 200
        value = dict(request_id="redirect-123", max_requests=2, deadline=time.time()+20)
        with budget.query_context(value), mock.patch.object(booth, "MIN_REQUEST_INTERVAL", 0), \
                mock.patch.object(booth, "cache_get", return_value=None), \
                mock.patch.object(booth._OPENER, "open", side_effect=[error, response]) as wire:
            self.assertEqual(booth.http_get(url)[0], b"ok")
            self.assertEqual(budget.statistics()["used"], 2)
        self.assertEqual(wire.call_count, 2)

    def test_subprocess_bot_envelope_context_and_counters(self):
        # Exercise real argparse/envelope and local cached HTTP; zero network.
        args = booth.build_parser().parse_args(["bot", json.dumps({
            "action": "item", "params": {"id": 1}, "context": dict(
                request_id="envelope-123", max_requests=12, deadline=time.time()+20)})])
        with mock.patch.object(booth, "cache_get", return_value=(b'{"id":1,"name":"cached"}', "url")), \
                mock.patch.object(booth._OPENER, "open") as wire, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_bot(args)
        value = json.loads(output.getvalue())
        self.assertTrue(value["ok"])
        self.assertEqual(value["request_budget"], {"used": 0, "maximum": 12})
        wire.assert_not_called()


class TestSmartStages(unittest.TestCase):
    def test_full_details_before_evaluation_reused_for_output(self):
        items = [dict(id=i, name=f"衣装 {i}", price=100, shop={}, url=f"https://booth.pm/items/{i}")
                 for i in range(1, 9)]
        args = booth.build_parser().parse_args(["smart", "Rexouium 衣装", "--no-webfind", "--json"])
        def detail(item_id, _lang):
            return dict(id=int(item_id), name="衣装", description="説明 " * 2000 + "Rexouium 対応",
                        tags=[{"name": "VRChat"}], category="3D衣装")
        def evaluate(query, kws, lines, **backend):
            self.assertTrue(all(it.get("detail_status") == "available" for it in items[:6]))
            self.assertIn("Rexouium 対応", "\n".join(lines))
            return {"verdict": "ok", "hits": ["1", "2", "3"], "evidence": [
                dict(item_id=str(it["id"]), field="description", quote="Rexouium 対応", status="related")
                for it in items[:3]]}
        with mock.patch.object(smart_search, "ai_backend", return_value={"model": "offline"}), \
                mock.patch.object(smart_search, "plan_search", return_value=(["衣装"], ["Rexouium"], True)), \
                mock.patch.object(booth, "_merged_search", return_value=(items, {"total": 8}, None)), \
                mock.patch.object(booth, "fetch_item", side_effect=detail) as fetch, \
                mock.patch.object(smart_search, "evaluate_results", side_effect=evaluate), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_smart(args)
        self.assertEqual(fetch.call_count, 6)
        value = json.loads(output.getvalue())
        self.assertEqual(value["items"][0]["relevance_status"], "related")
        self.assertNotIn("_desc", value["items"][0])
        self.assertEqual(len(value["items"]), 3)
        self.assertEqual(value["quality"]["omitted"], 5)
        self.assertEqual(items[3]["relevance_status"], "unknown")

    def test_benchmark_profile_does_not_use_configured_ai(self):
        with mock.patch.dict(os.environ, {"RUN_PROFILE": "benchmark", "VISION_API_KEY": "placeholder",
                                         "AI_FALLBACK_API_KEY": "placeholder", "BENCHMARK_ALLOW_AI": ""}):
            self.assertIsNone(smart_search.ai_backend())
            self.assertIsNone(smart_search.ai_fallback_backend())

    def test_fallback_only_backend_is_usable(self):
        with mock.patch.dict(os.environ, {"RUN_PROFILE": "production", "VISION_API_KEY": "",
                                         "AI_FALLBACK_API_KEY": "placeholder"}):
            self.assertEqual(smart_search.ai_backend(), smart_search.ai_fallback_backend())


if __name__ == "__main__":
    unittest.main()
