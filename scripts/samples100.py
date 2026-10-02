"""100 样本取样（在海外服务器运行）：多分类×随机页抽取真实商品，规则化 jp_query，下载官方图。"""
import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path.home() / "blind100"
(OUT / "images").mkdir(parents=True, exist_ok=True)
BOOTH_CLI = Path.home() / "booth-cli" / "booth.py"
random.seed(20260928)  # 固定种子保证可复现（非安全用途）

CATS = ["3Dキャラクター", "3D衣装", "3D髪型", "3D小道具", "3Dテクスチャ",
        "3D靴", "3D装飾品", "VRChat", "ソフトウェア", "コスプレ"]
TARGET = 100


def run_cli(args, timeout=90):
    r = subprocess.run([sys.executable, "-X", "utf8", str(BOOTH_CLI)] + args,
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return json.loads(r.stdout)


def clean_jp_query(title):
    """规则化日文查询：去【】装饰块与符号，取清洗后标题前 16 字（标题子串必可命中）。"""
    t = re.sub(r"【[^】]*】", " ", title)
    t = re.sub(r"[^\wぁ-んァ-ヶ一-龠ー～＆ ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:16].strip()


def dl(url, dest):
    """仅允许 Booth 官方图床（https），下载缩略图/原图（含重试）。"""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ("booth.pm", "booth.pximg.net"):
        return False
    stem = url[:-4] if url.endswith(".jpg") else url
    cands = [stem.replace("booth.pximg.net/", "booth.pximg.net/c/300x300_a2_g5/", 1)
             + "_base_resized.jpg", url]
    for cand in cands:
        for _ in range(2):
            try:
                req = urllib.request.Request(cand, headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://booth.pm/"})
                data = urllib.request.urlopen(req, timeout=25).read()
                if data[:2] == b"\xff\xd8":
                    dest.write_bytes(data)
                    return True
            except Exception:
                time.sleep(1.5)
    return False


seen, samples = set(), []
random.shuffle(CATS)
while len(samples) < TARGET:
    for cat in CATS:
        if len(samples) >= TARGET:
            break
        page = random.randint(1, 12)
        try:
            res = run_cli(["search", cat, "--category", cat, "--sort", "new",
                           "--page", str(page), "--limit", "30", "--json"])
        except Exception as e:
            print("search fail", cat, page, str(e)[:60], flush=True)
            time.sleep(5)
            continue
        time.sleep(1.5)
        for it in res.get("items") or []:
            if len(samples) >= TARGET:
                break
            iid = it["id"]
            if iid in seen or not it.get("image") or not it.get("name"):
                continue
            seen.add(iid)
            jq = clean_jp_query(it["name"])
            if len(jq) < 4:
                continue
            samples.append({"id": iid, "name": it["name"], "category": it.get("category"),
                            "shop": it["shop"]["name"], "image_url": it["image"],
                            "jp_query": jq})
        print(f"pool={len(samples)}", flush=True)

ok = 0
for i, s in enumerate(samples):
    dest = OUT / "images" / f"{s['id']}.jpg"
    if dl(s["image_url"], dest):
        s["image_file"] = f"{s['id']}.jpg"
        ok += 1
    else:
        s["image_file"] = None
    if i % 10 == 9:
        print(f"img {i+1}/{len(samples)}", flush=True)

(OUT / "truth.json").write_text(
    json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"DONE: {len(samples)} samples, {ok} with images", flush=True)
