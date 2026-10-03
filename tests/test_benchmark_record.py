"""The benchmark must preserve evidence without converting model labels to grades."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "benchmarks/run100_probe.py"
spec = importlib.util.spec_from_file_location("record_probe", path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class TestBenchmarkRecord(unittest.TestCase):
    def test_full_title_evidence_and_manual_unknown_survive_recording(self):
        result = {"header": "long header " * 100, "entries": [{"id": 1, "name": "商品" * 100,
            "relevance_status": "related", "relevance_evidence": {"field": "name", "quote": "商品"}}],
            "metrics": {"wire_requests": 12}, "quality": {"related": 1}}
        record = probe.result_record("query", result, 1.23456, ["second round"])
        self.assertEqual(record["items"][0]["name"], result["entries"][0]["name"])
        self.assertEqual(record["header"], result["header"])
        self.assertEqual(record["items"][0]["relevance_evidence"]["quote"], "商品")
        self.assertIsNone(record["items"][0]["manual_relevance"])
        self.assertEqual(record["metrics"]["wire_requests"], 12)
        self.assertEqual(record["notices"], ["second round"])

    def test_text_only_no_results_is_distinguishable_from_an_exception(self):
        record = probe.result_record("query", {"text": "no results", "entries": []}, 0, [])
        self.assertEqual(record["items"], [])
        self.assertEqual(record["header"], "no results")
        self.assertNotIn("error", record)


if __name__ == "__main__":
    unittest.main()
