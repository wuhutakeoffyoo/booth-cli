"""盲测取样脚本：从 Booth 多分类抽取真实商品，下载官方图，生成 truth.json。"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path("D:/ZCODE/blind4")
OUT.mkdir(exist_ok=True)
(OUT / "images").mkdir(exist_ok=True)

ALLOWED_HOSTS = ("booth.pm", "booth.pximg.net")


def assert_booth_image_url(url):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"图片 URL 超出许可范围: {url[:120]}")
    return url


def run_cli(args):
    r = subprocess.run([sys.executable, "-X", "utf8", "booth.py"] + args,
                       capture_output=True, text=True, encoding="utf-8", timeout=90)
    return json.loads(r.stdout)


CATS = ["3Dキャラクター", "3D衣装", "3D髪型", "3D小道具", "3Dテクスチャ"]
PICK_PER_CAT = 2

samples = []
for cat in CATS:
    res = run_cli(["search", cat, "--category", cat, "--sort", "popularity",
                   "--limit", "6", "--json"])
    picked = 0
    for it in res["items"]:
        if picked >= PICK_PER_CAT:
            break
        if not it.get("image") or it.get("is_adult"):
            continue
        try:
            detail = run_cli(["item", str(it["id"]), "--json"])
        except Exception as e:
            print("item fail", it["id"], e)
            continue
        img = (detail.get("images") or [None])[0] or it["image"]
        samples.append({
            "id": it["id"], "name": it["name"], "category": it.get("category"),
            "shop": it["shop"]["name"], "image_url": img,
        })
        picked += 1
    print(f"{cat}: picked {picked}")

for s in samples:
    assert_booth_image_url(s["image_url"])
    url = s["image_url"]
    base = url.rsplit("/", 1)[-1]
    thumb = url.replace("/" + base, "/c/300x300_a2_g5/" + base + "_base_resized") \
        if "/c/" not in url else url
    for cand in (thumb, url):
        try:
            req = urllib.request.Request(cand, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://booth.pm/"})
            data = urllib.request.urlopen(req, timeout=25).read()
            if data[:2] == b"\xff\xd8":
                (OUT / "images" / f"{s['id']}.jpg").write_bytes(data)
                s["image_file"] = f"{s['id']}.jpg"
                break
        except Exception as e:
            print("dl fail", s["id"], str(e)[:60])
    else:
        s["image_file"] = None

(OUT / "truth.json").write_text(json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")
for s in samples:
    print(s["id"], (s["image_file"] or "NO-IMG").ljust(12), s["name"][:44])
