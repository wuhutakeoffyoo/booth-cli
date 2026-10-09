#!/usr/bin/env python3
"""booth — Booth.pm (BOOTH 同人/VRChat 素材市场) 命令行搜索工具，为 AI agent 设计。

零第三方依赖，只用 Python 标准库。数据来自 Booth 页面内嵌的结构化数据与
非官方 JSON 接口（/ja/items/{id}.json），请在请求间保持 ≥1 秒间隔以减轻服务器负担。
内置 sqlite 磁盘缓存（商品 6h / 搜索页 10min，--no-cache 跳过）与
Retry-After 感知的指数退避重试。

用法:
  booth workflow <需求...> --keyword <检索词>  同一个 AI 的工具入口：返回候选与来源 JSON
  booth workflow --schema             查看工作流接口，无网络请求
  booth smart  <需求描述...> [选项]    本地词扩展检索；--delegate-ai 可选委托另一模型
  booth search <关键词...> [选项]     搜索全站商品
  booth item   <商品ID|URL> [选项]    查看商品详情
  booth shop   <商店子域名|URL> [选项] 查看商店信息与最新商品
  booth imgsearch <图片路径|URL> [选项]  以图搜品（Bing 纯HTTP优先,失败回落浏览器; ascii2d 备援;
                                     浏览器备援需 playwright）
  booth bot '<json>'                  bot 框架接入钩子：JSON 信封进出（见 QQBOT.md）
  booth help                          显示帮助

AI 调用建议: 一律加 --json 获取结构化输出。
内置对 booth.pm 的全局限速（每请求 ≥1 秒间隔）。
search/smart 默认收窄 VRChat 圈（--tag VRChat），--no-vrc 关闭。
默认由调用者 AI 规划与判断，不读取 API 配置发起模型调用。
smart/imgsearch 只有显式 --delegate-ai 才连接可选的 AI 服务。
"""

import argparse
import contextlib
import email.utils
import gzip
import html as html_mod
from html.parser import HTMLParser
import io
import json
import random
import re
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import request_budget
import uuid
import search_evidence

__version__ = "1.6.0"

BASE = "https://booth.pm"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PAGE_DELAY = 1.2          # 多页抓取时的请求间隔（秒）
DEFAULT_DESC_LEN = 600    # 商品详情描述默认截断长度
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 4          # 限流/临时错误的退避重试次数（借鉴 tenacity 的指数退避+抖动）

# 磁盘缓存（借鉴 requests-cache 的持久 HTTP 缓存思路，sqlite 实现，零依赖）
CACHE_PATH = Path.home() / ".booth-cli" / "cache.sqlite3"
CACHE_TTL_ITEM = 6 * 3600   # 商品 JSON 变化少，缓存 6 小时
CACHE_TTL_PAGE = 600        # 搜索/商店页要求新鲜度，缓存 10 分钟
_CACHE_CONN = None          # 惰性初始化；False 表示不可用
_CACHE_LOCK = threading.RLock()
_NO_CACHE = False

SORTS = ("new", "popularity", "liked", "price_asc", "price_desc")
TYPES = ("all", "digital", "physical")
# 常用分类 slug（VRChat 相关，来自搜索页分类导航，日语原文才能匹配）
CATEGORY_HINTS = ("3Dキャラクター", "3D衣装", "3D小道具", "3D装飾品", "3Dテクスチャ",
                  "3D髪型", "3D靴", "3Dモデル（その他）", "VRoid", "ソフトウェア")

CLOUDFLARE_HINT = ("店铺开启了 Cloudflare 人机验证（返回 'Just a moment' 页面），"
                   "该站无法直接抓取；可改用 booth item <ID> 逐个查商品。")

# 安全边界: 本工具只访问 Booth 官方域名，协议仅限 https，重定向逐跳校验
ALLOWED_HOST_SUFFIX = ".booth.pm"

# 出站限速：对 booth.pm 的所有请求全局保持最小间隔（对站点的礼貌，也是防风控）
MIN_REQUEST_INTERVAL = 1.0
_REQ_LOCK = threading.Lock()
_LAST_REQ_TS = 0.0


def _polite_wait():
    """保证距上一次出站请求至少 MIN_REQUEST_INTERVAL 秒（线程安全）。"""
    global _LAST_REQ_TS
    with _REQ_LOCK:
        try:
            _LAST_REQ_TS = request_budget.wait(MIN_REQUEST_INTERVAL)
        except request_budget.BudgetError as e:
            raise BoothError(str(e)) from e


class BoothError(Exception):
    pass


def assert_allowed_url(url):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "booth.pm" or host.endswith(ALLOWED_HOST_SUFFIX)):
        raise BoothError(f"URL 超出许可范围（仅允许 https://*.booth.pm）: {url}")
    return url


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def set_cache_enabled(enabled):
    global _NO_CACHE
    _NO_CACHE = not enabled


def _cache():
    """惰性打开 sqlite 缓存；不可用时返回 None（降级为无缓存）。"""
    global _CACHE_CONN
    with _CACHE_LOCK:
        return _open_cache()


def _open_cache():
    global _CACHE_CONN
    if _CACHE_CONN is None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(CACHE_PATH), timeout=2, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS cache "
                         "(key TEXT PRIMARY KEY, ts REAL, body BLOB, final_url TEXT)")
            conn.execute("DELETE FROM cache WHERE ts < ?", (time.time() - 7 * 24 * 3600,))
            conn.commit()
            _CACHE_CONN = conn
        except Exception:
            _CACHE_CONN = False
    return _CACHE_CONN if _CACHE_CONN else None


def cache_get(url, ttl):
    if _NO_CACHE or ttl <= 0:
        return None
    conn = _cache()
    if not conn:
        return None
    try:
        with _CACHE_LOCK:
            row = conn.execute("SELECT ts, body, final_url FROM cache WHERE key = ?",
                               (url,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or time.time() - row[0] > ttl:
        return None
    return bytes(row[1]), row[2]


def cache_put(url, body, final_url):
    if _NO_CACHE:
        return
    conn = _cache()
    if not conn:
        return
    try:
        with _CACHE_LOCK:
            conn.execute("INSERT OR REPLACE INTO cache (key, ts, body, final_url) VALUES (?,?,?,?)",
                         (url, time.time(), body, final_url))
            conn.commit()
    except sqlite3.Error:
        pass


def _retry_delay(attempt, headers=None):
    """退避延迟：优先响应 Retry-After（tenacity 思路），否则指数退避 + 抖动。"""
    if headers:
        ra = headers.get("Retry-After")
        if ra:
            if re.fullmatch(r"[0-9]+", str(ra).strip()):
                try:
                    return int(str(ra).strip())
                except ValueError:
                    pass
            else:
                try:
                    dt = email.utils.parsedate_to_datetime(ra)
                    delay = dt.timestamp() - time.time()
                    return max(delay, 0.0)
                except Exception:
                    pass
    return min(1.5 * (2 ** attempt), 15.0) + random.uniform(0.0, 1.0)


def http_get(url, *, json_accept=False, csrf=None, cache_ttl=0):
    """GET 一个 booth.pm 的 URL，返回 (body_bytes, final_url, status)。

    重定向手动跟随并逐跳校验主机白名单；限流/临时错误按 Retry-After 或指数
    退避重试；cache_ttl>0 时启用磁盘缓存。
    """
    assert_allowed_url(url)
    cached = cache_get(url, cache_ttl)
    if cached is not None:
        return cached[0], cached[1], 200
    headers = {
        "User-Agent": UA,
        "Accept": "application/json" if json_accept else "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.7,zh-CN;q=0.6",
        "Accept-Encoding": "gzip",
        "Cookie": "adult=t",  # 绕过年龄确认页；R-18 结果仍由 adult 参数控制
    }
    if csrf:
        headers["X-Csrf-Token"] = csrf
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin"

    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        current = url
        for _hop in range(MAX_REDIRECTS):
            try:
                req = urllib.request.Request(current, headers=headers)
                _polite_wait()
                with _OPENER.open(req, timeout=30) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    if cache_ttl > 0:
                        cache_put(url, data, current)
                    return data, current, resp.status
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc:
                        raise BoothError(f"重定向缺少 Location: {current}")
                    nxt = urllib.parse.urljoin(current, loc)
                    assert_allowed_url(nxt)
                    current = nxt
                    continue
                if e.code in (403, 429, 502, 503, 504):
                    body = b""
                    try:
                        body = e.read()
                    except Exception:
                        pass
                    if e.code == 403 and b"Just a moment" in body:
                        raise BoothError(CLOUDFLARE_HINT)
                    last_err = BoothError(f"HTTP {e.code}（可能是限流或防护页）: {current}")
                    delay = _retry_delay(attempt, e.headers)
                    try:
                        if e.code in (429, 503):
                            request_budget.cooldown(delay)
                        request_budget.retry_allowed(delay)
                    except request_budget.BudgetError as budget_err:
                        raise BoothError(str(budget_err)) from budget_err
                    time.sleep(delay)
                    break  # 走外层重试
                raise BoothError(f"HTTP {e.code}: {current}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = BoothError(f"网络错误: {e} ({current})")
                time.sleep(_retry_delay(attempt))
                break  # 走外层重试
        else:
            raise BoothError(f"重定向次数过多: {url}")
    raise last_err or BoothError(f"请求失败: {url}")


def get_html(url, cache_ttl=CACHE_TTL_PAGE):
    data, final_url, _ = http_get(url, cache_ttl=cache_ttl)
    text = data.decode("utf-8", errors="replace")
    if "Just a moment" in text and "challenge" in text.lower():
        raise BoothError(CLOUDFLARE_HINT)
    if "18歳未満" in text and "data-product-id" not in text and "data-item=" not in text:
        raise BoothError("返回了年龄确认页，未能获取内容（内部错误，请重试）")
    return text, final_url


def get_json(url, csrf=None, cache_ttl=None):
    data, _, _ = http_get(url, json_accept=True, csrf=csrf,
                          cache_ttl=CACHE_TTL_ITEM if cache_ttl is None else cache_ttl)
    return json.loads(data.decode("utf-8", errors="replace"))


def download_image(url):
    """下载 booth 官方图床（booth.pm / booth.pximg.net）的 JPEG 到本地字节。"""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (
            host == "booth.pm" or host.endswith(".booth.pm") or host == "booth.pximg.net"):
        raise BoothError(f"图片 URL 超出许可范围（仅允许 booth 官方图床）: {url}")
    _polite_wait()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://booth.pm/"})
    data = _OPENER.open(req, timeout=30).read()
    if data[:2] != b"\xff\xd8":
        raise BoothError("目标不是有效 JPEG 图片")
    return data


# ---------------------------------------------------------------- 解析工具

def unescape(s):
    return html_mod.unescape(s or "").strip()


def clean_text(html_fragment):
    """HTML -> 纯文本（去标签、合并空白）。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_fragment or "",
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def original_image(url):
    """把 300x300 缩略图还原为原图 URL。"""
    if not url:
        return None
    url = re.sub(r"/c/[^/]+/", "/", url)
    return url.replace("_base_resized", "")


def parse_item_id(s):
    s = (s or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"/items/(\d+)", s)
    if m:
        return m.group(1)
    raise BoothError(f"无法识别的商品 ID 或 URL: {s}")


def parse_shop_subdomain(s):
    s = (s or "").strip()
    s = re.sub(r"^https?://", "", s)
    m = re.match(r"^([a-z0-9][a-z0-9-]*)\.booth\.pm", s, re.I)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", s, re.I):
        return s.lower()
    raise BoothError(f"无法识别的商店子域名或 URL: {s}")


# ---------------------------------------------------------------- 搜索页解析

ATTR_RE = re.compile(r'data-(product-id|product-brand|product-price|product-category|product-event)="([^"]*)"')


class _SearchPaginationParser(HTMLParser):
    """Recognize usable next-page links in both BOOTH pagination layouts."""

    _VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.has_next = False
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        disabled = (bool(self._stack and self._stack[-1][1])
                    or "disabled" in attrs
                    or (attrs.get("aria-disabled") or "").lower() == "true"
                    or bool({"disabled", "is-disabled"}.intersection(classes)))
        href = (attrs.get("href") or "").strip()
        usable = bool(href and not href.startswith("#") and not disabled)
        if tag in ("a", "link") and usable and "next" in (attrs.get("rel") or "").lower().split():
            self.has_next = True
        if tag == "i" and "icon-arrow-open-right" in classes and not disabled:
            anchor = next((frame for frame in reversed(self._stack) if frame[0] == "a"), None)
            if anchor and anchor[2]:
                try:
                    parts = urllib.parse.urlsplit(anchor[2])
                    pages = urllib.parse.parse_qs(parts.query).get("page", [])
                    if parts.scheme in ("", "https", "http") and any(p.isdigit() and int(p) > 0 for p in pages):
                        self.has_next = True
                except ValueError:
                    pass
        if tag not in self._VOID:
            self._stack.append((tag, disabled, href if tag == "a" and usable else None))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)


def parse_search_page(page_html):
    """从搜索结果 HTML 提取商品卡片与元信息。"""
    total = None
    m = re.search(r"対象商品\s*([\d,]+)\s*件", page_html)
    if m:
        total = int(m.group(1).replace(",", ""))
    pagination = _SearchPaginationParser()
    pagination.feed(page_html)
    has_next = pagination.has_next

    items = []
    for chunk in re.split(r'<li class="item-card[\s"]', page_html)[1:]:
        head = chunk.split(">", 1)[0]
        attrs = {k: unescape(v) for k, v in ATTR_RE.findall(head)}
        if "product-id" not in attrs:
            continue

        title_m = re.search(r'item-card__title[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                            chunk, re.S)
        shop_m = re.search(r'item-card__shop-name">([^<]*)<', chunk)
        shop_url_m = re.search(r'item-card__shop-name-anchor[^>]*href="([^"]+)"', chunk)
        img_m = re.search(r'data-original="([^"]+)"', chunk)
        cat_m = re.search(r'item-card__category-anchor[^>]*href="([^"]+)"[^>]*>([^<]*)<', chunk)
        tags = [unescape(t) for t in
                re.findall(r'<img alt="([^"]+)"[^>]*src="[^"]*badges/', chunk)]
        is_adult = 'class="badge adult"' in chunk or ">R-18<" in chunk

        url = unescape(title_m.group(1)) if title_m else \
            f"{BASE}/ja/items/{attrs['product-id']}"
        items.append({
            "id": int(attrs["product-id"]),
            "name": clean_text(title_m.group(2)) if title_m else unescape(attrs.get("product-name", "")),
            "price": int(attrs["product-price"]) if attrs.get("product-price", "").isdigit() else None,
            "url": url,
            "shop": {
                "name": unescape(shop_m.group(1)) if shop_m else None,
                "subdomain": attrs.get("product-brand") or
                             (re.search(r"https?://([a-z0-9-]+)\.booth\.pm",
                                        shop_url_m.group(1) or "").group(1) if shop_url_m else None),
            },
            "category": unescape(cat_m.group(2)) if cat_m else None,
            "category_url": unescape(cat_m.group(1)) if cat_m else None,
            "tags": tags,
            "is_adult": is_adult,
            "image": original_image(unescape(img_m.group(1))) if img_m else None,
            "thumbnail": unescape(img_m.group(1)) if img_m else None,
        })
    return {"total": total, "items": items, "has_next": has_next}


# ---------------------------------------------------------------- 商品详情

def fetch_item(item_id, lang="ja", no_cache=False):
    """单品 JSON；被拒时先取 HTML 页拿 csrf 再重试。no_cache 跳过内容缓存。"""
    url = f"{BASE}/{lang}/items/{item_id}.json"
    ttl = 0 if no_cache else None
    try:
        return get_json(url, cache_ttl=ttl)
    except BoothError:
        page_html, _ = get_html(f"{BASE}/{lang}/items/{item_id}")
        csrf_m = re.search(r'name="csrf-token" content="([^"]+)"', page_html)
        return get_json(url, csrf=csrf_m.group(1) if csrf_m else None, cache_ttl=ttl)


def trim_item(raw, desc_len=DEFAULT_DESC_LEN):
    images = []
    original_images = []
    thumbnail = None
    for img in raw.get("images") or []:
        if (img or {}).get("original"):
            original_images.append(img["original"])
        if thumbnail is None:
            thumbnail = (img or {}).get("resized") or (img or {}).get("original") or None
        u = (img or {}).get("original") or (img or {}).get("resized")
        if u:
            images.append(u.replace("_base_resized", ""))
    variations = []
    for v in raw.get("variations") or []:
        name = " / ".join(str(v.get(f"field{i}")) for i in range(1, 7)
                          if v.get(f"field{i}"))
        variations.append({"name": name or v.get("name") or None,
                           "price": v.get("price"),
                           "status": v.get("status") or
                                     ("end_of_sale" if v.get("is_end_of_sale") else None)})
    shop = raw.get("shop") or {}
    cat = raw.get("category")
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "price": raw.get("price"),
        "url": raw.get("url") or f"{BASE}/ja/items/{raw.get('id')}",
        "shop": {
            "name": shop.get("name"),
            "subdomain": shop.get("subdomain"),
            "url": shop.get("url"),
        },
        "is_adult": raw.get("is_adult"),
        "is_vrchat": raw.get("is_vrchat"),
        "is_sold_out": raw.get("is_sold_out"),
        "is_end_of_sale": raw.get("is_end_of_sale"),
        "wish_lists_count": raw.get("wish_lists_count"),
        "published_at": raw.get("published_at"),
        "category": cat.get("name") if isinstance(cat, dict) else cat,
        "tags": [t.get("name") for t in raw.get("tags") or []],
        "images": images,
        "original_images": original_images,
        "thumbnail": thumbnail,
        "variations": variations,
        "description": (clean_text(raw.get("description")) if desc_len is None or desc_len < 0
                        else clean_text(raw.get("description"))[:desc_len]) or None,
    }


# ---------------------------------------------------------------- 商店页解析

def parse_shop_page(page_html):
    items = []
    for m in re.finditer(r'data-item="([^"]+)"', page_html):
        try:
            raw = json.loads(html_mod.unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
        shop = raw.get("shop") or {}
        thumbs = raw.get("thumbnail_image_urls") or []
        items.append({
            "id": raw.get("id"),
            "name": raw.get("name"),
            "price": raw.get("price"),
            "url": raw.get("shop_item_url") or raw.get("url"),
            "shop": {"name": shop.get("name"), "subdomain": shop.get("subdomain")},
            "is_adult": raw.get("is_adult"),
            "is_sold_out": raw.get("is_sold_out"),
            "is_vrchat": raw.get("is_vrchat"),
            "image": original_image(thumbs[0]) if thumbs else None,
            "thumbnail": thumbs[0] if thumbs else None,
        })
    return items


# ---------------------------------------------------------------- 命令

# 关注清单（watch）与已购清单（own）：本地 sqlite 快照 + 变动检查。
# 分工：own=已购素材库（本地个人数据，CLI 专属——bot 是多用户服务不持有个人已购）；
# watch=关注等降价（check 查价格/补货）；own 的 check 偏重商品更新（对应
# MioVRCA 的「Booth 更新检查」——已购作者发新版可免费重下）。
# 数据只存本机（BOOTH_WISH_DB 可覆盖路径）；出站仍走 http_get 的 booth.pm
# 白名单与共享请求预算，无新增外部端点。
WISH_DB_PATH = Path(os.environ.get("BOOTH_WISH_DB",
                                   str(Path.home() / ".booth-cli" / "wishlist.sqlite3")))
WATCH_CHECK_LIMIT = int(os.environ.get("BOOTH_WATCH_CHECK_LIMIT", "30") or 30)
_WISH_CONN = None  # 惰性；False 表示不可用


def _wish_db():
    global _WISH_CONN
    if _WISH_CONN is None:
        try:
            WISH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(WISH_DB_PATH), timeout=2)
            # 单表 + kind 数据列（watch=关注等降价 / own=已购素材库）：
            # kind 全程作为 execute 参数绑定，SQL 结构为固定字符串
            conn.execute("CREATE TABLE IF NOT EXISTS library ("
                         "kind TEXT NOT NULL, id INTEGER NOT NULL, name TEXT, "
                         "price INTEGER, is_sold_out INTEGER, updated_at TEXT, "
                         "added_at REAL, last_checked REAL, "
                         "PRIMARY KEY (kind, id))")
            try:  # 迁移旧版单 watch 表（一次性，迁完即删防复活）
                conn.execute("INSERT OR IGNORE INTO library "
                             "(kind, id, name, price, is_sold_out, updated_at, added_at, last_checked) "
                             "SELECT 'watch', id, name, price, is_sold_out, updated_at, added_at, last_checked "
                             "FROM watch")
                conn.execute("DROP TABLE watch")
            except sqlite3.Error:
                pass
            _WISH_CONN = conn
        except Exception:
            _WISH_CONN = False
    return _WISH_CONN if _WISH_CONN else None


def _library_ids(kind: str) -> set:
    """清单 ID 集合；存储不可用时返回空集（功能静默降级，不影响搜索）。"""
    conn = _wish_db()
    if not conn:
        return set()
    try:
        return {r[0] for r in conn.execute(
            "SELECT id FROM library WHERE kind = ?", (kind,))}
    except sqlite3.Error:
        return set()


def wish_ids():
    """已关注商品 ID 集合。"""
    return _library_ids("watch")


def own_ids():
    """已购商品 ID 集合（本地素材库标记，CLI 专属）。"""
    return _library_ids("own")


def _wish_snapshot(raw):
    shop = raw.get("shop") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or shop.get("name") or "",
        "price": raw.get("price"),
        "is_sold_out": 1 if raw.get("is_sold_out") else 0,
        "updated_at": raw.get("updated_at"),
    }


def _wish_changes(old_row, snap):
    """对比快照与库内记录（old_row: id,name,price,is_sold_out,updated_at），返回变动描述。"""
    changes = []
    if old_row is None:
        return changes
    if snap["price"] is not None and old_row[2] is not None \
            and snap["price"] != old_row[2]:
        arrow = "↓" if snap["price"] < old_row[2] else "↑"
        changes.append(f"价格 {old_row[2]:,} → {snap['price']:,} 円（{arrow}）")
    if bool(snap["is_sold_out"]) != bool(old_row[3]):
        changes.append("已补货上架" if not snap["is_sold_out"] else "已售罄")
    if snap["updated_at"] and old_row[4] and snap["updated_at"] != old_row[4]:
        changes.append(f"商品有更新（{snap['updated_at'][:10]}）")
    if snap["name"] and old_row[1] and snap["name"] != old_row[1]:
        changes.append(f"改名：{old_row[1]} → {snap['name']}")
    return changes


def _cmd_library(args, kind: str):
    """watch/own 共用实现：kind 为 'watch'（关注等降价）或 'own'（已购素材库）。"""
    op = (getattr(args, "op", None) or "list").strip().lower()
    label = "已购" if kind == "own" else "关注"
    icon = "🛒" if kind == "own" else "♥"
    conn = _wish_db()
    if not conn:
        raise BoothError(f"{label}清单存储不可用: {WISH_DB_PATH}")
    ids_arg = [parse_item_id(x) for x in (getattr(args, "ids", None) or [])]

    if op in ("add", "rm", "remove", "del"):
        removing = op != "add"
        if not ids_arg:
            raise BoothError(f"用法: booth {'own' if kind == 'own' else 'watch'} "
                             f"{'remove' if removing else 'add'} <商品ID|URL>...")
        results = []
        for iid in ids_arg:
            if removing:
                cur = conn.execute("DELETE FROM library WHERE kind = ? AND id = ?",
                                   (kind, int(iid)))
                results.append({"id": int(iid), "removed": cur.rowcount > 0})
                continue
            old = conn.execute("SELECT id, name, price, is_sold_out, updated_at "
                               "FROM library WHERE kind = ? AND id = ?",
                               (kind, int(iid))).fetchone()
            raw = fetch_item(iid, "ja")  # 快照取详情（走缓存）
            snap = _wish_snapshot(raw)
            conn.execute("INSERT OR REPLACE INTO library "
                         "(kind, id, name, price, is_sold_out, updated_at, added_at, last_checked) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (kind, snap["id"], snap["name"], snap["price"], snap["is_sold_out"],
                          snap["updated_at"], time.time(), time.time()))
            results.append({"id": snap["id"], "name": snap["name"],
                            "price": snap["price"], "already": old is not None})
        conn.commit()
        if args.json:
            print(json.dumps({"op": "remove" if removing else "add", "kind": kind,
                              "count": len(results), "results": results},
                             ensure_ascii=False, indent=2))
            return
        for r in results:
            if removing:
                print(f"#{r['id']} {'已移除' if r['removed'] else f'不在{label}清单中'}")
            else:
                tag = f"（已在清单，快照已刷新）" if r["already"] else ""
                price = f"¥{r['price']:,}" if isinstance(r["price"], int) else "价格未知"
                print(f"{icon} {label} #{r['id']}  {price}  {r['name']}{tag}")
        return

    if op == "list":
        rows = conn.execute("SELECT id, name, price, is_sold_out, updated_at, added_at, "
                            "last_checked FROM library WHERE kind = ? "
                            "ORDER BY added_at DESC", (kind,)).fetchall()
        items = [{"id": r[0], "name": r[1], "price": r[2],
                  "is_sold_out": bool(r[3]), "updated_at": r[4],
                  "added_at": r[5], "last_checked": r[6]} for r in rows]
        if args.json:
            print(json.dumps({"op": "list", "kind": kind, "count": len(items),
                              "items": items}, ensure_ascii=False, indent=2))
            return
        if not items:
            print(f"{label}清单为空。用法: booth {'own' if kind == 'own' else 'watch'} add <商品ID|URL>")
            return
        print(f"{label}清单 {len(items)} 件：")
        for it in items:
            price = f"¥{it['price']:,}" if isinstance(it["price"], int) else "价格未知"
            sold = "  [已售罄]" if it["is_sold_out"] else ""
            print(f"{icon} #{it['id']}  {price}{sold}  {it['name']}")
        return

    if op == "check":
        rows = conn.execute("SELECT id, name, price, is_sold_out, updated_at, added_at, "
                            "last_checked FROM library WHERE kind = ? "
                            "ORDER BY last_checked ASC", (kind,)).fetchall()
        if not rows:
            if args.json:
                print(json.dumps({"op": "check", "kind": kind, "checked": 0,
                                  "changed": [], "errors": []}, ensure_ascii=False, indent=2))
            else:
                print(f"{label}清单为空，无需检查。")
            return
        rows = rows[:max(1, WATCH_CHECK_LIMIT)]  # 单轮出站上限（每项 ≥1s 限速）
        changed, errors = [], []
        for r in rows:
            try:
                raw = fetch_item(str(r[0]), "ja", no_cache=True)
                snap = _wish_snapshot(raw)
                diffs = _wish_changes(r, snap)
                conn.execute("UPDATE library SET name=?, price=?, is_sold_out=?, "
                             "updated_at=?, last_checked=? WHERE kind=? AND id=?",
                             (snap["name"], snap["price"], snap["is_sold_out"],
                              snap["updated_at"], time.time(), kind, snap["id"]))
                if diffs:
                    changed.append({"id": snap["id"], "name": snap["name"],
                                    "price": snap["price"], "changes": diffs})
            except BoothError as e:
                errors.append({"id": r[0], "error": str(e)[:120]})
        conn.commit()
        if args.json:
            print(json.dumps({"op": "check", "kind": kind, "checked": len(rows),
                              "changed": changed, "errors": errors,
                              "limit": WATCH_CHECK_LIMIT},
                             ensure_ascii=False, indent=2))
            return
        if not changed and not errors:
            if kind == "own":
                print(f"已检查 {len(rows)} 件已购商品，暂无更新。")
            else:
                print(f"已检查 {len(rows)} 件关注商品，暂无变动。")
        for c in changed:
            price = f"¥{c['price']:,}" if isinstance(c["price"], int) else ""
            head_icon = "🔄" if kind == "own" else "🔎"
            print(f"{head_icon} #{c['id']}  {c['name']}  {price}")
            for d in c["changes"]:
                print(f"    {d}")
        for e in errors:
            print(f"⚠ #{e['id']} 检查失败: {e['error']}")
        return

    raise BoothError(f"未知子命令: {op!r}（支持 add / remove / list / check）")


def cmd_watch(args):
    _cmd_library(args, "watch")


def cmd_own(args):
    _cmd_library(args, "own")


def _mark_watched(items):
    """给搜索结果打本地清单标记（watched=♥关注 / owned=🛒已购；不改变排序）。"""
    watching, owned = wish_ids(), own_ids()
    if not watching and not owned:
        return
    for it in items:
        if it.get("id") in watching:
            it["watched"] = True
        if it.get("id") in owned:
            it["owned"] = True


def build_search_url(args):
    lang = args.lang
    query = " ".join(args.query or []).strip()
    if args.category:
        path = f"/{lang}/browse/{urllib.parse.quote(args.category, safe='')}"
    elif args.event:
        path = f"/{lang}/events/{urllib.parse.quote(args.event, safe='')}"
    elif query:
        path = f"/{lang}/search/{urllib.parse.quote(query, safe='')}"
    else:
        path = f"/{lang}/items"
    params = {}
    if query and (args.category or args.event):
        params["q"] = query
    if args.sort != "popularity":
        params["sort"] = args.sort
    if args.type != "all":
        params["type"] = args.type
    if args.adult == "include":
        params["adult"] = "include"
    elif args.adult == "only":
        params["adult"] = "only"
    if args.in_stock:
        params["in_stock"] = "true"
    if args.min_price is not None:
        params["min_price"] = str(args.min_price)
    if args.max_price is not None:
        params["max_price"] = str(args.max_price)
    tags = list(args.tag or [])
    if getattr(args, "vrc", False):
        tags.append("VRChat")  # VRC 对口默认收窄（--no-vrc 关闭）
    seen_tags = set()
    for t in tags:  # 去重保序（bot 显式传 tag 时避免 VRChat 重复）
        if t and t not in seen_tags:
            seen_tags.add(t)
            params.setdefault("tags[]", []).append(t)
    for w in args.or_word or []:
        params.setdefault("or_words[]", []).append(w)
    for w in args.exclude or []:
        params.setdefault("except_words[]", []).append(w)
    if args.page > 1:
        params["page"] = str(args.page)
    qs = urllib.parse.urlencode(params, doseq=True)
    return BASE + path + (f"?{qs}" if qs else "")


def effective_sort(sort: str, page: int) -> tuple:
    """返回 (实际排序, 标注)。Booth 在 popularity 排序下忽略 page 参数（站点行为），
    翻页时自动改按新着排序以保证翻页有效。"""
    if page > 1 and sort == "popularity":
        return "new", "（翻页按新着排序）"
    return sort, ""


def cmd_search(args):
    start_page = args.page
    sort, sort_note = effective_sort(args.sort, max(start_page, 2) if args.pages > 1 else start_page)
    args.sort = sort
    items, total, has_next = [], None, False
    seen = set()
    fetched = 0
    for offset in range(args.pages):
        args.page = start_page + offset
        url = build_search_url(args)
        page_html, final_url = get_html(url)
        result = parse_search_page(page_html)
        if not result["items"] and result["total"] != 0 and "item-card" not in page_html:
            raise BoothError(f"搜索页无结果或结构异常: {final_url}（语言 {args.lang} 可能不渲染列表，试试 --lang ja）")
        if total is None:
            total = result["total"]
        has_next = result["has_next"]
        fetched += 1
        for item in result["items"]:
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        if len(items) >= args.limit:
            break
        if not has_next:
            break
        if offset < args.pages - 1:
            time.sleep(PAGE_DELAY)
    items = items[:args.limit]
    _mark_watched(items)

    if args.json:
        print(json.dumps({
            "query": " ".join(args.query or []).strip(), "page": args.page, "pages_fetched": fetched,
            "total": total, "count": len(items), "has_next": has_next,
            "sort_note": sort_note or None,
            "items": items,
        }, ensure_ascii=False, indent=2))
    else:
        if total is not None:
            print(f"共 {total:,} 件，本次显示 {len(items)} 件{sort_note}\n")
        for it in items:
            price = f"¥{it['price']:,}" if it["price"] is not None else "价格未知"
            flags = " ".join(filter(None, ["R-18" if it.get("is_adult") else "",
                                           "🛒已购" if it.get("owned") else "",
                                           "♥已关注" if it.get("watched") else ""]))
            meta = f"[{it['category']}]" if it.get("category") else ""
            tags = "#" + " #".join(it["tags"]) if it["tags"] else ""
            print(f"#{it['id']}  {price}  {it['shop']['name'] or ''}({it['shop']['subdomain'] or ''})"
                  f"  {flags}  {meta}  {tags}")
            print(f"    {it['name']}")
            print(f"    {it['url']}")


def cmd_item(args):
    item_id = parse_item_id(" ".join(args.id))
    raw = fetch_item(item_id, "ja")
    payload = raw if args.full else trim_item(raw, desc_len=args.desc_len)
    translate_note = ""
    if getattr(args, "translate", False):
        import smart_search
        backend = smart_search.ai_backend()
        if not backend:
            translate_note = "⚠ 未配置 AI（AI_API_KEY 等），跳过翻译"
        else:
            try:
                zh = smart_search.translate_item_info(
                    payload.get("name") or "",
                    clean_text(raw.get("description"))[:1200], **backend)
                payload["name_zh"] = zh["name_zh"] or None
                payload["desc_zh"] = zh["desc_zh"] or None
            except Exception as e:
                import smart_search as _s
                translate_note = f"⚠ 翻译失败：{_s.friendly_ai_error(e)}"
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    it = trim_item(raw, desc_len=len(clean_text(raw.get("description")))) if args.full else payload
    print(f"#{it['id']}  {it['price']}  {it['shop'].get('name') or ''}({it['shop'].get('subdomain') or ''})")
    print(f"标题: {it['name']}")
    if it.get("name_zh"):
        print(f"中文: {it['name_zh']}")
    if it.get("desc_zh"):
        print(f"简介(译): {it['desc_zh']}")
    flags = []
    if it.get("is_adult"):
        flags.append("R-18")
    if it.get("is_sold_out"):
        flags.append("已售罄")
    if it.get("is_end_of_sale"):
        flags.append("已停售")
    if flags:
        print("状态:", " ".join(flags))
    if it.get("wish_lists_count") is not None:
        print(f"收藏: {it['wish_lists_count']}  上架: {it.get('published_at')}")
    if it.get("category"):
        print(f"分类: {it['category']}")
    if it.get("tags"):
        print("标签:", " ".join("#" + t for t in it["tags"] if t))
    if it.get("variations"):
        print("规格:")
        for v in it["variations"]:
            print(f"  - {v['name'] or '(默认)'}  {v['price']}  {v['status'] or ''}")
    if it.get("description"):
        print(f"简介: {it['description']}")
    print("URL:", it["url"])
    if it.get("images"):
        print("图片:")
        for u in it["images"][:6]:
            print("  ", u)


def cmd_shop(args):
    sub = parse_shop_subdomain(" ".join(args.shop))
    items_all = []
    shop_name = None
    for p in range(1, args.pages + 1):
        url = f"https://{sub}.booth.pm/items" + (f"?page={p}" if p > 1 else "")
        page_html, _ = get_html(url)
        title_m = re.search(r"<title>([^<]*)</title>", page_html)
        if shop_name is None and title_m:
            shop_name = re.sub(r"\s*[-|]\s*BOOTH\s*$", "", unescape(title_m.group(1)))
        got = parse_shop_page(page_html)
        items_all.extend(got)
        if not got:
            break
        if args.pages > 1 and p < args.pages:
            time.sleep(PAGE_DELAY)

    info = {"subdomain": sub, "name": shop_name, "url": f"https://{sub}.booth.pm/"}
    if args.json:
        print(json.dumps({"shop": info, "count": len(items_all), "items": items_all},
                         ensure_ascii=False, indent=2))
        return
    print(f"{info['name'] or sub}  ({info['url']})")
    print(f"商店页服务端渲染出的最新商品 {len(items_all)} 件"
          f"（多数店铺整页由前端加载，此处只能取到少量；找该店商品可用 booth search \"店铺名\"）\n")
    for it in items_all:
        price = f"¥{it['price']:,}" if isinstance(it["price"], int) else (it["price"] or "")
        flags = "R-18" if it.get("is_adult") else ""
        vrc = "[VRChat]" if it.get("is_vrchat") else ""
        print(f"#{it['id']}  {price}  {flags} {vrc} {it['name']}")
        print(f"    {it['url']}")


# ---------------------------------------------------------------- 智能搜索（VRC 对口）
# 默认仅做本地词扩展与站内检索。--delegate-ai 显式选择可选模型规划/评估。
# 工作流中的当前 AI 应使用 workflow，直接提供检索词并读取来源资料。

_SMART_WORKERS = 3           # 并发工作线程（出站仍受全局限速约束）


def _search_once(term, base_args):
    """按单词级检索词发一次站内搜索，返回 parse_search_page 结果。"""
    ns = argparse.Namespace(**vars(base_args))
    ns.query = [term]
    ns.category = None
    ns.event = None
    # Reuse all candidates already present on this page; details stay bounded.
    # smart 解析器没有的 search 字段补默认值（build_search_url 需要全量字段）
    ns.lang = getattr(ns, "lang", "ja")
    ns.type = getattr(ns, "type", "all")
    ns.tag = getattr(ns, "tag", None)
    ns.or_word = getattr(ns, "or_word", None)
    ns.exclude = getattr(ns, "exclude", None)
    ns.in_stock = getattr(ns, "in_stock", False)
    ns.min_price = getattr(ns, "min_price", None)
    ns.max_price = getattr(ns, "max_price", None)
    url = build_search_url(ns)
    page_html, final_url = get_html(url)
    return parse_search_page(page_html)


def _merged_search(terms, base_args, via="关键词"):
    """单词级多路并发搜索合并：去重 + 标题含词置顶（bot 侧 _search_merged 同款）。
    返回 (merged_items, first_result, last_error)。单个词失败跳过。"""
    from concurrent.futures import ThreadPoolExecutor
    merged, seen, first_res, last_err = [], set(), None, None
    successes, failures, has_next = 0, 0, False

    def _one(term):
        try:
            return _search_once(term, base_args)
        except BoothError as e:
            return e

    with ThreadPoolExecutor(max_workers=_SMART_WORKERS) as pool:
        for res in pool.map(_one, terms[:6]):
            if isinstance(res, BoothError):
                last_err = res
                failures += 1
                continue
            successes += 1
            has_next = has_next or bool(res.get("has_next"))
            if first_res is None:
                first_res = res
            for it in res["items"]:
                if it["id"] not in seen:
                    it["via"] = via
                    seen.add(it["id"])
                    merged.append(it)
    low = [t.lower() for t in terms if t]
    if low:
        # 命中更多检索词的优先；同分内按首个命中词序位（首选词的命中优先于尾部词的子串噪音）
        def _rank(it):
            name = (it.get("name") or "").lower()
            hit_idx = [i for i, t in enumerate(low) if t in name]
            return (-len(hit_idx), hit_idx[0] if hit_idx else len(low))

        merged.sort(key=_rank)
    if first_res is not None:
        first_res = dict(first_res, _search_successes=successes,
                         _search_failures=failures, has_next=has_next)
    return merged, first_res, last_err


def _enrich_details(entries, desc_len=DEFAULT_DESC_LEN):
    """并发拉商品详情：补收藏数/真实 tags/上架日期，简介存 _desc（描述核实用）。
    并发受限（出站仍受全局 1s 限速），单条失败静默跳过。"""
    from concurrent.futures import ThreadPoolExecutor

    def _one(it):
        if it.get("detail_status") in ("available", "unavailable"):
            return
        try:
            detail = trim_item(fetch_item(str(it["id"]), "ja"), desc_len=desc_len)
        except BoothError:
            it["detail_status"] = "unavailable"
            return
        for k in ("wish_lists_count", "tags", "published_at", "category",
                  "is_adult", "is_vrchat", "is_sold_out", "is_end_of_sale",
                  "original_images", "thumbnail", "variations"):
            if detail.get(k) is not None:
                it[k] = detail[k]
        it["_desc"] = detail.get("description") or ""
        it["detail_status"] = "available"

    with ThreadPoolExecutor(max_workers=_SMART_WORKERS) as pool:
        list(pool.map(_one, entries))


def item_matches_policy(item, adult="include", tag=None):
    """详情筛选：未知字段不作为要求已满足的证据。"""
    if adult == "exclude" and item.get("is_adult") is not False:
        return False
    if adult == "only" and item.get("is_adult") is not True:
        return False
    if tag:
        tags = [t.get("name") if isinstance(t, dict) else t for t in item.get("tags") or []]
        if not any(str(t).casefold() == tag.casefold() for t in tags if t):
            return tag.casefold() == "vrchat" and item.get("is_vrchat") is True
    return True

def cmd_workflow(args):
    """Execute caller-provided search axes without importing any AI provider."""
    import agent_workflow
    if args.schema:
        print(json.dumps(agent_workflow.contract(), ensure_ascii=False, indent=2))
        return
    try:
        query, terms = agent_workflow.search_plan(args.query, args.keyword,
                                                args.limit, args.page, args.desc_len,
                                                args.require_term)
    except ValueError as exc:
        raise BoothError(str(exc)) from None
    if request_budget.context() is None:
        with request_budget.query_context({"request_id": uuid.uuid4().hex,
                "max_requests": 12, "deadline": time.time() + 180}):
            return cmd_workflow(args)
    args.sort, sort_note = effective_sort(args.sort, args.page)
    merged, first_res, last_err = _merged_search(terms, args, via="caller")
    if first_res is None:
        raise last_err or BoothError("工作流检索未能完成")
    candidates = merged[:args.limit]
    _enrich_details(candidates, desc_len=-1)
    print(json.dumps(agent_workflow.result(query, terms, candidates,
        candidate_count=len(merged), search_info=first_res,
        required_terms=args.require_term or [], desc_len=args.desc_len,
        sort_note=sort_note), ensure_ascii=False, indent=2))


def cmd_smart(args):
    import smart_search
    if request_budget.context() is None:
        with request_budget.query_context({"request_id": uuid.uuid4().hex,
                "max_requests": 12, "deadline": time.time() + 180}):
            return cmd_smart(args)

    query = " ".join(args.query or []).strip()
    if not query:
        raise BoothError("smart 需要需求描述，如: booth smart 适用于Rexouium素体的服装")
    if smart_search.pykakasi is None:
        raise BoothError(smart_search.PYKAKASI_HINT)
    sort, sort_note = effective_sort(args.sort, args.page)
    args.sort = sort

    # 1) 委托必须显式选择；继承了 API 环境变量也不会自动调用另一模型。
    def _try_plan(bk):
        kws, dkws, translated = smart_search.plan_search(query, **bk)
        return kws, dkws, ("plan-translated" if translated else "plan")

    mode, kws_ai, desc_kws, ai_note = "direct", [], [], ""
    backend = smart_search.ai_backend() if getattr(args, "delegate_ai", False) and not args.no_ai else None
    if getattr(args, "delegate_ai", False) and not backend:
        ai_note = "未配置可用的委托 AI，已使用本地词扩展检索"
    if backend:
        try:
            kws_ai, desc_kws, mode = _try_plan(backend)
        except Exception as main_err:
            fb = smart_search.ai_fallback_backend()
            if fb and fb != backend:
                try:
                    kws_ai, desc_kws, mode = _try_plan(fb)
                    backend = fb
                except Exception as fb_err:
                    ai_note = f"⚠ AI 方案规划不可用：{smart_search.friendly_ai_error(fb_err)}（已用原词检索）"
            else:
                ai_note = f"⚠ AI 方案规划不可用：{smart_search.friendly_ai_error(main_err)}（已用原词检索）"
            if ai_note:
                kws_ai, desc_kws = [], []

    # 2) 检索词：AI 词 → 同义词种子 + 变体扩展；无 AI 词时对原词朴素分词
    if kws_ai:
        kws_ai = smart_search.apply_industry_synonyms(query, kws_ai)
        kws = smart_search.expand_reading_variants(kws_ai)
        terms = smart_search.build_search_terms(kws)
    else:
        direct = [t for t in re.split(r"[\s/、，,]+", query) if len(t.strip()) >= 2] or [query]
        kws = smart_search.expand_reading_variants(smart_search.apply_industry_synonyms(query, direct))
        terms = smart_search.build_search_terms(kws)

    # 3) 分词合并搜索 → 有上限的完整详情 → 来源证据评估 → 可选二轮
    used_terms = search_evidence.retrieval_terms(query, terms, smart_search.INDUSTRY_SYNONYMS) or [query]
    merged, first_res, last_err = _merged_search(used_terms, args)
    merged = search_evidence.rank_target(merged, query, smart_search.INDUSTRY_SYNONYMS, desc_kws)
    if not merged and terms and search_evidence.normalized(query) not in {
            search_evidence.normalized(term) for term in used_terms}:
        # 方案词无果，退回原词直搜
        merged, first_res2, last_err = _merged_search([query], args)
        if merged and first_res2 and first_res2.get("total"):
            first_res = first_res2
    eval_note = ""
    _enrich_details(merged[:min(args.limit, 6)], desc_len=-1)
    if merged and backend:
        try:
            titles = search_evidence.candidate_lines(merged[:12], desc_kws or used_terms)
            ev = smart_search.evaluate_results(query, kws_ai or used_terms,
                                               titles, **backend)
            ev = smart_search.validate_evaluation(ev, titles)
            ev = search_evidence.grounded_evaluation(ev, merged[:12], desc_kws,
                target_query=search_evidence.literal_query(query, smart_search.INDUSTRY_SYNONYMS))
            print(f"[eval] 第一轮: {ev.get('verdict')} {ev.get('reason')}",
                  file=sys.stderr)
            # 保守 retry：评估员放行但标题命中率过低时仍触发二轮
            need_retry = (ev.get("verdict") == "retry" and bool(ev.get("keywords"))) or \
                smart_search.conservative_retry(ev.get("verdict", ""),
                                                [it.get("name") or "" for it in merged[:6]],
                                                used_terms)
            if need_retry:
                request_budget.context()["max_requests"] = 18
                print("[eval] 第一轮结果不理想，执行第二轮搜索…", file=sys.stderr)
                if ev.get("keywords"):
                    terms2 = smart_search.build_search_terms(smart_search.expand_reading_variants(
                        smart_search.apply_industry_synonyms(query, ev["keywords"])))
                else:
                    try:
                        kws2, _, _ = smart_search.plan_search(
                            query, feedback=f"关键词 {used_terms[:4]} 无效（{ev.get('reason') or '候选不相关'}）",
                            **backend)
                        terms2 = smart_search.build_search_terms(smart_search.expand_reading_variants(
                            smart_search.apply_industry_synonyms(query, kws2)))
                    except Exception as e2:
                        print(f"[eval] 二轮重新规划失败: {e2}", file=sys.stderr)
                        terms2 = []
                terms2 = search_evidence.retrieval_terms(query, terms2, smart_search.INDUSTRY_SYNONYMS,
                                                       previous=used_terms, limit=3)
                if terms2:
                    merged2, first_res2, _ = _merged_search(terms2, args)
                    if merged2:
                        merged = search_evidence.merge_rounds(merged, merged2, query,
                                                             smart_search.INDUSTRY_SYNONYMS, desc_kws)
                        if first_res2 and first_res2.get("total"):
                            first_res = first_res2
                        used_terms = list(dict.fromkeys(used_terms + terms2))
                        try:
                            fresh = [it for it in merged if not it.get("detail_status")][:3]
                            _enrich_details(fresh, desc_len=-1)
                            titles2 = search_evidence.candidate_lines(merged[:12], desc_kws or used_terms)
                            ev2 = smart_search.evaluate_results(query, terms2,
                                                                titles2, **backend)
                            ev2 = smart_search.validate_evaluation(ev2, titles2)
                            ev2 = search_evidence.grounded_evaluation(ev2, merged[:12], desc_kws,
                                target_query=search_evidence.literal_query(query, smart_search.INDUSTRY_SYNONYMS))
                            eval_note = ("第二轮找到至少三条有来源证据的相关候选（适配以商品说明为准）"
                                         if ev2.get("verdict") == "ok" else
                                         "两轮搜索后仍未完全确认，以下为最接近的结果")
                        except Exception as e2:
                            print(f"[eval] 第二轮评估失败: {e2}", file=sys.stderr)
                            eval_note = "第二轮相关性评估暂不可用，以下候选尚未核实"
                else:
                    eval_note = "未得到新的有效检索词，以下候选尚未完全核实"
        except Exception as e:
            print(f"[eval] 结果评估失败（按第一轮返回）: {e}", file=sys.stderr)
            eval_note = "相关性评估暂不可用，以下候选尚未核实"
    web_note = ""
    if not merged and not args.no_webfind:
        import os
        ids = smart_search.find_booth_item_ids(
            (kws_ai or [query])[0],
            exa_api_key=os.environ.get("EXA_API_KEY", "").strip(),
            exa_base_url=os.environ.get("EXA_BASE_URL", "").strip() or "https://api.exa.ai",
            search_api_key=os.environ.get("SEARCH_API_KEY", "").strip(),
            search_base_url=os.environ.get("SEARCH_BASE_URL", "").strip(),
            search_provider=os.environ.get("SEARCH_PROVIDER", "auto").strip(),
            ddg_enabled=os.environ.get("SEARCH_DDG_ENABLED", "true").strip().lower()
                        not in ("0", "false", "no", "off"))
        if ids:
            for iid in ids[:3]:
                try:
                    it = trim_item(fetch_item(str(iid), "ja"), desc_len=200)
                except BoothError:
                    continue
                required_tags = list(getattr(args, "tag", None) or []) + (["VRChat"] if args.vrc else [])
                if not item_matches_policy(it, args.adult) or any(
                        not item_matches_policy(it, "include", tag) for tag in required_tags):
                    continue
                it["via"] = "网络检索"
                merged.append(it)
            mode = (mode + "+webfind").lstrip("+")
            web_note = "（站内搜索无果，以下为网络检索命中，供参考）"
    if not merged:
        if last_err:
            raise BoothError(f"smart 搜索失败: {last_err}")
        msg = f"Booth 上没搜到「{query}」"
        if args.json:
            print(json.dumps({"query": query, "mode": mode, "ai_keywords": kws_ai,
                              "keywords": terms, "desc_keywords": desc_kws,
                              "ai_note": ai_note or None, "total": 0, "count": 0,
                              "items": []}, ensure_ascii=False, indent=2))
            return
        print(msg + (f"\n{ai_note}" if ai_note else ""))
        return

    # 4) 描述核实：拉详情（简介扩长），说明文/标题含核实词的置顶并注明
    total = first_res.get("total") if first_res else None
    has_next = first_res.get("has_next") if first_res else False
    desc_note = None
    if desc_kws:
        merged = smart_search.desc_boost(merged, terms + kws_ai, desc_kws)
        n_hit = sum(1 for it in merged if smart_search.description_status(it, desc_kws) == "mentioned")
        desc_note = (f"商品说明中提及「{' / '.join(desc_kws[:2])}」:{n_hit} 件（未确认兼容性）"
                     if n_hit else "商品说明未提供可确认的兼容性证据，按关键词相关度展示")
    for item in merged:
        item.setdefault("relevance_status", "unknown")
    merged, quality = search_evidence.select_results(merged, query, smart_search.INDUSTRY_SYNONYMS,
                                                   desc_kws, assessed=bool(backend), display_limit=args.limit)
    selection_note = (f"已隐藏 {quality['omitted']} 件缺乏相关证据或不满足要求的候选，结果不足时不补满"
                      if quality["omitted"] else "")
    items = [dict({k: v for k, v in it.items() if k != "_desc"},
                  description=it.get("_desc") or None)
             for it in merged[:args.limit]]
    _mark_watched(items)

    if args.json:
        print(json.dumps({
            "query": query, "mode": mode, "ai_keywords": kws_ai,
            "keywords": used_terms,
            "desc_keywords": desc_kws, "desc_note": desc_note,
            "eval_note": eval_note or None,
            "sort_note": sort_note or None, "ai_note": ai_note or None,
            "web_note": web_note or None,
            "selection_note": selection_note or None, "quality": quality,
            "total": total, "count": len(items), "has_next": has_next,
            "items": items,
        }, ensure_ascii=False, indent=2))
        return
    head = f"共 {total:,} 件，显示前 {len(items)} 件{sort_note}" if total else f"前 {len(items)} 件{sort_note}"
    if terms:
        head += f"\n检索词: {' / '.join(terms[:3])}"
    if eval_note:
        head += f"\n{eval_note}"
    if selection_note:
        head += f"\n{selection_note}"
    if desc_note:
        head += f"\n{desc_note}"
    if web_note:
        head += f"\n{web_note}"
    if ai_note:
        head += f"\n{ai_note}"
    print(head + "\n")
    for it in items:
        price = f"¥{it['price']:,}" if it["price"] is not None else "价格未知"
        flags = " ".join(filter(None, ["R-18" if it.get("is_adult") else "",
                                       "🛒已购" if it.get("owned") else "",
                                       "♥已关注" if it.get("watched") else ""]))
        via = f"[{it['via']}]" if it.get("via") else ""
        print(f"#{it['id']}  {price}  {it['shop']['name'] or ''}({it['shop']['subdomain'] or ''})"
              f"  {flags}  {via}")
        print(f"    {it['name']}")
        print(f"    {it['url']}")
        print("    " + search_evidence.evidence_label(it))


def cmd_imgsearch(args):
    if not getattr(args, "delegate_ai", False):
        raise BoothError("默认由当前 AI 读图并向 workflow 提交检索词；若当前模型不支持识图，"
                         "请仅使用文字搜索。独立图搜须显式 --delegate-ai 并连接经检测支持识图的多模态 API")
    import smart_search
    provider = smart_search.provider_api
    last = {"model": "未配置", "reason": "尚未配置可验证的多模态 API"}
    for backend in (smart_search.ai_backend(), smart_search.ai_fallback_backend()):
        if not backend:
            continue
        try:
            last = provider.image_capability(backend["base_url"], backend["api_key"],
                                            backend["model"], min(backend["timeout"], 15))
        except provider.ProviderError:
            last = {"model": backend["model"] or "自动模型", "reason": "模型能力检测暂不可用"}
        if last.get("state") == "supported":
            break
    else:
        raise BoothError(provider.image_notice(last))
    src = " ".join(args.image).strip()
    tmp_path = None
    if re.match(r"^https?://", src, re.I):
        data = download_image(src)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            tmp_path = tf.name
            try:
                tf.write(data)
            except Exception:
                tf.close()
                os.unlink(tmp_path)
                raise
        path = tmp_path
    else:
        path = src.strip('"')
        if not Path(path).is_file():
            raise BoothError(f"本地图片不存在: {path}（也支持 booth 官方图床的图片 URL）")

    try:
        _run_imgsearch(args, path, src)
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _run_imgsearch(args, path, src):
    engines = [e.strip() for e in args.engine.split(",") if e.strip()]
    ordered, via, derived = [], {}, ""
    for eng in engines:
        try:
            if eng == "bing":
                # 先走纯 HTTP 快路径（无浏览器、约 2 秒），失败回落 playwright
                try:
                    from reverse_search import bing_search_http
                    ids, derived, _ = bing_search_http(path)
                except ImportError:
                    raise
                except Exception as e_http:
                    from reverse_search import bing_search
                    print(f"警告: bing-http 失败({str(e_http)[:80]})，改用浏览器引擎",
                          file=sys.stderr)
                    ids, derived, _ = bing_search(path, wait_s=args.wait_s,
                                                  headless=args.headless)
            elif eng == "ascii2d":
                from reverse_search import ascii2d_search
                ids, _ = ascii2d_search(path, wait_s=args.wait_s,
                                        headless=args.headless)
            else:
                continue
        except ImportError:
            raise BoothError("浏览器备援引擎需要 playwright: "
                             "pip install playwright && playwright install chrome")
        except Exception as e:
            print(f"警告: {eng} 引擎失败: {str(e)[:120]}", file=sys.stderr)
            continue
        for iid in ids:
            if iid not in via:
                via[iid] = eng
                ordered.append(iid)
        print(f"[{eng}] 候选 {len(ids)} 个: {ids[:8]}", file=sys.stderr)
        # bing 即使无视觉直链，派生词也足以驱动关键词搜索，不必再试第二引擎
        if (ordered or derived) and eng == "bing":
            break

    if not ordered and derived:
        # 图搜没给 booth 直链时，用引擎派生的关键词兜底走关键词搜索
        kw = derived.strip()
        print(f"提示: 图搜无直链，改用派生词搜索: {kw}", file=sys.stderr)
        page_html, _ = get_html(
            BASE + "/ja/search/" + urllib.parse.quote(kw, safe="") + "?sort=popularity&adult=include")
        res = parse_search_page(page_html)
        if not res["items"]:
            # 全派生词搜不到（Bing 的 OCR 词常带括号注释），退回拉丁词元再试
            latin = " ".join(re.findall(
                r"[^\s\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+", derived)).strip()
            if latin and latin != kw:
                print(f"提示: 派生词无结果，改用拉丁词元: {latin}", file=sys.stderr)
                page_html, _ = get_html(
                    BASE + "/ja/search/" + urllib.parse.quote(latin, safe="")
                    + "?sort=popularity&adult=include")
                res = parse_search_page(page_html)
        ordered = [it["id"] for it in res["items"][:args.limit]]
        via = {i: "bing-query" for i in ordered}

    if ordered and derived:
        # 派生词常就是商品名（Bing 会读图内文字）——必做关键词合并，取非 CJK 词元避免语义稀释
        latin = " ".join(re.findall(r"[^\s\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+", derived)).strip()
        if latin:
            try:
                page_html, _ = get_html(
                    BASE + "/ja/search/" + urllib.parse.quote(latin, safe="") + "?sort=popularity&adult=include")
                res = parse_search_page(page_html)
                for it in res["items"]:
                    iid = it["id"]
                    if iid not in via:
                        via[iid] = "derived-search"
                        ordered.append(iid)
                print(f"[derived] 关键词合并 {len(res['items'])} 个: {latin!r}", file=sys.stderr)
            except BoothError as e:
                print(f"警告: 派生词搜索失败: {str(e)[:80]}", file=sys.stderr)

    # 排序：视觉命中前2 → 派生词关键词命中 → 其余视觉候选
    visual = [i for i in ordered if via.get(i, "").startswith("bing")]
    kw = [i for i in ordered if not via.get(i, "").startswith("bing")]
    ordered = visual[:2] + kw + visual[2:]

    if not ordered:
        raise BoothError("反向搜图未发现任何 booth 关联（引擎: " + ",".join(engines) + "）")

    matches = []
    for iid in ordered[:args.limit]:
        try:
            raw = fetch_item(str(iid), "ja")
            m = trim_item(raw, desc_len=200)
        except BoothError:
            m = {"id": iid, "name": None, "price": None, "url": f"{BASE}/ja/items/{iid}"}
        m["via"] = via.get(iid, "")
        matches.append(m)
        time.sleep(1.0)

    payload = {"image": src, "engines": engines, "derived_query": derived,
               "match_count": len(matches), "matches": matches}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"反向搜图命中 {len(matches)} 个候选" +
          (f"（引擎派生词: {derived}）" if derived else "") + "：\n")
    for m in matches:
        price = f"¥{m['price']:,}" if isinstance(m.get("price"), int) else (m.get("price") or "")
        print(f"#{m.get('id')}  {price}  {m.get('name') or '(详情获取失败)'}  [{m.get('via')}]")
        print(f"    {m.get('url')}")


# ---------------------------------------------------------------- bot 钩子
# 为 QQ bot 框架（NoneBot/Koishi/Yunzai/go-cqhttp 插件等）提供的稳定接入层：
# 子进程调用 `booth bot '<json>'`（或 stdin 管道），返回统一 JSON 信封，
# 永不抛栈、退出码恒为 0，ok 字段表达成败。详见 QQBOT.md。

BOT_ACTIONS = ("search", "item", "shop", "imgsearch", "smart", "workflow", "watch", "own")
_BOT_POSITIONAL = {"search": "query", "item": "id", "shop": "shop",
                   "imgsearch": "image", "smart": "query", "workflow": "query",
                   "watch": "op", "own": "op"}
# watch/own 的 ids 以多个位置参数形式追加（argparse 位置参数 ids nargs="*"）
_BOT_EXTRA_POSITIONAL = {"watch": ("ids",), "own": ("ids",)}
_BOT_LIST_FLAGS = ("tag", "or_word", "exclude", "keyword", "require_term")
_BOT_BOOL_FLAGS = ("vrc", "no_vrc", "in_stock", "full", "headless", "no_cache",
                   "no_ai", "no_webfind", "delegate_ai", "schema", "translate")


def bot_params_to_argv(action, params):
    """把信封里的 params（snake_case，与 CLI 旗标同名）转成子命令 argv。"""
    argv = [action]
    pos = params.get(_BOT_POSITIONAL[action])
    if pos is not None and not isinstance(pos, (str, list)):
        pos = str(pos)
    if isinstance(pos, str):
        argv.append(pos)
    elif isinstance(pos, list):
        argv.extend(str(x) for x in pos)
    for extra in _BOT_EXTRA_POSITIONAL.get(action, ()):  # 如 watch 的 ids[]
        vals = params.get(extra)
        if vals is None:
            continue
        argv.extend(str(x) for x in (vals if isinstance(vals, list) else [vals]))
    for key, value in params.items():
        if key in (_BOT_POSITIONAL[action], "action", "json", "params") \
                or key in _BOT_EXTRA_POSITIONAL.get(action, ()):
            continue
        flag = "--" + key.replace("_", "-")
        if key in _BOT_BOOL_FLAGS:
            if not isinstance(value, bool):
                raise BoothError(f"{key} 必须是 JSON 布尔值 true/false")
            if value:
                argv.append(flag)
        elif isinstance(value, list) or key in _BOT_LIST_FLAGS:
            items = value if isinstance(value, list) else [value]
            for one in items:
                argv.extend([flag, str(one)])
        elif value is not None:
            argv.extend([flag, str(value)])
    return argv


def _bot_envelope(ok, action, data=None, error=None, budget=None):
    payload = {"ok": ok, "action": action}
    if ok:
        payload["data"] = data
    else:
        payload["error"] = error
    if budget is not None:
        payload["request_budget"] = budget
    return json.dumps(payload, ensure_ascii=False)


def cmd_bot(args):
    action = "?"
    budget = None
    raw = (args.payload or "").strip()
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    try:
        req = json.loads(raw) if raw else {}
        if not isinstance(req, dict):
            raise BoothError("payload 必须是 JSON 对象，如 {\"action\":\"search\",\"params\":{...}}")
        action = str(req.get("action", "")).strip()
        if action == "version":
            print(_bot_envelope(True, "version", {"version": __version__,
                "capabilities": ["shared_request_budget", "search_evidence", "verified_image_input", "generic_ai", "pluggable_web_search", "caller_workflow", "explicit_ai_delegation"],
                "ai_execution": {"default": "caller", "delegation": "explicit_opt_in"},
                "semantic_fingerprint": request_budget.semantic_fingerprint()}))
            return
        if action not in BOT_ACTIONS:
            raise BoothError(f"未知 action: {action!r}（支持 {list(BOT_ACTIONS)} 与 version）")
        extra = req.get("params") if isinstance(req.get("params"), dict) else {}
        flat = {k: v for k, v in req.items() if k not in ("action", "params", "context")}
        params = {**flat, **extra}
        if action == "workflow":
            import agent_workflow
            agent_workflow.validate_params(params)

        argv = bot_params_to_argv(action, params) + ["--json"]
        err_buf, out_buf = io.StringIO(), io.StringIO()
        exit_code = 0
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                ns = build_parser().parse_args(argv)
                previous_cache = _NO_CACHE
                try:
                    set_cache_enabled(not getattr(ns, "no_cache", False))
                    with request_budget.query_context(req.get("context")):
                        try:
                            ns.func(ns)
                        finally:
                            budget = request_budget.statistics()
                finally:
                    set_cache_enabled(not previous_cache)
        except SystemExit as e:  # argparse 用法错误
            exit_code = e.code or 0
        if exit_code:
            msg = err_buf.getvalue().strip().splitlines()
            raise BoothError(msg[-1] if msg else f"参数解析失败（exit {exit_code}）")

        data = json.loads(out_buf.getvalue())
        print(_bot_envelope(True, action, data, budget=budget))
    except (BoothError, ValueError) as e:
        print(_bot_envelope(False, action, error=str(e)[:300], budget=budget))
    except json.JSONDecodeError as e:
        print(_bot_envelope(False, action, error=f"payload/输出 JSON 解析失败: {e}"))
    except Exception as e:  # 信封接口永不抛栈
        print(_bot_envelope(False, action, error=f"{type(e).__name__}: {str(e)[:300]}"))


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(prog="booth",
                                description="Booth.pm 搜索 CLI（为 AI agent 设计，输出建议加 --json）",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="常用分类 slug(--category): " + "、".join(CATEGORY_HINTS))
    p.add_argument("--version", action="version", version=f"booth {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--no-cache", action="store_true",
                        help="跳过磁盘缓存强制重新请求（默认缓存: 商品 6h / 搜索页 10min）")

    ps = sub.add_parser("search", help="搜索全站商品", aliases=["s"])
    ps.add_argument("query", nargs="*", help="关键词（可含空格/日文）")
    ps.add_argument("--sort", default="new", choices=SORTS,
                    help="排序: new=新着 popularity=人气 liked=收藏(默认 new)")
    ps.add_argument("--type", default="all", choices=TYPES,
                    help="商品类型: digital=下载品 physical=实体(默认 all)")
    ps.add_argument("--adult", default="include", choices=("exclude", "include", "only"),
                    help="R-18: include=联合搜索(默认,结果带 is_adult 标记) exclude=仅全年齢 only=仅R-18")
    ps.add_argument("--tag", action="append", help="按标签过滤，可多次")
    ps.add_argument("--vrc", dest="vrc", action="store_true", default=True,
                    help="收窄 VRChat 圈（--tag VRChat），默认开启")
    ps.add_argument("--no-vrc", dest="vrc", action="store_false",
                    help="关闭 VRChat 收窄，搜全站")
    ps.add_argument("--or-word", action="append", help="OR 关键词，可多次")
    ps.add_argument("--exclude", action="append", help="排除词，可多次")
    ps.add_argument("--min-price", type=int)
    ps.add_argument("--max-price", type=int)
    ps.add_argument("--in-stock", action="store_true", help="仅显示有库存/在售")
    ps.add_argument("--category", help="分类 slug（日语原文，如 3D衣装）")
    ps.add_argument("--event", help="活动 slug")
    ps.add_argument("--lang", default="ja", help="站点语言路径（默认 ja）")
    ps.add_argument("--page", type=int, default=1)
    ps.add_argument("--pages", type=int, default=1, help="连续抓取页数（页间隔约1.2秒）")
    ps.add_argument("--limit", type=int, default=30, help="最多返回条数（默认 30）")
    ps.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    add_common(ps)
    ps.set_defaults(func=cmd_search)

    pi = sub.add_parser("item", help="商品详情", aliases=["i"])
    pi.add_argument("id", nargs="+", help="商品 ID 或 URL")
    pi.add_argument("--desc-len", type=int, default=DEFAULT_DESC_LEN, help="简介截断长度（-1 保留完整正文）")
    pi.add_argument("--full", action="store_true", help="输出原始完整 JSON")
    pi.add_argument("--translate", action="store_true",
                    help="AI 翻译商品名与说明为中文（name_zh/desc_zh；需配置 AI）")
    pi.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    add_common(pi)
    pi.set_defaults(func=cmd_item)

    ph = sub.add_parser("shop", help="商店信息与最新商品", aliases=["sh"])
    ph.add_argument("shop", nargs="+", help="商店子域名或 URL")
    ph.add_argument("--pages", type=int, default=1)
    ph.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    add_common(ph)
    ph.set_defaults(func=cmd_shop)

    pw = sub.add_parser("workflow", help="当前 AI 提供检索词，工具返回候选与商品来源 JSON（无模型调用）")
    pw.add_argument("query", nargs="*", help="用户原始需求；可选 --keyword 提供当前 AI 规划的检索轴")
    pw.add_argument("--keyword", action="append", help="完整检索轴，不再分词或改写；最多 6 个")
    pw.add_argument("--require-term", action="append", help="需核对的素体/依赖等词，供来源摘要定位；不代表兼容已确认")
    pw.add_argument("--sort", default="popularity", choices=SORTS)
    pw.add_argument("--adult", default="include", choices=("exclude", "include", "only"))
    pw.add_argument("--no-vrc", dest="vrc", action="store_false", help="关闭默认 VRChat 标签收窄")
    pw.add_argument("--page", type=int, default=1)
    pw.add_argument("--limit", type=int, default=6, help="返回并拉取详情的候选数量，1-6")
    pw.add_argument("--desc-len", type=int, default=3000, help="每件商品返回的说明长度，1-12000；-1 返回完整说明")
    pw.add_argument("--schema", action="store_true", help="输出机器可读的输入契约与示例，不访问网络")
    pw.add_argument("--json", action="store_true", help="兼容标记；workflow 始终输出 JSON")
    add_common(pw)
    pw.set_defaults(func=cmd_workflow)

    pi2 = sub.add_parser("imgsearch", help="以图搜品（Bing 视觉搜索: HTTP 快路径+浏览器备援; ascii2d 备援）",
                         aliases=["is"])
    pi2.add_argument("image", nargs="+", help="本地图片路径 或 booth 官方图床的图片 URL")
    pi2.add_argument("--engine", default="bing,ascii2d",
                     help="引擎与顺序（默认 bing,ascii2d；bing=纯HTTP优先,失败自动回落浏览器; "
                          "首个引擎有结果时可跳过第二个）")
    pi2.add_argument("--headless", action="store_true",
                     help="浏览器备援引擎用无头模式（默认有头以复用 ~/.booth-cli/pw_profile 的通过状态）")
    pi2.add_argument("--wait-s", type=int, default=24,
                     help="浏览器备援引擎等待结果的秒数（默认 24，bot 接入时可调小控时）")
    pi2.add_argument("--limit", type=int, default=5, help="最多返回候选数（默认 5）")
    pi2.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    pi2.add_argument("--delegate-ai", action="store_true", help="显式启用独立图搜并验证已配置的多模态 API")
    add_common(pi2)
    pi2.set_defaults(func=cmd_imgsearch)

    pm = sub.add_parser("smart", help="本地词扩展搜索；--delegate-ai 可选委托其他 AI 规划与评估",
                        aliases=["sm"])
    pm.add_argument("query", nargs="+", help="需求描述（中文/日文均可，如：适用于Rexouium素体的服装）")
    pm.add_argument("--sort", default="popularity", choices=SORTS,
                    help="排序（默认 popularity；翻页自动切新着）")
    pm.add_argument("--adult", default="include", choices=("exclude", "include", "only"),
                    help="R-18: include=联合搜索(默认,结果带 is_adult 标记) exclude=仅全年齢 only=仅R-18")
    pm.add_argument("--no-vrc", dest="vrc", action="store_false",
                    help="关闭 VRChat 收窄（默认收窄 VRChat 圈）")
    pm.add_argument("--page", type=int, default=1)
    pm.add_argument("--limit", type=int, default=6, help="最多返回条数（默认 6）")
    ai_choice = pm.add_mutually_exclusive_group()
    ai_choice.add_argument("--delegate-ai", action="store_true",
                           help="显式调用 AI_API_KEY/AI_BASE_URL 配置的可选 AI 规划与评估")
    ai_choice.add_argument("--no-ai", action="store_true",
                           help="兼容旧选项；默认已不调用 AI")
    pm.add_argument("--no-webfind", action="store_true",
                    help="禁用网络检索兜底（DDG/Exa）")
    pm.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    add_common(pm)
    pm.set_defaults(func=cmd_smart)

    pw2 = sub.add_parser("watch", help="关注清单：add/list/remove/check（价格·售罄·商品更新变动检查）",
                         aliases=["w"])
    pw2.add_argument("op", nargs="?", default="list",
                     help="add / list / remove / check（默认 list）")
    pw2.add_argument("ids", nargs="*", help="商品 ID 或 URL（add/remove 必填，可多个）")
    pw2.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    pw2.set_defaults(func=cmd_watch)

    po = sub.add_parser("own", help="已购清单（本地素材库）：add/list/remove/check（商品更新检查）",
                        aliases=["o"])
    po.add_argument("op", nargs="?", default="list",
                    help="add / list / remove / check（默认 list）")
    po.add_argument("ids", nargs="*", help="商品 ID 或 URL（add/remove 必填，可多个）")
    po.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    po.set_defaults(func=cmd_own)

    pb = sub.add_parser("bot", help="bot 框架接入钩子：JSON 信封进出（见 QQBOT.md）")
    pb.add_argument("payload", nargs="?", help="JSON 请求，如 '{\"action\":\"search\",\"params\":{\"query\":\"VRChat\"}}'；缺省读 stdin")
    pb.set_defaults(func=cmd_bot)

    return p


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("help", "--help", "-h") and len(argv) == 1:
        print(__doc__)
        return 0

    args = build_parser().parse_args(argv)
    if getattr(args, "no_cache", False):
        set_cache_enabled(False)
    try:
        args.func(args)
        return 0
    except BoothError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
