"""VRC 对口 100 样本取样（在 SG 服务器运行）：
搜索 q=VRChat 的随机页抽取 VRChat 商品，规则化 jp_query，下载官方图。
输出 ~/vblind100/truth.json 与 images/。"""
import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path.home() / "vblind100"
(OUT / "images").mkdir(parents=True, exist_ok=True)
BOOTH_CLI = Path.home() / "booth-cli" / "booth.py"
random.seed(20260928)


def run_cli(args, timeout=90):
    r = subprocess.run([sys.executable, "-X", "utf8", str(BOOTH_CLI)] + args,
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return json.loads(r.stdout)


def clean_jp_query(title):
    t = re.sub(r"【[^】]*】", " ", title)
    t = re.sub(r"[^\wぁ-んァ-ヶ一-龠ー～＆ ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:16].strip()


def dl(url, dest):
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
pages = list(range(1, 60))
random.shuffle(pages)
TARGET = 100
for page in pages:
    if len(samples) >= TARGET:
        break
    try:
        res = run_cli(["search", "VRChat", "--sort", "new", "--page", str(page),
                       "--limit", "30", "--json"])
    except Exception as e:
        print("search fail", page, str(e)[:60], flush=True)
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
    print(f"pool={len(samples)} (page {page})", flush=True)

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
print(f"DONE: {len(samples)} VRC samples, {ok} with images", flush=True)
