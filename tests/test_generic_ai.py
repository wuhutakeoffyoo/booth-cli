"""Offline CLI admission and configurable API/search integration checks."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

import booth
import provider_api as api
import smart_search as smart


class TestGenericCli(unittest.TestCase):
    def setUp(self):
        api._CACHE.clear()
        patch = mock.patch.dict(os.environ, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_new_two_field_config_and_legacy_aliases(self):
        self.assertIsNone(smart.ai_backend())
        with mock.patch.dict(os.environ, {"AI_API_KEY":"new", "AI_BASE_URL":"https://new.invalid/v1",
                                          "VISION_MODEL":"stale-provider-model"}):
            self.assertEqual(smart.ai_backend()["model"], "")
        with mock.patch.dict(os.environ, {"VISION_API_KEY":"old", "VISION_BASE_URL":"https://old.invalid/v1",
                                          "VISION_MODEL":"old-model", "AI_API_KEY":"", "AI_BASE_URL":""}):
            self.assertEqual(smart.ai_backend()["api_key"], "old")
        with mock.patch.dict(os.environ, {"RUN_PROFILE":"benchmark", "AI_API_KEY":"test",
                                          "AI_BASE_URL":"https://new.invalid/v1"}):
            self.assertIsNone(smart.ai_backend())

    def test_every_imgsearch_entry_blocks_before_any_user_io(self):
        for state in ("unsupported", "unknown"):
            for source in ("https://booth.pximg.net/private.jpg", "private-local.png"):
                with mock.patch.dict(os.environ, {"AI_API_KEY":"test", "AI_BASE_URL":"https://api.invalid/v1", "AI_MODEL":"text"}), \
                        mock.patch.object(api, "image_capability", return_value={
                            "state":state, "model":"text", "reason":"检测未通过"}), \
                        mock.patch.object(booth, "download_image") as download, \
                        mock.patch.object(booth.Path, "is_file") as read, \
                        mock.patch.object(booth, "_run_imgsearch") as engine:
                    args = booth.build_parser().parse_args(["imgsearch", source])
                    with self.assertRaisesRegex(booth.BoothError, "请使用文字搜索"):
                        booth.cmd_imgsearch(args)
                    download.assert_not_called()
                    read.assert_not_called()
                    engine.assert_not_called()

    def test_unconfigured_and_benchmark_images_have_zero_outbound(self):
        for env in ({}, {"RUN_PROFILE":"benchmark", "AI_API_KEY":"test", "AI_BASE_URL":"https://api.invalid/v1"}):
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(api, "request_json") as wire, \
                    mock.patch.object(booth, "_run_imgsearch") as engine:
                with self.assertRaisesRegex(booth.BoothError, "图片功能未启用"):
                    booth.cmd_imgsearch(booth.build_parser().parse_args(["imgsearch", "a.jpg"]))
                wire.assert_not_called()
                engine.assert_not_called()

    def test_configured_multimodal_fallback_can_admit(self):
        with mock.patch.dict(os.environ, {
                "AI_API_KEY":"primary", "AI_BASE_URL":"https://primary.invalid/v1", "AI_MODEL":"text",
                "AI_FALLBACK_API_KEY":"fallback", "AI_FALLBACK_BASE_URL":"https://fallback.invalid/v1", "AI_FALLBACK_MODEL":"multi"}), \
                mock.patch.object(api, "image_capability", side_effect=[
                    {"state":"unsupported", "model":"text", "reason":"纯文字"},
                    {"state":"supported", "model":"multi"}]) as capability, \
                mock.patch.object(booth.Path, "is_file", return_value=True), \
                mock.patch.object(booth, "_run_imgsearch") as engine:
            booth.cmd_imgsearch(booth.build_parser().parse_args(["imgsearch", "a.jpg"]))
        self.assertEqual(capability.call_count, 2)
        engine.assert_called_once()

    def test_bot_image_envelope_explains_restriction(self):
        with mock.patch.object(api, "request_json") as wire, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            booth.cmd_bot(booth.build_parser().parse_args(["bot", '{"action":"imgsearch","image":"private.png"}']))
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("文字搜索", result["error"])
        wire.assert_not_called()

    def test_auto_text_plan_runs_through_each_protocol(self):
        for base, response, text_path in (
            ("https://api.invalid/v1", {"choices":[{"message":{"content":'{"keywords":["鈴"],"translated":true}'}}]}, "messages"),
            ("https://api.invalid/v1/messages", {"content":[{"type":"text","text":'{"keywords":["鈴"],"translated":true}'}]}, "messages"),
            ("https://api.invalid/v1beta", {"candidates":[{"content":{"parts":[{"text":'{"keywords":["鈴"],"translated":true}'}]}}]}, "contents"),
        ):
            api._CACHE.clear()
            seen = []
            def open_wire(req, **kwargs):
                seen.append(req)
                return io.BytesIO(json.dumps(response).encode())
            with mock.patch.object(api, "request_json", return_value={
                    "data":[{"id":"generic"}], "models":[{"name":"models/generic", "supportedGenerationMethods":["generateContent"]}]}), \
                    mock.patch.object(smart._AUTH_OPENER, "open", side_effect=open_wire):
                self.assertEqual(smart.plan_search("铃铛", base_url=base, api_key="test", model="")[0], ["鈴"])
            payload = json.loads(seen[0].data)
            self.assertIn(text_path, payload)
            self.assertNotIn("x-opencode-session", {name.lower():value for name, value in seen[0].header_items()})

    def test_custom_exa_url_and_booth_result_filter(self):
        requests = []
        def open_wire(req, **kwargs):
            requests.append(req)
            return io.BytesIO(json.dumps({"results":[
                {"url":"https://evilbooth.pm/items/1"}, {"url":"https://booth.pm/ja/items/2"}]}).encode())
        with mock.patch.object(api.socket, "getaddrinfo", return_value=[(2,1,6,"",("93.184.216.34",443))]), \
                mock.patch.object(smart._AUTH_OPENER, "open", side_effect=open_wire):
            self.assertEqual(smart.exa_find("鈴", "test-key", base_url="https://search.invalid/api"), [2])
        self.assertEqual(requests[0].full_url, "https://search.invalid/api/search")
        self.assertEqual(requests[0].get_header("X-api-key"), "test-key")
        self.assertEqual(json.loads(requests[0].data)["includeDomains"], ["booth.pm"])


if __name__ == "__main__":
    unittest.main()
