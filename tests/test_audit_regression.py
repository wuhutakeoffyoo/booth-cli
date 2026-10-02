"""审查 F01–F13 的离线回归，网络与 AI 均由固定响应替代。"""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest import mock

import booth
import reverse_search
import smart_search


class TestAuditRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"BOOTH_REQUEST_BUDGET_DB": str(Path(tmp.name) / "budget.sqlite3")})
        patch.start()
        self.addCleanup(patch.stop)

    def test_authenticated_redirects_do_not_send_second_request(self):
        seen = []

        class Wire(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
            def https_open(self, req):
                seen.append(req)
                headers = Message()
                headers["Location"] = "http://redirect.invalid/result"
                resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                resp.msg = "Found"
                return resp
            http_open = https_open

        opener = urllib.request.build_opener(smart_search._NoRedirect, Wire)
        with mock.patch.object(smart_search, "_AUTH_OPENER", opener):
            with self.assertRaises(smart_search.AiError):
                smart_search._post_chat("https://api.invalid/chat", {}, "placeholder", 1)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].get_header("Authorization"), "Bearer placeholder")
            seen.clear()
            with mock.patch.object(smart_search.provider_api.socket, "getaddrinfo",
                                   return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
                self.assertEqual(smart_search.exa_find("x", "placeholder"), [])
            self.assertEqual(len(seen), 1)

    def test_fallback_policy_requires_details(self):
        adult = {"is_adult": True, "tags": ["Illustration"]}
        self.assertFalse(booth.item_matches_policy(adult, "exclude", "VRChat"))
        self.assertFalse(booth.item_matches_policy({"is_adult": False}, "only"))
        self.assertFalse(booth.item_matches_policy({}, "exclude"))
        self.assertTrue(booth.item_matches_policy(
            {"is_adult": False, "tags": ["VRChat"]}, "exclude", "VRChat"))

    def test_smart_fallback_is_filtered(self):
        args = booth.build_parser().parse_args(["smart", "test", "--no-ai", "--adult", "exclude", "--json"])
        raw = {"id": 1, "name": "adult", "is_adult": True,
               "tags": [{"name": "Illustration"}]}
        with mock.patch.object(booth, "_merged_search", return_value=([], {"total": 0}, None)), \
                mock.patch.object(smart_search, "find_booth_item_ids", return_value=[1]), \
                mock.patch.object(booth, "fetch_item", return_value=raw), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_smart(args)
        self.assertEqual(json.loads(output.getvalue())["items"], [])

    def test_description_states_and_real_enrichment(self):
        items = [{"id": 1, "name": "Rexouium outfit"}]
        raw = {"id": 1, "description": "Rexouiumには対応していません。"}
        with mock.patch.object(booth, "fetch_item", return_value=raw):
            booth._enrich_details(items)
        self.assertEqual(items[0]["detail_status"], "available")
        self.assertEqual(smart_search.description_status(items[0], ["Rexouium"]), "unsupported")
        self.assertFalse(smart_search.desc_hit(items[0], ["Rexouium"]))
        self.assertEqual(smart_search.description_status(
            {"name": "Rexouium", "detail_status": "unavailable"}, ["Rexouium"]), "title_only")

    def test_pages_from_one_and_three(self):
        for start in (1, 3):
            args = booth.build_parser().parse_args([
                "search", "x", "--page", str(start), "--pages", "4",
                "--limit", "100", "--sort", "new", "--json"])
            seen = []
            def get(url):
                seen.append(url)
                return "item-card", url
            with mock.patch.object(booth, "get_html", side_effect=get), \
                    mock.patch.object(booth, "parse_search_page", return_value={
                        "total": 100, "items": [{"id": 1}], "has_next": True}), \
                    mock.patch.object(booth.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                booth.cmd_search(args)
            pages = [int(urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("page", ["1"])[0])
                     for u in seen]
            self.assertEqual(pages, list(range(start, start + 4)))

    def test_popularity_multiple_pages_use_one_pageable_sort(self):
        args = booth.build_parser().parse_args(["search", "x", "--pages", "2", "--sort", "popularity", "--json"])
        with mock.patch.object(booth, "get_html", return_value=("対象商品 0 件", "url")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_search(args)
        self.assertEqual(args.sort, "new")
        self.assertIn("翻页", json.loads(output.getvalue())["sort_note"])

    def test_canonical_search_url_does_not_duplicate_query_or_default_sort(self):
        args = booth.build_parser().parse_args(["search", "鈴", "--sort", "popularity"])
        url = booth.build_search_url(args)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertNotIn("q", params)
        self.assertNotIn("sort", params)
        self.assertIn("/search/", url)

    def test_new_and_liked_sort_are_explicit_and_category_keeps_query(self):
        for sort in ("new", "liked"):
            args = booth.build_parser().parse_args(["search", "衣装", "--sort", sort, "--page", "2"])
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(booth.build_search_url(args)).query)
            self.assertEqual(params["sort"], [sort])
            self.assertEqual(params["page"], ["2"])
        args = booth.build_parser().parse_args(["search", "衣装", "--category", "3D衣装"])
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(booth.build_search_url(args)).query)
        self.assertEqual(params["q"], ["衣装"])

    def test_zero_result_is_success_but_unknown_structure_is_error(self):
        for page, expected in (("対象商品 0 件", 0), ("broken page", 2)):
            with mock.patch.object(booth, "get_html", return_value=(page, "https://booth.pm/ja/search/x")), \
                    contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()):
                result = booth.main(["search", "x", "--json"])
            self.assertEqual(result, expected)
            if expected == 0:
                self.assertEqual(json.loads(output.getvalue())["count"], 0)

    def test_full_item_text_uses_normalized_tags_and_full_description(self):
        raw = {"id": 1, "name": "x", "price": 1, "shop": {}, "description": "<p>full description</p>",
               "tags": [{"name": "VRChat"}]}
        with mock.patch.object(booth, "fetch_item", return_value=raw), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_item(booth.build_parser().parse_args(["item", "1", "--full"]))
        self.assertIn("#VRChat", output.getvalue())
        self.assertIn("full description", output.getvalue())

    def test_reverse_links_host_and_order(self):
        text = ("https%3A%2F%2Fbooth.pm%2Fja%2Fitems%2F1000001 "
                "https://catalog.invalid/items/9999999 "
                "https://booth.pm/ja/items/2000002 /items/9999998 "
                "https://booth.pm.evil.invalid/items/9999997")
        self.assertEqual(reverse_search._ordered_ids(text), [1000001, 2000002])

    def test_each_redirect_and_retry_is_throttled(self):
        url = "https://booth.pm/ja/items/1"
        for error in (
            urllib.error.HTTPError(url, 302, "Found", {"Location": "/ja/items/2"}, io.BytesIO()),
            urllib.error.HTTPError(url, 503, "Unavailable", {}, io.BytesIO()),
        ):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"ok"
            response.headers = {}
            response.status = 200
            with mock.patch.object(booth, "cache_get", return_value=None), \
                    mock.patch.object(booth._OPENER, "open", side_effect=[error, response]), \
                    mock.patch.object(booth, "_polite_wait") as wait, \
                    mock.patch.object(booth.time, "sleep"):
                self.assertEqual(booth.http_get(url)[0], b"ok")
            self.assertEqual(wait.call_count, 2)

    def test_url_image_failure_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            original = tempfile.NamedTemporaryFile
            def create(*a, **kw):
                return original(*a, **dict(kw, dir=directory))
            with mock.patch.object(booth, "download_image", return_value=b"\xff\xd8fake"), \
                    mock.patch.object(smart_search, "ai_backend", return_value={"base_url":"https://api.invalid/v1", "api_key":"placeholder", "model":"m", "timeout":1}), \
                    mock.patch.object(smart_search.provider_api, "image_capability", return_value={"state":"supported", "model":"m"}), \
                    mock.patch.object(booth.tempfile, "NamedTemporaryFile", side_effect=create), \
                    mock.patch.object(reverse_search, "ascii2d_search", return_value=([], "")):
                with self.assertRaises(booth.BoothError):
                    booth.cmd_imgsearch(booth.build_parser().parse_args([
                        "imgsearch", "https://booth.pximg.net/test.jpg", "--engine", "ascii2d"]))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_evaluation_requires_three_real_unique_candidates(self):
        titles = ["1. A", "2. B", "3. C"]
        for hits in ([], ["1", "1", "1"], ["1", "2", "999"], ["A"]):
            ev = smart_search.validate_evaluation({"verdict": "ok", "hits": hits}, titles)
            self.assertEqual(ev["verdict"], "retry")
        self.assertEqual(smart_search.validate_evaluation(
            {"verdict": "ok", "hits": ["1", "2", "3"]}, titles)["verdict"], "ok")

    def test_ddg_form_is_encoded_once(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b""
        with mock.patch.object(smart_search, "_validated_outbound_url", side_effect=lambda u: u), \
                mock.patch.object(smart_search._AUTH_OPENER, "open", return_value=response) as wire:
            smart_search.ddg_find("尻尾 + 50%")
        form = urllib.parse.parse_qs(wire.call_args.args[0].data.decode())
        self.assertEqual(form["q"], ["尻尾 + 50% booth.pm"])

    def test_web_links_validate_exact_host_and_unwrap_ddg(self):
        urls = ["https://evilbooth.pm/ja/items/1000001",
                "https://booth.pm.evil.invalid/ja/items/1000002",
                "https://evil.invalid/?url=https://booth.pm/ja/items/1000003",
                "https://booth.pm/ja/items/1000004",
                "https://shop.booth.pm/items/1000005",
                "https://[invalid/"]
        self.assertEqual(smart_search.item_ids_from_urls(urls), [1000004, 1000005])
        content = ('<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fbooth.pm%2Fja%2Fitems%2F1000004&amp;rut=x">x</a>'
                   '<a href="https://evilbooth.pm/ja/items/1000001">x</a>')
        self.assertEqual(smart_search.ids_from_ddg_html(content), [1000004])


class TestThreadedCache(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old = (booth.CACHE_PATH, booth._CACHE_CONN, booth._NO_CACHE)
        booth.CACHE_PATH = Path(self.directory.name) / "cache.sqlite3"
        booth._CACHE_CONN = None
        booth.set_cache_enabled(True)

    def tearDown(self):
        if booth._CACHE_CONN:
            booth._CACHE_CONN.close()
        booth.CACHE_PATH, booth._CACHE_CONN, booth._NO_CACHE = self.old
        self.directory.cleanup()

    def test_workers_read_and_write(self):
        booth.cache_put("main", b"x", "main")
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(pool.submit(booth.cache_get, "main", 100).result(), (b"x", "main"))
            pool.submit(booth.cache_put, "worker", b"y", "worker").result()
        self.assertEqual(booth.cache_get("worker", 100), (b"y", "worker"))

    def test_bot_no_cache_is_scoped_to_request(self):
        url = "https://booth.pm/ja/items/1.json"
        raw = {"id": 1, "name": "fresh"}
        booth.cache_put(url, json.dumps({"id": 1, "name": "stale"}).encode(), url)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(raw).encode()
        response.headers = {}
        response.status = 200
        with mock.patch.object(booth._OPENER, "open", return_value=response) as network, \
                mock.patch.object(booth, "_polite_wait"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_bot(booth.build_parser().parse_args([
                "bot", json.dumps({"action": "item", "params": {"id": 1, "no_cache": True}})]))
        self.assertEqual(json.loads(output.getvalue())["data"]["name"], "fresh")
        self.assertTrue(network.called)
        self.assertFalse(booth._NO_CACHE)
