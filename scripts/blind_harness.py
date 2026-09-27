"""三链路盲测 harness：日文直搜 / 中文AI翻译 / 识图，真实 bot 代码栈。"""
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / "booth-bot" / "src" / "plugins"))
sys.path.insert(0, str(Path.home() / "booth-bot"))

import nonebot  # noqa: E402

nonebot.init()
import booth_search as bs  # noqa: E402

BLIND = Path.home() / "blind"
samples = json.loads((BLIND / "truth.json").read_text(encoding="utf-8"))
RESULTS = BLIND / "results.json"

ID_RE = re.compile(r"items/(\d+)")


def rank_of(text, target):
    ids = []
    for m in ID_RE.finditer(text):
        i = int(m.group(1))
        if i not in ids:
            ids.append(i)
    return (ids.index(target) + 1) if target in ids else 0, len(ids)


async def main():
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8799",
         "--directory", str(BLIND / "images")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    results = {}
    try:
        for s in samples:
            tid = s["id"]
            entry = {"name": s["name"][:40], "jp_query": s["jp_query"],
                     "zh_query": s["zh_query"]}

            try:
                out = await bs._handle_text(s["jp_query"])
                r, n = rank_of(out, tid)
                entry["jp_rank"], entry["jp_total"] = r, n
            except Exception as e:
                entry["jp_rank"], entry["jp_err"] = -1, f"{type(e).__name__}: {str(e)[:120]}"
            results[str(tid)] = entry
            RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            print(f"[JP] {tid} rank={entry['jp_rank']}", flush=True)

            try:
                out = await bs._handle_text(s["zh_query"])
                r, n = rank_of(out, tid)
                entry["zh_rank"], entry["zh_total"] = r, n
                m = re.search(r"AI 关键词: (.+?)（原词", out)
                entry["zh_keywords"] = m.group(1) if m else "(无AI行)"
            except Exception as e:
                entry["zh_rank"], entry["zh_err"] = -1, f"{type(e).__name__}: {str(e)[:120]}"
            results[str(tid)] = entry
            RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            print(f"[ZH] {tid} rank={entry['zh_rank']} kw={entry.get('zh_keywords', '')[:60]}",
                  flush=True)

            try:
                out = await bs._handle_image(
                    f"http://127.0.0.1:8799/{s['image_file']}", "")
                r, n = rank_of(out, tid)
                entry["img_rank"], entry["img_total"] = r, n
                m = re.search(r"识图关键词: (.+)", out)
                entry["img_keywords"] = (m.group(1)[:100]) if m else "(无)"
            except Exception as e:
                entry["img_rank"], entry["img_err"] = -1, f"{type(e).__name__}: {str(e)[:120]}"
            results[str(tid)] = entry
            RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            print(f"[IMG] {tid} rank={entry['img_rank']}", flush=True)
            print(f"--- done {tid} ---", flush=True)
    finally:
        server.terminate()

    print("ALL DONE", flush=True)


asyncio.run(main())
