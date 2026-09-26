#!/usr/bin/env python3
"""反向图搜引擎，供 booth.py imgsearch 调用。

引擎：Bing 视觉搜索（主力）+ ascii2d（备援）。
为什么不是 Google/SauceNAO：
- Google 对自动化客户端 403 封锁 /search 结果页，绕不过；
- SauceNAO 匿名账户禁用 JSON API，且索引不含 booth.pm；
- Bing 视觉搜索对 booth.pm 商品页覆盖好、反爬宽松，实测上传 4 秒出结果。

返回：按"结果页首次出现顺序"排序的 booth 商品 ID 列表（越靠前越相关）+ 引擎派生的关键词。
依赖：playwright（pip install playwright && playwright install chrome）。
profile 存于 ~/.booth-cli/pw_profile，信任 cookie 长期复用。
"""

import re
import time
import urllib.parse
from pathlib import Path

PROFILE_DIR = Path.home() / ".booth-cli" / "pw_profile"


def _open_browser(pw):
    prof = PROFILE_DIR
    prof.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(prof), headless=False, channel="chrome",
        viewport={"width": 1300, "height": 950},
        args=["--disable-blink-features=AutomationControlled"])


def _ordered_ids(html):
    """按首次出现顺序提取 booth 商品 ID（含 URL 编码形态）。"""
    seen = {}
    for cand in (html, html.replace("%2F", "/").replace("%3A", ":")):
        for m in re.finditer(r"items/(\d{4,})", cand):
            seen.setdefault(int(m.group(1)), None)
    return list(seen)


def _booth_links(html):
    links = re.findall(r'https?://[^"\'<>\\ ]*booth\.pm[^"\'<>\\ ]*', html)
    return list(dict.fromkeys(links))


def bing_search(image_path, wait_s=24):
    """Bing 视觉搜索：上传图片，返回 (item_ids有序, derived_query, result_url)。"""
    from playwright.sync_api import sync_playwright
    ids, query, final = [], "", ""
    with sync_playwright() as p:
        b = _open_browser(p)
        try:
            pg = b.pages[0] if b.pages else b.new_page()
            pg.goto("https://www.bing.com/images", timeout=45000)
            pg.wait_for_timeout(2500)
            inp = pg.locator('input[type="file"]').first
            if inp.count() == 0:
                for sel in ('button[aria-label*="Visual"]', 'div#sb_sbip',
                            'button[aria-label*="视觉"]'):
                    try:
                        pg.locator(sel).first.click(timeout=4000)
                        if pg.locator('input[type="file"]').count() > 0:
                            break
                    except Exception:
                        continue
            pg.locator('input[type="file"]').first.set_input_files(str(image_path))
            deadline = time.time() + wait_s
            while time.time() < deadline:
                pg.wait_for_timeout(3000)
                final = pg.url
                ids = _ordered_ids(pg.content())
                m = re.search(r"[?&]q=([^&]+)", final)
                if m and not query:
                    query = urllib.parse.unquote_plus(m.group(1))
                if ids and ("q=" in final or "bcid" in final):
                    break
        finally:
            b.close()
    return ids, query, final


def ascii2d_search(image_path, wait_s=24):
    """ascii2d：上传图片（色合い→特徴），返回 (item_ids有序, page_url)。"""
    from playwright.sync_api import sync_playwright
    ids, final = [], ""
    with sync_playwright() as p:
        b = _open_browser(p)
        try:
            pg = b.pages[0] if b.pages else b.new_page()
            pg.goto("https://ascii2d.net/", timeout=45000)
            pg.wait_for_timeout(2500)
            form = pg.locator('form:has(input[type="file"])').first
            form.locator('input[type="file"]').first.set_input_files(str(image_path))
            try:
                form.locator('button[type="submit"], input[type="submit"]').first.click(timeout=5000)
            except Exception:
                pass
            deadline = time.time() + wait_s
            while time.time() < deadline:
                pg.wait_for_timeout(3000)
                final = pg.url
                if "/search/" in final:
                    ids = _ordered_ids(pg.content())
                    break
            try:
                feat = pg.locator('a:has-text("特徴")').first
                if feat.count() > 0:
                    feat.click(timeout=6000)
                    pg.wait_for_timeout(6000)
                    ids = _ordered_ids(pg.content()) or ids
            except Exception:
                pass
        finally:
            b.close()
    return ids, final
