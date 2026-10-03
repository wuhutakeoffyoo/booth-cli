"""The real caller-led command path: BOOTH sources only, even with inherited keys."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_workflow
import booth
import provider_api
import smart_search
import workflow_client


class TestCallerWorkflow(unittest.TestCase):
    def call(self, params, search=None, detail=None):
        request = json.dumps({"action": "workflow", "params": params}, ensure_ascii=False)
        with mock.patch.dict(os.environ, {"AI_API_KEY": "inherited", "AI_BASE_URL": "https://ai.invalid/v1",
                "AI_FALLBACK_API_KEY": "fallback", "AI_FALLBACK_BASE_URL": "https://fallback.invalid/v1",
                "SEARCH_BASE_URL": "https://search.invalid/find"}), \
                mock.patch.object(smart_search, "ai_backend", side_effect=AssertionError("hidden AI discovery")), \
                mock.patch.object(provider_api, "request_json", side_effect=AssertionError("hidden API call")), \
                mock.patch.object(smart_search._AUTH_OPENER, "open", side_effect=AssertionError("hidden model/search request")), \
                mock.patch.object(booth, "_search_once", side_effect=search or (lambda term, args: {"total": 0, "has_next": False, "items": []})) as lookup, \
                mock.patch.object(booth, "fetch_item", side_effect=detail or (lambda iid, lang: {"id": int(iid), "name": "candidate", "description": "source"})) as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_bot(booth.build_parser().parse_args(["bot", request]))
        return json.loads(output.getvalue()), lookup, fetch

    def test_exact_caller_axes_and_source_metadata_no_internal_ai(self):
        original = "https://booth.pximg.net/exact_base_resized.jpg?variant=1"
        def search(term, args):
            self.assertEqual(args.adult, "exclude")
            return {"total": 8, "has_next": term == "桔梗 衣装", "items": [{"id": 1, "name": "桔梗衣装", "shop": {}, "is_adult": False}]}
        def detail(iid, lang):
            return {"id": 1, "description": "<p>桔梗には非対応です。</p>", "images": [{"original": original, "resized": "https://booth.pximg.net/thumb.jpg"}], "variations": [{"name": "衣装のみ", "price": 0}], "wish_lists_count": 0}
        result, lookup, fetch = self.call({"query": "给桔梗找衣装", "keyword": ["桔梗 衣装", "Kikyo outfit"], "require_term": ["桔梗"], "adult": "exclude"}, search, detail)
        self.assertTrue(result["ok"], result)
        data = result["data"]
        self.assertEqual(data["keywords"], ["桔梗 衣装", "Kikyo outfit"])
        self.assertCountEqual([call.args[0] for call in lookup.call_args_list], ["桔梗 衣装", "Kikyo outfit"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((data["ai_execution"], data["ai_calls"], data["source_trust"]), ("caller", 0, "untrusted_data"))
        self.assertTrue(data["has_next"])
        item = data["items"][0]
        self.assertEqual(item["original_images"], [original])
        self.assertEqual(item["variations"][0]["name"], "衣装のみ")
        self.assertEqual(item["wish_lists_count"], 0)
        self.assertIn("非対応", item["description"])
        self.assertEqual(item["compatibility_status"], "unknown")
        self.assertEqual(item["relevance_status"], "unknown")
        self.assertEqual(len(item["source_hash"]), 64)

    def test_caller_retains_unassessed_pool_and_bounds_details(self):
        def search(term, args):
            return {"total": 40, "items": [{"id": i, "name": "source"} for i in range(1, 41)]}
        result, _, fetch = self.call({"query": "需求", "keyword": ["term"], "limit": 4}, search)
        self.assertEqual(result["data"]["candidate_count"], 40)
        self.assertEqual(result["data"]["count"], 4)
        self.assertEqual(fetch.call_count, 4)

    def test_partial_empty_search_is_not_claimed_as_complete_absence(self):
        def search(term, args):
            if term == "fail":
                raise booth.BoothError("offline")
            return {"total": 0, "items": []}
        result, _, _ = self.call({"query": "target", "keyword": ["fail", "empty"]}, search)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["warnings"], ["search_incomplete"])
        result, _, _ = self.call({"query": "target", "keyword": ["fail"]}, search)
        self.assertFalse(result["ok"])

    def test_missing_detail_is_visible_and_not_fabricated(self):
        result, _, _ = self.call({"query": "target"}, lambda t, a: {"total": 1, "items": [{"id": 1, "name": "target"}]}, lambda i, l: (_ for _ in ()).throw(booth.BoothError("offline")))
        item = result["data"]["items"][0]
        self.assertEqual(item["detail_status"], "unavailable")
        self.assertEqual(item["description"], "")
        self.assertIn("detail_unavailable", result["data"]["warnings"])

    def test_description_excerpt_and_truncation_preserve_full_source_hash(self):
        def search(term, args):
            return {"total": 1, "items": [{"id": 1, "name": "target"}]}
        detail = lambda i, l: {"id": 1, "description": "x" * 500 + "桔梗には非対応"}
        short, _, _ = self.call({"query": "target", "require_term": ["桔梗"], "desc_len": 10}, search, detail)
        full, _, _ = self.call({"query": "target", "desc_len": -1}, search, detail)
        item = short["data"]["items"][0]
        self.assertTrue(item["description_truncated"])
        self.assertIn("非対応", item["source_excerpt"])
        self.assertEqual(item["source_hash"], full["data"]["items"][0]["source_hash"])
        self.assertFalse(full["data"]["items"][0]["description_truncated"])

    def test_malformed_json_plan_fails_before_search(self):
        for params in ({"query": "x", "keyword": [None]}, {"query": "x", "keyword": []},
                       {"query": "x", "keyword": ["a"] * 7}, {"query": "x", "limit": 0},
                       {"query": "x", "limit": True}, {"query": "x", "delegate_ai": True},
                       {"query": "x", "no_vrc": "false"}, {"query": "x", "require_term": [3]},
                       {"query": "x", "desc_len": 0}):
            with self.subTest(params=params):
                result, lookup, fetch = self.call(params)
                self.assertFalse(result["ok"], result)
                lookup.assert_not_called()
                fetch.assert_not_called()

    def test_cli_schema_and_invalid_input_need_no_optional_dependencies(self):
        root = str(Path(booth.__file__).parent)
        code = "import sys, importlib.abc; sys.path.insert(0, " + repr(root) + ")\n"
        code += "class NoAI(importlib.abc.MetaPathFinder):\n def find_spec(self, fullname, *args):\n  if fullname in ('smart_search', 'provider_api', 'search_api', 'pykakasi'): raise AssertionError('optional import: '+fullname)\n"
        code += "sys.meta_path.insert(0, NoAI()); import booth; raise SystemExit(booth.main(sys.argv[1:]))\n"
        for args in (["workflow", "--schema"], ["bot", '{"action":"workflow","params":{"schema":true}}']):
            proc = subprocess.run([sys.executable, "-I", "-c", code, *args], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual((payload.get("data") or payload)["input_schema"], agent_workflow.contract()["input_schema"])
        proc = subprocess.run([sys.executable, "-I", "-c", code, "workflow", "target", "--limit", "0"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_default_smart_never_discovers_or_calls_a_model(self):
        args = booth.build_parser().parse_args(["smart", "鈴", "--no-webfind", "--json"])
        with mock.patch.dict(os.environ, {"AI_API_KEY": "inherited", "AI_BASE_URL": "https://ai.invalid/v1"}), \
                mock.patch.object(smart_search, "ai_backend", side_effect=AssertionError("implicit model")) as discover, \
                mock.patch.object(smart_search, "plan_search") as plan, \
                mock.patch.object(smart_search, "evaluate_results") as evaluate, \
                mock.patch.object(booth, "_search_once", return_value={"total": 0, "items": []}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_smart(args)
        self.assertEqual(json.loads(output.getvalue())["mode"], "direct")
        discover.assert_not_called()
        plan.assert_not_called()
        evaluate.assert_not_called()

    def test_default_image_and_mistyped_opt_in_do_not_probe_or_read(self):
        for params in ({"image": "private.png"}, {"image": "private.png", "delegate_ai": "false"}):
            with mock.patch.object(provider_api, "image_capability") as probe, \
                    mock.patch.object(booth, "download_image") as download, \
                    mock.patch.object(booth.Path, "is_file") as read, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                booth.cmd_bot(booth.build_parser().parse_args(["bot", json.dumps({"action": "imgsearch", "params": params})]))
            self.assertFalse(json.loads(output.getvalue())["ok"])
            probe.assert_not_called()
            download.assert_not_called()
            read.assert_not_called()


class TestWorkflowClient(unittest.TestCase):
    def test_real_stdio_adapter_needs_only_current_ai_and_stdlib(self):
        root = str(Path(booth.__file__).parent)
        code = "import sys, importlib.abc; sys.path.insert(0, " + repr(root) + ")\n"
        code += "class NoAI(importlib.abc.MetaPathFinder):\n def find_spec(self, fullname, *args):\n  if fullname in ('smart_search', 'provider_api', 'pykakasi'): raise AssertionError('unexpected AI dependency')\n"
        code += "sys.meta_path.insert(0, NoAI()); import booth\n"
        code += "booth._search_once=lambda term,args: {'total':1,'items':[{'id':1,'name':term}]}\n"
        code += "booth.fetch_item=lambda iid,lang: {'id':1,'description':'桔梗に対応。別途素体が必要。'}\n"
        code += "raise SystemExit(booth.main())\n"
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "offline_booth.py"
            shim.write_text(code, encoding="utf-8")
            with mock.patch.dict(os.environ, {"AI_API_KEY": "inherited", "AI_BASE_URL": "https://ai.invalid/v1"}):
                data = workflow_client.search({"query": "给桔梗找衣装", "keyword": ["桔梗 衣装"]}, cli_path=shim)
        self.assertEqual(data["ai_calls"], 0)
        self.assertEqual(data["items"][0]["name"], "桔梗 衣装")
        self.assertIn("別途素体", data["items"][0]["description"])
        self.assertEqual(data["items"][0]["compatibility_status"], "unknown")

    def test_tool_definition_is_protocol_neutral_and_requires_no_key(self):
        definition = workflow_client.tool_definition()
        self.assertEqual(definition["input_schema"], agent_workflow.contract()["input_schema"])
        self.assertNotIn("api_key", definition["input_schema"]["properties"])
        self.assertNotIn("context", definition["input_schema"]["properties"])

    def test_adapter_uses_stdin_not_shell_and_keeps_shared_context_out_of_model_args(self):
        data = {"ai_execution": "caller", "ai_calls": 0, "items": []}
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": data, "request_budget": {"wire_count": 1}}), "")
        context = {"request_id": "trusted", "max_requests": 12}
        with mock.patch.object(workflow_client.subprocess, "run", return_value=response) as call:
            result = workflow_client.search({"query": "target", "keyword": ["a\"; echo b"]}, context=context)
        self.assertNotIn("target", call.call_args.args[0])
        self.assertIs(call.call_args.kwargs["shell"], False)
        self.assertEqual(json.loads(call.call_args.kwargs["input"])["context"], context)
        self.assertEqual(result["request_budget"]["wire_count"], 1)

    def test_adapter_failure_never_returns_a_success_or_raw_stderr(self):
        for response in (subprocess.CompletedProcess([], 0, "not JSON", "private stderr"),
                         subprocess.CompletedProcess([], 0, '{"ok":true,"data":{}}', "private stderr"),
                         subprocess.CompletedProcess([], 2, "", "private stderr")):
            with mock.patch.object(workflow_client.subprocess, "run", return_value=response), self.assertRaises(workflow_client.WorkflowError) as error:
                workflow_client.search({"query": "x"})
            self.assertNotIn("private stderr", str(error.exception))
        with mock.patch.object(workflow_client.subprocess, "run", side_effect=subprocess.TimeoutExpired("booth", 1)), self.assertRaisesRegex(workflow_client.WorkflowError, "超时"):
            workflow_client.search({"query": "x"})


if __name__ == "__main__":
    unittest.main()
