"""单链路盲测 harness：python blind_one.py <jp|zh|img>

JP: 日文直搜；ZH: 中文AI翻译；IMG: 识图（起本地图片HTTP服务）。
结果逐样本写入 ~/blind/results_<mode>.json，日志 ~/blind/run_<mode>.log。
"""
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else "jp"
assert MODE in ("jp", "zh", "img"), MODE

sys.path.insert(0, str(Path.home() / "booth-bot" / "src" / "plugins"))
sys.path.insert(0, str(Path.home() / "booth-bot"))

import nonebot  # noqa: E402

nonebot.init()
import booth_search as bs  # noqa: E402

BLIND = Path.home() / "blind"
samples = json.loads((BLIND / "truth.json").read_text(encoding="utf-8"))
RESULTS = BLIND / f"results_{MODE}.json"

ID_RE = re.compile(r"items/(\d+)")


def rank_of(text, target):
    ids = []
    for m in ID_RE.finditer(text):
        i = int(m.group(1))
        if i not in ids:
            ids.append(i)
    return (ids.index(target) + 1) if target in ids else 0, len(ids)


async def main():
    server = None
    if MODE == "img":
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", "8799",
             "--directory", str(BLIND / "images")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

    results = {}
    try:
        for s in samples:
            tid = s["id"]
            entry = {"name": s["name"][:44]}
            try:
                if MODE == "jp":
                    out = await bs._handle_text(s["jp_query"])
                elif MODE == "zh":
                    out = await bs._handle_text(s["zh_query"])
                else:
                    out = await bs._handle_image(
                        f"http://127.0.0.1:8799/{s['image_file']}", "")
                r, n = rank_of(out, tid)
                entry["rank"], entry["total"] = r, n
                if MODE != "jp":
                    kw = (re.search(r"AI 关键词: (.+?)（原词", out) if MODE == "zh"
                          else re.search(r"识图关键词: (.+)", out))
                    entry["keywords"] = (kw.group(1)[:110]) if kw else "(无)"
                if "⚠" in out:
                    warn = [ln.strip() for ln in out.splitlines() if "⚠" in ln]
                    entry["warn"] = warn[0][:160]
            except Exception as e:
                entry["rank"], entry["err"] = -1, f"{type(e).__name__}: {str(e)[:150]}"
            results[str(tid)] = entry
            RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            print(f"[{MODE}] {tid} rank={entry['rank']} err={entry.get('err', '')[:60]}",
                  flush=True)
    finally:
        if server:
            server.terminate()
    print("ALL DONE", flush=True)


asyncio.run(main())
