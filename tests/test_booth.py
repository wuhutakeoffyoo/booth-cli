# -*- coding: utf-8 -*-
"""booth-cli 单元测试（纯标准库 unittest，不联网）。

运行: python -m unittest discover -s tests -v
"""

import argparse
import html as html_mod
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import booth  # noqa: E402
import reverse_search  # noqa: E402


class TestUrlGuards(unittest.TestCase):
    def test_assert_allowed_url_ok(self):
        for url in ("https://booth.pm/ja/items/1",
                    "https://cielswap.booth.pm/items/3368697",
                    "https://booth.pm/ja/search/x"):
            self.assertEqual(booth.assert_allowed_url(url), url)

    def test_assert_allowed_url_rejects(self):
        for url in ("http://booth.pm/ja/items/1",          # 非 https
                    "https://evil.com/items/1",            # 陌生主机
                    "https://booth.pm.evil.com/items/1",   # 后缀伪装
                    "ftp://booth.pm/x"):
            with self.assertRaises(booth.BoothError):
                booth.assert_allowed_url(url)


class TestParsers(unittest.TestCase):
    def test_parse_item_id(self):
        self.assertEqual(booth.parse_item_id("3368697"), "3368697")
        self.assertEqual(booth.parse_item_id("https://cielswap.booth.pm/items/3368697"),
                         "3368697")
        with self.assertRaises(booth.BoothError):
            booth.parse_item_id("not-an-id")

    def test_parse_shop_subdomain(self):
        self.assertEqual(booth.parse_shop_subdomain("cielswap"), "cielswap")
        self.assertEqual(booth.parse_shop_subdomain("https://cielswap.booth.pm/"), "cielswap")
        with self.assertRaises(booth.BoothError):
            booth.parse_shop_subdomain("https://evil.com/")

    def test_original_image(self):
        self.assertEqual(
            booth.original_image("https://booth.pximg.net/c/300x300_a2_g5/abc_base_resized.jpg"),
            "https://booth.pximg.net/abc.jpg")
        self.assertIsNone(booth.original_image(None))

    def test_clean_text(self):
        self.assertEqual(booth.clean_text("<p>a<b>c</b></p><script>x</script>  d "),
                         "a c d")

    def test_parse_search_page(self):
        page = (
            "<html>対象商品 1,234 件"
            '<a rel="next" href="?page=2">次へ</a>'
            '<li class="item-card " data-product-id="3368697" '
            'data-product-brand="cielswap" data-product-price="2980">'
            '<p class="item-card__title">'
            '<a href="https://cielswap.booth.pm/items/3368697">Ciel - シエル</a></p>'
            '<a class="item-card__shop-name-anchor" href="https://cielswap.booth.pm/">'
            '<span class="item-card__shop-name">CielSwap</span></a>'
            '<a class="item-card__category-anchor" href="/ja/browse/3D%E3%82%AD%E3%83%A3%E3%83%A9">'
            '3Dキャラクター</a>'
            '<img data-original="https://booth.pximg.net/c/300x300_a2_g5/x_base_resized.jpg">'
            '<img alt="VRChat" src="https://booth.pm/badges/vrchat.png">'
            '<span class="badge adult">R-18</span>'
            "</li></html>")
        res = booth.parse_search_page(page)
        self.assertEqual(res["total"], 1234)
        self.assertTrue(res["has_next"])
        self.assertEqual(len(res["items"]), 1)
        it = res["items"][0]
        self.assertEqual(it["id"], 3368697)
        self.assertEqual(it["price"], 2980)
        self.assertEqual(it["name"], "Ciel - シエル")
        self.assertEqual(it["shop"]["subdomain"], "cielswap")
        self.assertEqual(it["shop"]["name"], "CielSwap")
        self.assertEqual(it["category"], "3Dキャラクター")
        self.assertEqual(it["tags"], ["VRChat"])
        self.assertTrue(it["is_adult"])
        self.assertEqual(it["image"], "https://booth.pximg.net/x.jpg")

    def test_parse_shop_page(self):
        raw = {
            "id": 3368697, "name": "Ciel", "price": 2980,
            "shop_item_url": "https://cielswap.booth.pm/items/3368697",
            "shop": {"name": "CielSwap", "subdomain": "cielswap"},
            "is_adult": True, "is_sold_out": False, "is_vrchat": True,
            "thumbnail_image_urls": ["https://booth.pximg.net/c/300x300_a2_g5/x_base_resized.jpg"],
        }
        page = f'<div data-item="{html_mod.escape(json.dumps(raw), quote=True)}"></div>'
        items = booth.parse_shop_page(page)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["id"], 3368697)
        self.assertTrue(it["is_vrchat"])
        self.assertEqual(it["image"], "https://booth.pximg.net/x.jpg")

    def test_trim_item(self):
        raw = {
            "id": 1, "name": "n", "price": 100,
            "shop": {"name": "s", "subdomain": "sd", "url": "https://sd.booth.pm/"},
            "is_adult": True, "is_sold_out": True, "is_end_of_sale": False,
            "wish_lists_count": 7, "published_at": "2024-01-01",
            "category": {"name": "cat"},
            "tags": [{"name": "VRChat"}],
            "images": [{"original": "https://booth.pximg.net/a_base_resized.jpg"}],
            "variations": [{"field1": "size L", "field2": None, "price": 100,
                            "is_end_of_sale": True}],
            "description": "<p>desc</p>",
        }
        it = booth.trim_item(raw)
        self.assertEqual(it["category"], "cat")
        self.assertEqual(it["tags"], ["VRChat"])
        self.assertEqual(it["images"], ["https://booth.pximg.net/a.jpg"])
        self.assertEqual(it["variations"][0]["name"], "size L")
        self.assertEqual(it["variations"][0]["status"], "end_of_sale")
        self.assertEqual(it["description"], "desc")


class TestReverseSearch(unittest.TestCase):
    def test_ordered_ids_order_and_escapes(self):
        text = ("https://booth.pm/ja/items/3368697 ... https%3A%2F%2Fbooth.pm%2Fitems%2F555001 ... "
                'https:\/\/shop.booth.pm\/ja\/items\/1000001')
        self.assertEqual(reverse_search._ordered_ids(text), [3368697, 555001, 1000001])

    def test_decrypt_bing_signature(self):
        key = reverse_search._BING_SIG_KEY
        seg = "hello-sig"
        cipher = bytes((ord(seg[i]) + 3) ^ ord(key[i % len(key)])
                       for i in range(len(seg)))
        import base64
        raw = f"1|{base64.b64encode(cipher).decode()}|1234567890"
        self.assertEqual(reverse_search._decrypt_bing_signature(raw),
                         f"1|{seg}|1234567890")
        # 非3段格式原样返回
        self.assertEqual(reverse_search._decrypt_bing_signature("plain"), "plain")

    def test_validated_bing_url(self):
        # http / 陌生主机：直接拒绝（不触发 DNS）
        with self.assertRaises(RuntimeError):
            reverse_search._validated_bing_url("http://www.bing.com/images")
        with self.assertRaises(RuntimeError):
            reverse_search._validated_bing_url("https://evil.com/images")
        with self.assertRaises(RuntimeError):
            reverse_search._validated_bing_url("https://www.bing.com:8080/images")
        # 正常 https 主机：DNS 解析到公网地址 → 通过
        ok = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch("socket.getaddrinfo", return_value=ok):
            self.assertTrue(reverse_search._validated_bing_url(
                "https://www.bing.com/images/search?x=1").startswith("https://www.bing.com/"))
        # DNS 解析到私网地址 → 拒绝（SSRF/DNS rebinding 防护）
        private = [(2, 1, 6, "", ("192.168.1.1", 443))]
        with mock.patch("socket.getaddrinfo", return_value=private):
            with self.assertRaises(RuntimeError):
                reverse_search._validated_bing_url("https://www.bing.com/images")

    def test_multipart_shape(self):
        body, ctype = reverse_search._multipart([("cbir", "sbi"), ("imageBin", "QUJD")])
        text = body.decode()
        self.assertIn('name="cbir"', text)
        self.assertIn("QUJD", text)
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))


class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        booth.CACHE_PATH = Path(self._tmp.name) / "c.sqlite3"
        booth._CACHE_CONN = None
        booth.set_cache_enabled(True)

    def tearDown(self):
        if isinstance(booth._CACHE_CONN, object) and booth._CACHE_CONN not in (None, False):
            try:
                booth._CACHE_CONN.close()
            except Exception:
                pass
        booth._CACHE_CONN = None
        self._tmp.cleanup()
        booth.set_cache_enabled(True)

    def test_put_get_roundtrip(self):
        booth.cache_put("u", b"x", "fu")
        self.assertEqual(booth.cache_get("u", 600), (b"x", "fu"))

    def test_ttl_zero_disables_read(self):
        booth.cache_put("u", b"x", "fu")
        self.assertIsNone(booth.cache_get("u", 0))

    def test_no_cache_flag(self):
        booth.set_cache_enabled(False)
        booth.cache_put("u", b"x", "fu")
        self.assertIsNone(booth.cache_get("u", 600))


class TestRetryDelay(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(booth._retry_delay(0, {"Retry-After": "2"}), 2.0)

    def test_retry_after_http_date(self):
        future = time.time() + 30
        d = booth._retry_delay(0, {"Retry-After": time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future))})
        self.assertTrue(25 <= d <= 45)

    def test_exponential_with_jitter(self):
        d = booth._retry_delay(2)
        self.assertTrue(1.5 <= d <= 15.0 + 1.0 + 0.1)

    def test_garbage_falls_back(self):
        d = booth._retry_delay(1, {"Retry-After": "soon"})
        self.assertTrue(1.5 <= d <= 15.0 + 1.0 + 0.1)


class TestBotHook(unittest.TestCase):
    def _run_bot(self, payload):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            booth.cmd_bot(argparse.Namespace(payload=payload))
        return json.loads(buf.getvalue())

    def test_params_to_argv_search(self):
        argv = booth.bot_params_to_argv("search", {
            "query": ["VRChat", "アバター"], "sort": "popularity",
            "tag": ["VRChat", "衣装"], "vrc": True, "min_price": 100,
            "no_cache": True, "limit": 5})
        self.assertEqual(argv, ["search", "VRChat", "アバター", "--sort", "popularity",
                                "--tag", "VRChat", "--tag", "衣装", "--vrc",
                                "--min-price", "100", "--no-cache", "--limit", "5"])

    def test_params_to_argv_scalar_positional(self):
        argv = booth.bot_params_to_argv("item", {"id": 3368697, "full": True})
        self.assertEqual(argv, ["item", "3368697", "--full"])

    def test_params_to_argv_accepts_parse(self):
        # 生成的 argv 必须能被正式 parser 接受（校验旗标拼写）
        for action, params in (("search", {"query": "x", "limit": 3}),
                               ("item", {"id": 1}),
                               ("shop", {"shop": "mukumi", "pages": 2}),
                               ("imgsearch", {"image": "a.jpg", "headless": True,
                                              "wait_s": 10}),
                               ("smart", {"query": "尾巴", "limit": 3})):
            ns = booth.build_parser().parse_args(
                booth.bot_params_to_argv(action, params) + ["--json"])
            self.assertTrue(hasattr(ns, "func"))

    def test_envelope_ok_and_errors(self):
        out = self._run_bot('{"action":"nope"}')
        self.assertFalse(out["ok"])
        self.assertIn("未知 action", out["error"])
        out = self._run_bot("not-json")
        self.assertFalse(out["ok"])
        out = self._run_bot('{"action":"item"}')  # 缺必填位置参数
        self.assertFalse(out["ok"])
        self.assertIn("required", out["error"])

    def test_envelope_version(self):
        out = self._run_bot('{"action":"version"}')
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["version"], booth.__version__)


class TestPoliteWait(unittest.TestCase):
    def test_global_interval(self):
        import time as _time
        booth._LAST_REQ_TS = 0.0
        t0 = _time.monotonic()
        booth._polite_wait()
        booth._polite_wait()
        # 第二次调用应等待到间隔满足（至少 sleep 过）
        self.assertLessEqual(booth.MIN_REQUEST_INTERVAL - (_time.monotonic() - t0), 0.6)


if __name__ == "__main__":
    unittest.main()
