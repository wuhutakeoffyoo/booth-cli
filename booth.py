#!/usr/bin/env python3
"""booth — Booth.pm (BOOTH 同人/VRChat 素材市场) 命令行搜索工具，为 AI agent 设计。

零第三方依赖，只用 Python 标准库。数据来自 Booth 页面内嵌的结构化数据与
非官方 JSON 接口（/ja/items/{id}.json），请在请求间保持 ≥1 秒间隔以减轻服务器负担。

用法:
  booth search <关键词...> [选项]     搜索全站商品
  booth item   <商品ID|URL> [选项]    查看商品详情
  booth shop   <商店子域名|URL> [选项] 查看商店信息与最新商品
  booth help                          显示帮助

AI 调用建议: 一律加 --json 获取结构化输出。
"""

import argparse
import gzip
import html as html_mod
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://booth.pm"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PAGE_DELAY = 1.2          # 多页抓取时的请求间隔（秒）
DEFAULT_DESC_LEN = 600    # 商品详情描述默认截断长度
MAX_REDIRECTS = 5

SORTS = ("new", "popularity", "liked", "price_asc", "price_desc")
TYPES = ("all", "digital", "physical")
# 常用分类 slug（VRChat 相关，来自搜索页分类导航，日语原文才能匹配）
CATEGORY_HINTS = ("3Dキャラクター", "3D衣装", "3D小道具", "3D装飾品", "3Dテクスチャ",
                  "3D髪型", "3D靴", "3Dモデル（その他）", "VRoid", "ソフトウェア")

CLOUDFLARE_HINT = ("店铺开启了 Cloudflare 人机验证（返回 'Just a moment' 页面），"
                   "该站无法直接抓取；可改用 booth item <ID> 逐个查商品。")

# 安全边界: 本工具只访问 Booth 官方域名，协议仅限 https，重定向逐跳校验
ALLOWED_HOST_SUFFIX = ".booth.pm"


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


def http_get(url, *, json_accept=False, csrf=None):
    """GET 一个 booth.pm 的 URL，返回 (body_bytes, final_url, status)。

    重定向手动跟随并逐跳校验主机白名单，带限次重试。
    """
    assert_allowed_url(url)
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
    for attempt in range(3):
        current = url
        for _hop in range(MAX_REDIRECTS):
            try:
                req = urllib.request.Request(current, headers=headers)
                with _OPENER.open(req, timeout=30) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
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
                    break  # 走外层重试
                raise BoothError(f"HTTP {e.code}: {current}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = BoothError(f"网络错误: {e} ({current})")
                break  # 走外层重试
        else:
            raise BoothError(f"重定向次数过多: {url}")
        time.sleep(1.5 * (attempt + 1))
    raise last_err or BoothError(f"请求失败: {url}")


def get_html(url):
    data, final_url, _ = http_get(url)
    text = data.decode("utf-8", errors="replace")
    if "Just a moment" in text and "challenge" in text.lower():
        raise BoothError(CLOUDFLARE_HINT)
    if "18歳未満" in text and "data-product-id" not in text and "data-item=" not in text:
        raise BoothError("返回了年龄确认页，未能获取内容（内部错误，请重试）")
    return text, final_url


def get_json(url, csrf=None):
    data, _, _ = http_get(url, json_accept=True, csrf=csrf)
    return json.loads(data.decode("utf-8", errors="replace"))


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


def parse_search_page(page_html):
    """从搜索结果 HTML 提取商品卡片与元信息。"""
    total = None
    m = re.search(r"対象商品\s*([\d,]+)\s*件", page_html)
    if m:
        total = int(m.group(1).replace(",", ""))
    has_next = 'rel="next"' in page_html

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
        })
    return {"total": total, "items": items, "has_next": has_next}


# ---------------------------------------------------------------- 商品详情

def fetch_item(item_id, lang="ja"):
    """单品 JSON；被拒时先取 HTML 页拿 csrf 再重试。"""
    url = f"{BASE}/{lang}/items/{item_id}.json"
    try:
        return get_json(url)
    except BoothError:
        page_html, _ = get_html(f"{BASE}/{lang}/items/{item_id}")
        csrf_m = re.search(r'name="csrf-token" content="([^"]+)"', page_html)
        return get_json(url, csrf=csrf_m.group(1) if csrf_m else None)


def trim_item(raw, desc_len=DEFAULT_DESC_LEN):
    images = []
    for img in raw.get("images") or []:
        u = (img or {}).get("original") or (img or {}).get("resized")
        if u:
            images.append(u.replace("_base_resized", ""))
    variations = []
    for v in raw.get("variations") or []:
        name = " / ".join(str(v.get(f"field{i}")) for i in range(1, 7)
                          if v.get(f"field{i}"))
        variations.append({"name": name or None,
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
        "is_sold_out": raw.get("is_sold_out"),
        "is_end_of_sale": raw.get("is_end_of_sale"),
        "wish_lists_count": raw.get("wish_lists_count"),
        "published_at": raw.get("published_at"),
        "category": cat.get("name") if isinstance(cat, dict) else cat,
        "tags": [t.get("name") for t in raw.get("tags") or []],
        "images": images,
        "variations": variations,
        "description": clean_text(raw.get("description"))[:desc_len] or None,
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
        thumbs = [u.replace("_base_resized", "") for u in
                  (raw.get("thumbnail_image_urls") or [])]
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
        })
    return items


# ---------------------------------------------------------------- 命令

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
    if query:
        params["q"] = query
    if args.sort != "new":
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
    if args.vrc:
        args.tag = list(args.tag or []) + ["VRChat"]
    for t in args.tag or []:
        params.setdefault("tags[]", []).append(t)
    for w in args.or_word or []:
        params.setdefault("or_words[]", []).append(w)
    for w in args.exclude or []:
        params.setdefault("except_words[]", []).append(w)
    if args.page > 1:
        params["page"] = str(args.page)
    qs = urllib.parse.urlencode(params, doseq=True)
    return BASE + path + (f"?{qs}" if qs else "")


def cmd_search(args):
    items, total, has_next = [], None, False
    fetched = 0
    for offset in range(args.pages):
        args.page = args.page + offset
        url = build_search_url(args)
        page_html, final_url = get_html(url)
        result = parse_search_page(page_html)
        if not result["items"] and "item-card" not in page_html:
            raise BoothError(f"搜索页无结果或结构异常: {final_url}（语言 {args.lang} 可能不渲染列表，试试 --lang ja）")
        if total is None:
            total = result["total"]
        has_next = result["has_next"]
        fetched += 1
        items.extend(result["items"])
        if len(items) >= args.limit:
            break
        if not has_next:
            break
        if offset < args.pages - 1:
            time.sleep(PAGE_DELAY)
    items = items[:args.limit]

    if args.json:
        print(json.dumps({
            "query": " ".join(args.query or []).strip(), "page": args.page, "pages_fetched": fetched,
            "total": total, "count": len(items), "has_next": has_next,
            "items": items,
        }, ensure_ascii=False, indent=2))
    else:
        if total is not None:
            print(f"共 {total:,} 件，本次显示 {len(items)} 件\n")
        for it in items:
            price = f"¥{it['price']:,}" if it["price"] is not None else "价格未知"
            flags = "R-18" if it.get("is_adult") else ""
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
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    it = payload
    print(f"#{it['id']}  {it['price']}  {it['shop'].get('name') or ''}({it['shop'].get('subdomain') or ''})")
    print(f"标题: {it['name']}")
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


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(prog="booth",
                                description="Booth.pm 搜索 CLI（为 AI agent 设计，输出建议加 --json）",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="常用分类 slug(--category): " + "、".join(CATEGORY_HINTS))
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="搜索全站商品", aliases=["s"])
    ps.add_argument("query", nargs="*", help="关键词（可含空格/日文）")
    ps.add_argument("--sort", default="new", choices=SORTS,
                    help="排序: new=新着 popularity=人气 liked=收藏(默认 new)")
    ps.add_argument("--type", default="all", choices=TYPES,
                    help="商品类型: digital=下载品 physical=实体(默认 all)")
    ps.add_argument("--adult", default="include", choices=("exclude", "include", "only"),
                    help="R-18: include=联合搜索(默认,结果带 is_adult 标记) exclude=仅全年齢 only=仅R-18")
    ps.add_argument("--tag", action="append", help="按标签过滤，可多次")
    ps.add_argument("--vrc", action="store_true", help="等价 --tag VRChat")
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
    ps.set_defaults(func=cmd_search)

    pi = sub.add_parser("item", help="商品详情", aliases=["i"])
    pi.add_argument("id", nargs="+", help="商品 ID 或 URL")
    pi.add_argument("--desc-len", type=int, default=DEFAULT_DESC_LEN, help="简介截断长度")
    pi.add_argument("--full", action="store_true", help="输出原始完整 JSON")
    pi.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    pi.set_defaults(func=cmd_item)

    ph = sub.add_parser("shop", help="商店信息与最新商品", aliases=["sh"])
    ph.add_argument("shop", nargs="+", help="商店子域名或 URL")
    ph.add_argument("--pages", type=int, default=1)
    ph.add_argument("--json", action="store_true", help="输出 JSON（AI 推荐）")
    ph.set_defaults(func=cmd_shop)

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
