"""Exercise the real smart command with a wrong AI plan and bounded retries."""
import contextlib
import io
import json
import unittest
from unittest import mock

import booth
import smart_search


class TestSmartTargetFlow(unittest.TestCase):
    def run_search(self, evaluate):
        original = dict(id=1, name="みかんバード", detail_status="available", _desc="original source")
        noise = dict(id=2, name="AvatarPoseSystem", detail_status="available")
        args = booth.build_parser().parse_args(["smart", "みかんバード", "--no-webfind", "--json"])
        with mock.patch.object(smart_search, "ai_backend", return_value={"model": "offline"}), \
                mock.patch.object(smart_search, "plan_search", return_value=(["VRChat", "アバター"], [], False)), \
                mock.patch.object(booth, "_merged_search", side_effect=[([noise, original], {"total": 2}, None),
                                                                    ([noise], {"total": 1}, None)]) as search, \
                mock.patch.object(booth, "_enrich_details"), \
                mock.patch.object(smart_search, "evaluate_results", side_effect=evaluate), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            booth.cmd_smart(args)
        return json.loads(output.getvalue()), search

    def test_original_axis_and_candidate_survive_second_round(self):
        result, search = self.run_search([{"verdict": "retry", "keywords": ["VRChat", "オレンジ鳥"]},
                                          {"verdict": "retry", "keywords": []}])
        self.assertEqual(search.call_args_list[0].args[0], ["みかんバード"])
        self.assertEqual(search.call_args_list[1].args[0], ["オレンジ鳥", "オレンジとり"])
        self.assertEqual([it["id"] for it in result["items"]], [1])
        self.assertEqual(result["count"], 1)
        self.assertIn("みかんバード", result["keywords"])

    def test_evaluator_outage_keeps_exact_target_with_unverified_notice(self):
        result, search = self.run_search(RuntimeError("offline evaluator outage"))
        self.assertEqual(search.call_count, 1)
        self.assertEqual([it["id"] for it in result["items"]], [1])
        self.assertEqual(result["items"][0]["relevance_status"], "unknown")
        self.assertIn("尚未核实", result["eval_note"])


if __name__ == "__main__":
    unittest.main()
