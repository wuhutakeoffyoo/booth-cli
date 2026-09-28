"""为 100 样本生成中文用户模拟查询（glm-5.3-flash，Go 端点）。
在 SG 服务器运行：读取 ~/blind100/truth.json，写回 zh_query 字段。"""
import asyncio
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path.home() / "booth-bot" / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init()
import httpx  # noqa: E402
from booth_search import vision  # noqa: E402
from booth_search.config import Config  # noqa: E402
from nonebot import get_plugin_config  # noqa: E402

cfg = get_plugin_config(Config)
BLIND_DIR = Path.home() / (sys.argv[1] if len(sys.argv) > 1 else "blind100")
TRUTH = BLIND_DIR / "truth.json"

PROMPT = ("你是中国 VRChat 玩家，在 Booth.pm 看到了这个商品页面，想之后能搜到它。"
          "请给出你会输入的中文搜索词：2-8 个字的自然中文短语（可包含你知道的商品名原文、"
          "角色名中文叫法或类型词）。只输出短语本身，不要任何解释。")


def make_payload(title, category):
    return {
        "model": cfg.vision_model,
        "messages": [{"role": "user", "content": f"{PROMPT}\n商品标题：{title}\n分类：{category or '未知'}"}],
        "temperature": 0.6,
        "max_tokens": 2000,
    }


async def gen_one(client, sem, s):
    vision.guard_api_base(cfg.vision_base_url)
    url = cfg.vision_base_url.rstrip("/") + "/chat/completions"
    async with sem:
        for attempt in range(2):
            try:
                resp = await client.post(
                    url, json=make_payload(s["name"], s.get("category")),
                    headers={"Authorization": f"Bearer {cfg.vision_api_key}",
                             "x-opencode-session": f"zhgen-{s['id']}",
                             "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                           "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"})
                resp.raise_for_status()
                choice = (resp.json().get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                content = re.sub(r"\s+", " ", content).strip("「」\"'。. ")
                if 2 <= len(content) <= 20:
                    return str(s["id"]), content
            except Exception as e:
                if attempt == 1:
                    print(f"gen fail {s['id']}: {type(e).__name__} {str(e)[:80]}", flush=True)
                await asyncio.sleep(2)
    return str(s["id"]), None


async def main():
    samples = json.loads(TRUTH.read_text(encoding="utf-8"))
    todo = [s for s in samples if not s.get("zh_query")]
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=90) as client:
        for i, coro in enumerate(asyncio.as_completed([gen_one(client, sem, s) for s in todo])):
            sid, zh = await coro
            for s in samples:
                if str(s["id"]) == sid:
                    s["zh_query"] = zh
            if (i + 1) % 10 == 0:
                print(f"gen {i+1}/{len(todo)}", flush=True)
                TRUTH.write_text(json.dumps(samples, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    TRUTH.write_text(json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(1 for s in samples if s.get("zh_query"))
    print(f"DONE: {n}/{len(samples)} have zh_query", flush=True)


asyncio.run(main())
