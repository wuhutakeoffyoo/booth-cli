"""单链路盲测 harness（支持 100 样本切片）：
python blind_one.py <jp|zh|img> [--start N] [--end M] [--delay S]
样本取 ~/blind100/truth.json（缺省回退 ~/blind/truth.json 10 样本版）。
结果写 ~/blind100/results_{mode}_{start}-{end}.json。IMG 零候选自动退避（风控保护）。"""
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

import argparse

ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=["jp", "zh", "img"])
ap.add_argument("--start", type=int, default=0)
ap.add_argument("--end", type=int, default=10)
ap.add_argument("--delay", type=float, default=None)
ap.add_argument("--dir", default="blind100")
ap.add_argument("--port", type=int, default=8799)
ARGS = ap.parse_args()
MODE = ARGS.mode
DELAY = ARGS.delay if ARGS.delay is not None else (5.0 if MODE == "img" else 2.5)

import nonebot  # noqa: E402

nonebot.init()
import booth_search as bs  # noqa: E402

B100 = Path.home() / ARGS.dir
BLIND = Path.home() / "blind"
truth_path = (B100 / "truth.json") if (B100 / "truth.json").exists() else (BLIND / "truth.json")
samples = json.loads(truth_path.read_text(encoding="utf-8"))[ARGS.start:ARGS.end]
RESULTS = (B100 if B100.exists() else BLIND) / f"results_{MODE}_{ARGS.start}-{ARGS.end}.json"

ID_RE = re.compile(r"items/(\d+)")


def rank_of(text, target):
    ids = []
    for m in ID_RE.finditer(text):
        i = int(m.group(1))
        if i not in ids:
            ids.append(i)
    return (ids.index(target) + 1) if target in ids else 0, len(ids)


_zero_streak = 0


async def main():
    server = None
    if MODE == "img":
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(ARGS.port),
             "--directory", str(B100 / "images")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

    global _zero_streak
    results = {}
    try:
        for idx, s in enumerate(samples):
            tid = s["id"]
            entry = {"name": s["name"][:44]}
            try:
                if MODE in ("jp", "zh") and not s.get(f"{MODE}_query"):
                    entry["rank"], entry["err"] = -2, "no query"
                    results[str(tid)] = entry
                    print(f"[{MODE}] {tid} skip(no query)", flush=True)
                    continue
                if MODE == "jp":
                    out = (await bs._handle_text(s["jp_query"]))["text"]
                elif MODE == "zh":
                    out = (await bs._handle_text(s["zh_query"]))["text"]
                elif not s.get("image_file"):
                    entry["rank"], entry["err"] = -2, "no image"
                    results[str(tid)] = entry
                    print(f"[{MODE}] {tid} skip(no image)", flush=True)
                    continue
                else:
                    out = (await bs._handle_image(
                        f"http://127.0.0.1:8799/{s['image_file']}", ""))["text"]
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
            print(f"[{MODE}] {idx+ARGS.start} {tid} rank={entry['rank']} "
                  f"err={entry.get('err', '')[:50]}", flush=True)
            if MODE == "img" and entry.get("total", 1) == 0 and entry.get("rank", -1) != -1:
                _zero_streak += 1
                if _zero_streak >= 6:
                    print("[fuse] 连续零候选过多，熔断停止本进程", flush=True)
                    break
                if _zero_streak >= 2:
                    back = min(30 * _zero_streak, 90)
                    print(f"[backoff] {_zero_streak} 连续零候选，退避 {back}s", flush=True)
                    await asyncio.sleep(back)
            else:
                _zero_streak = 0
            await asyncio.sleep(DELAY)
    finally:
        if server:
            server.terminate()
    print("ALL DONE", flush=True)


asyncio.run(main())
