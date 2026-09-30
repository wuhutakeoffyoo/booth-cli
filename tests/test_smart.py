# -*- coding: utf-8 -*-
"""smart 搜索（VRC 对口策略）单测：需求解析/变体/描述核实/网络检索解析，不联网。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import booth  # noqa: E402
import smart_search  # noqa: E402


class TestLooksChinese(unittest.TestCase):
    def test_cases(self):
        self.assertTrue(smart_search.looks_chinese("猫娘女仆装"))
        self.assertTrue(smart_search.looks_chinese("信浓 原创VRChat模型"))
        self.assertTrue(smart_search.looks_chinese("手枪道具 3Dギミック"))  # 简体特有形优先
        self.assertFalse(smart_search.looks_chinese("シエル 3Dモデル"))      # 假名=日语
        self.assertFalse(smart_search.looks_chinese("Ciel avatar"))          # 纯英文


class TestParseTranslation(unittest.TestCase):
    def test_json_with_desc(self):
        kws, dkws = smart_search.parse_translation(
            '```json\n{"keywords": ["ショコラドレス", "衣装"], '
            '"desc_keywords": ["Rexouium", "対応素体"]}\n```')
        self.assertEqual(kws, ["ショコラドレス", "衣装"])
        self.assertEqual(dkws, ["Rexouium", "対応素体"])

    def test_json_without_desc(self):
        kws, dkws = smart_search.parse_translation('{"keywords": ["尻尾"]}')
        self.assertEqual(kws, ["尻尾"])
        self.assertEqual(dkws, [])

    def test_fallback_no_json(self):
        kws, dkws = smart_search.parse_translation("尻尾\nしっぽ\nテイル")
        self.assertIn("尻尾", kws)
        self.assertEqual(dkws, [])

    def test_desc_capped_at_3(self):
        _, dkws = smart_search.parse_translation(
            '{"keywords": ["x"], "desc_keywords": ["a", "b", "c", "d", "e"]}')
        self.assertEqual(len(dkws), 3)

    def test_reasoning_prose_filtered(self):
        kws, _ = smart_search.parse_translation(
            '{"keywords": ["尻尾", "I can see a tail in the image, likely a fox tail accessory"]}')



class TestParseRecall(unittest.TestCase):
    def test_json(self):
        out = smart_search.parse_recall(
            '{"candidates": ["ショコラドレス", "I can see it is a dress"]}')
        self.assertEqual(out, ["ショコラドレス"])


class TestVariantsAndTerms(unittest.TestCase):
    def test_build_search_terms(self):
        terms = smart_search.build_search_terms(["グリモワール 衣装", "猫耳", "x"])
        self.assertIn("グリモワール", terms)
        self.assertIn("衣装", terms)
        self.assertIn("グリモワール 衣装", terms)  # 连写整词保留
        self.assertNotIn("x", terms)              # 单字丢弃

    def test_terms_capped_6(self):
        out = smart_search.build_search_terms([f"词{i}" for i in range(10)])
        self.assertEqual(len(out), 6)

    def test_expand_reading_variants(self):
        # pykakasi 为必装依赖，测试不跳过
        out = smart_search.expand_reading_variants(["信濃 3Dモデル"])
        self.assertEqual(out[0], "信濃 3Dモデル")
        self.assertIn("しなの 3Dモデル", out[1:])
        self.assertEqual(smart_search.expand_reading_variants(["ツインテール"]),
                         ["ツインテール"])


class TestDescRank(unittest.TestCase):
    def test_desc_hit(self):
        self.assertTrue(smart_search.desc_hit(
            {"name": "ドレス", "_desc": "対応素体：Rexouium"}, ["Rexouium"]))
        self.assertTrue(smart_search.desc_hit(
            {"name": "Rexouium outfit", "_desc": ""}, ["rexouium"]))
        self.assertFalse(smart_search.desc_hit({"name": "x", "_desc": "y"}, []))

    def test_desc_boost_three_tier(self):
        items = [
            {"id": 1, "name": "衣装 A"},
            {"id": 2, "name": "B 対応衣装", "_desc": "対応素体: Rexouium"},
            {"id": 3, "name": "Rexouium 衣装"},
            {"id": 4, "name": "无关 C"},
        ]
        out = smart_search.desc_boost(items, ["衣装", "Rexouium"], ["Rexouium"])
        self.assertEqual([it["id"] for it in out], [2, 3, 1, 4])


    def test_parse_plan(self):
        kws, dkws, translated = smart_search.parse_plan(
            '{"keywords": ["鈴", "ベル"], "desc_keywords": ["Rexouium"], '
            '"translated": true}')
        self.assertEqual(kws, ["鈴", "ベル"])
        self.assertEqual(dkws, ["Rexouium"])
        self.assertTrue(translated)
        kws, dkws, translated = smart_search.parse_plan(
            '{"keywords": ["シエル"], "translated": false}')
        self.assertEqual(kws, ["シエル"])
        self.assertFalse(translated)

    def test_parse_evaluation(self):
        ev = smart_search.parse_evaluation(
            '{"verdict": "retry", "reason": "返回的是鸟居", "keywords": ["鈴"]}')
        self.assertEqual(ev["verdict"], "retry")
        self.assertEqual(ev["keywords"], ["鈴"])
        with self.assertRaises(smart_search.AiError):
            smart_search.parse_evaluation("no json")


class TestAiBackendGuard(unittest.TestCase):
    def test_guard_api_base(self):
        for bad in ("http://opencode.ai/v1", "https://localhost/v1",
                    "https://127.0.0.1/v1", "https://192.168.1.1/v1"):
            with self.assertRaises(smart_search.AiError):
                smart_search.guard_api_base(bad)
        self.assertEqual(smart_search.guard_api_base("https://opencode.ai/zen/go/v1"),
                         "https://opencode.ai/zen/go/v1")

    def test_backend_requires_key(self):
        import os
        old = os.environ.pop("VISION_API_KEY", None)
        try:
            self.assertIsNone(smart_search.ai_backend())
        finally:
            if old is not None:
                os.environ["VISION_API_KEY"] = old


class TestWebfindParse(unittest.TestCase):
    def test_ids_from_urls(self):
        urls = ["https://booth.pm/ja/items/3368697",
                "https://shop.booth.pm/ja/items/1000001",
                "https://booth.pm/ja/items/3368697"]
        self.assertEqual(smart_search.item_ids_from_urls(urls), [3368697, 1000001])

    def test_ddg_html_parse(self):
        html = ('<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fbooth.pm%2Fja%2Fitems%2F3368697">a</a>'
                '<a href="https://cielswap.booth.pm/ja/items/1000001">b</a>')
        self.assertEqual(smart_search.ids_from_ddg_html(html), [3368697, 1000001])

    def test_outbound_guard(self):
        with self.assertRaises(ValueError):
            smart_search._validated_outbound_url("http://html.duckduckgo.com/html/")
        with self.assertRaises(ValueError):
            smart_search._validated_outbound_url("https://evil.com/html/")


class TestSearchDefaults(unittest.TestCase):
    def _ns(self, argv):
        return booth.build_parser().parse_args(argv)

    def _url(self, argv):
        ns = self._ns(argv)
        return booth.build_search_url(ns)

    def test_vrc_tag_default_on(self):
        self.assertIn("tags%5B%5D=VRChat", self._url(["search", "猫"]))

    def test_no_vrc_disables(self):
        self.assertNotIn("VRChat", self._url(["search", "猫", "--no-vrc"]))

    def test_tag_dedup(self):
        url = self._url(["search", "猫", "--tag", "VRChat"])
        self.assertEqual(url.count("VRChat"), 1)

    def test_effective_sort(self):
        self.assertEqual(booth.effective_sort("popularity", 2), ("new", "（翻页按新着排序）"))
        self.assertEqual(booth.effective_sort("popularity", 1), ("popularity", ""))
        self.assertEqual(booth.effective_sort("new", 2), ("new", ""))

    def test_smart_envelope_argv_parses(self):
        argv = booth.bot_params_to_argv("smart", {"query": "尾巴", "limit": 3,
                                                  "no_ai": True})
        ns = booth.build_parser().parse_args(argv + ["--json"])
        self.assertEqual(ns.query, ["尾巴"])
        self.assertEqual(ns.limit, 3)
        self.assertTrue(ns.no_ai)


if __name__ == "__main__":
    unittest.main()
