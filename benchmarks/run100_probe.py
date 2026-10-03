"""Record bounded text-search evidence; run from the bot checkout's virtualenv.

python run100_probe.py words.json output_dir [start] [end]
AI is off unless BENCHMARK_ALLOW_AI=true is explicitly supplied. Use independent
quota for large runs. Manual labels remain separate from the model's assessment.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def git_revision(root):
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def result_record(query, result, elapsed, notices):
    fields = ("id", "name", "url", "category", "tags", "detail_status",
              "relevance_status", "relevance_evidence", "via")
    return {"query": query, "header": result.get("header") or result.get("text") or "",
            "elapsed_seconds": round(elapsed, 3), "notices": notices,
            "metrics": result.get("metrics") or {}, "quality": result.get("quality") or {},
            "items": [dict({key: item.get(key) for key in fields}, manual_relevance=None)
                      for item in (result.get("entries") or [])[:6]]}


async def main():
    words_file, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    raw_words = words_file.read_bytes()
    words = json.loads(raw_words)["words"]
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    end = int(sys.argv[4]) if len(sys.argv) > 4 else len(words)
    if not 0 <= start <= end <= len(words) or len(set(words)) != len(words):
        raise SystemExit("Invalid range or duplicate queries")
    os.environ["RUN_PROFILE"] = "benchmark"
    sys.path.insert(0, str(Path.cwd() / "src/plugins"))
    import nonebot
    nonebot.init(log_level="ERROR")
    import booth_search as bot
    from booth_search import booth_client, qcache
    cfg = bot.plugin_config
    parent = booth_client.call_booth("version", cli_path=cfg.booth_cli_path)
    identity = {"dataset_sha256": hashlib.sha256(raw_words).hexdigest(),
                "bot_revision": git_revision(Path.cwd()),
                "bot_semantic_fingerprint": qcache.semantic_fingerprint(cfg.booth_cli_path),
                "configuration_fingerprint": qcache.configuration_key(cfg),
                "cli_version": parent["version"],
                "cli_semantic_fingerprint": parent.get("semantic_fingerprint"),
                "cache_version": qcache.CACHE_VERSION, "profile": cfg.run_profile,
                "ai_enabled": bool(bot._ai_backend()[0]), "requested_model": cfg.vision_model,
                "request_budget": cfg.request_budget, "retry_request_budget": cfg.retry_request_budget,
                "query_timeout": cfg.query_timeout, "range": [start, end]}
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"probe_{start}_{end}.json"
    manifest_path = out_dir / f"manifest_{start}_{end}.json"
    if out.exists():
        if not manifest_path.exists():
            raise SystemExit("Old output lacks a manifest; choose a new output directory")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["identity"] != identity:
            raise SystemExit("Code, dataset or configuration changed; choose a new output directory")
        results = json.loads(out.read_text(encoding="utf-8"))
    else:
        results = {}
        manifest_path.write_text(json.dumps({"identity": identity,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "scoring": "manual labels required; availability is not relevance; no scarcity adjustment"},
            ensure_ascii=False, indent=2), encoding="utf-8")
    for query in words[start:end]:
        if query in results:
            continue
        notices = []
        async def notify(message):
            notices.append(message)
        began = time.monotonic()
        try:
            result = await bot._handle_text(query, notify=notify)
            record = result_record(query, result, time.monotonic()-began, notices)
        except Exception as error:
            record = {"query": query, "error": type(error).__name__,
                      "elapsed_seconds": round(time.monotonic()-began, 3), "notices": notices}
        results[query] = record
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(json.dumps({"done": query, "items": len(record.get("items") or []),
                          "error": record.get("error")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
