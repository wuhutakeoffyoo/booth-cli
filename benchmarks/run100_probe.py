"""100 词中文泛称盲测探针（本地运行）：逐词走完整文本链路，Top6 结果写入 JSON。

用法:
    python run100_probe.py <words_json> <out_dir> [起始] [结束]
    支持断点续跑（输出 JSON 里已有的词自动跳过）；建议 2 进程按起止切分并行。

环境: 在 bot 仓库根目录用其 venv 运行（sys.path 依赖 src/plugins 与 .env）。
"""
import asyncio
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, "src/plugins")

import nonebot  # noqa: E402

nonebot.init()
from booth_search import _handle_text  # noqa: E402

WORDS_FILE = Path(sys.argv[1])
OUT_DIR = Path(sys.argv[2])
WORDS = json.loads(WORDS_FILE.read_text(encoding="utf-8"))["words"]


async def notify(msg: str) -> None:
    print(f"[NOTIFY] {msg}", flush=True)


async def main() -> None:
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    end = int(sys.argv[4]) if len(sys.argv) > 4 else len(WORDS)
    out = OUT_DIR / f"probe_{start}_{end}.json"
    results = {}
    if out.exists():
        results = json.loads(out.read_text(encoding="utf-8"))
    for w in WORDS[start:end]:
        if w in results:  # 断点续跑
            continue
        entry = {"query": w}
        try:
            r = await _handle_text(w, notify=notify)
            entry["header"] = (r.get("header") or r.get("text") or "")[:400]
            entry["items"] = [{"id": it.get("id"),
                               "name": (it.get("name") or "")[:70]}
                              for it in (r.get("entries") or [])[:6]]
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {str(e)[:150]}"
            traceback.print_exc()
        results[w] = entry
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"[done] {w}: {len(entry.get('items') or [])} items", flush=True)
    print(f"PROBE-DONE {start}-{end}", flush=True)


asyncio.run(main())
