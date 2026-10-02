"""随机目标盲测词表生成：从 Booth 分类导航随机抽取真实商品标题中的核心词。
运行（在 bot 仓库 venv）：python gen_random_words.py <输出json> [数量=100]
规则：随机分类 × 随机页取商品，LLM 无关（纯规则抽词，避免同源方差），
人工剔除类目词后保留「物件/功能/素材」性质的 2-6 字核心词，去重去旧表重叠。
"""
import asyncio
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, "src/plugins")
import nonebot  # noqa: E402

nonebot.init()
from booth_search import booth_client  # noqa: E402

CATEGORIES = ["3Dキャラクター", "3D衣装", "3D小道具", "3D装飾品", "3Dテクスチャ",
              "3D髪型", "3D靴", "ソフトウェア"]
# 排除：类目通用词、品牌/专有名词特征（含大写连续/片假名长串留给人工判断），
# 这里用否定黑名单 + 长度启发，最后人工可编辑输出文件
STOP = {"3D", "VRChat", "対応", "アバター", "セット", "モデル", "無料", "MA対応",
        "Modular", "Avatar", "Unity", "BOOTH", "ver", "初回", "限定", "販売",
        "更新的", "再贩", "予約"}
OLD_WORDS = set()


def extract_core_words(title: str) -> list:
    """从标题提取候选核心词：必须含日文字符（可搜索的实义目标），
    去「専用/対応」类缀词，去含数字/纯拉丁的片段。"""
    t = re.sub(r"【\[（(].*?】\]）)]", " ", title)  # 去括注
    t = re.sub(r"[^\wぁ-んァ-ヶ一-龠々ー]+", " ", t)
    words = []
    for w in t.split():
        w = re.sub(r"(専用|対応|向け)$", "", w)  # 去缀词
        if not (2 <= len(w) <= 8):
            continue
        if not re.search(r"[ぁ-んァ-ヶ一-龠々ー]", w):
            continue  # 必须含日文字符
        if re.search(r"[0-9]", w) or any(s in w for s in STOP):
            continue
        words.append(w)
    return words


async def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("random_words.json")
    n_target = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    rng = random.Random(20261002)  # 固定种子保证可复现
    picked, seen = [], set()
    attempts = 0
    while len(picked) < n_target and attempts < 400:
        attempts += 1
        cat = rng.choice(CATEGORIES)
        page = rng.randint(1, 6)
        try:
            res = await asyncio.to_thread(
                booth_client.search, "", limit=20, sort="new", adult="exclude",
                category=cat, page=page, timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {cat} p{page}: {str(e)[:60]}", flush=True)
            continue
        items = res.get("items") or []
        if not items:
            continue
        it = rng.choice(items)  # 每页随机取 1 件，避免同页集中
        for w in extract_core_words(it.get("name") or ""):
            if w not in seen and w not in OLD_WORDS:
                seen.add(w)
                picked.append(w)
                break  # 每件商品只取 1 词，保证来源分散
        if attempts % 40 == 0:
            print(f"[progress] {len(picked)}/{n_target} (attempts={attempts})", flush=True)
    out = {"desc": "随机目标盲测词表（Booth 分类导航随机抽取的核心词，固定种子可复现）",
           "seed": 20261002, "count": len(picked), "words": picked[:n_target]}
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"DONE: {len(picked)} words -> {out_path}", flush=True)


asyncio.run(main())
