#!/usr/bin/env python3
"""反向图搜引擎，供 booth.py imgsearch 调用。

引擎与路径（按优先级）：
1. bing_search_http —— Bing 视觉搜索纯 HTTP 协议（主路径）：multipart 上传拿 BCID，
   再 POST knowledge API 拿 insights JSON。无浏览器、无 profile 依赖，约 2 秒。
   协议逆向自开源库 kitUIN/PicImageSearch（https://github.com/kitUIN/PicImageSearch）。
2. bing_search —— Bing playwright 备援（有头 Chrome + 持久 profile ~/.booth-cli/pw_profile
   过 Cloudflare）。仅在 HTTP 路径被 Bing 拒绝时使用。
3. ascii2d_search —— ascii2d playwright 备援。

为什么不是 Google/SauceNAO：
- Google 对自动化客户端 403 封锁 /search 结果页，绕不过；
- SauceNAO 匿名账户禁用 JSON API，且索引不含 booth.pm；
- Bing 视觉搜索对 booth.pm 商品页覆盖好、反爬宽松。

返回：按"结果首次出现顺序"排序的 booth 商品 ID 列表（越靠前越相关）+ 引擎派生的关键词。
依赖：主路径零依赖（纯标准库）；备援路径需 playwright（pip install playwright && playwright install chrome）。
"""

import base64
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROFILE_DIR = Path.home() / ".booth-cli" / "pw_profile"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Bing 纯 HTTP 端点：主机/路径均为模块常量；Bing 下发的 token 只进 query 与表单，
# 永不进入主机名/路径；请求前做 https+主机白名单+DNS 解析边界校验，重定向一律拒绝。
_BING_HOST = "www.bing.com"
_BING_UPLOAD_PATH = "/images/search?view=detailv2&iss=sbiupload"
_BING_INSIGHTS_PATH = "/images/api/custom/knowledge"
_BING_SIG_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIGd3gMN2v1KRLBGmotz7jbQYF8PaB+Jpe6iVf2YIeN5b"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_BING_OPENER = urllib.request.build_opener(_NoRedirect)


def _validated_bing_url(url):
    """出站前校验 Bing 请求 URL：仅 https、主机精确等于 www.bing.com、无显式端口，
    且 DNS 解析结果全部为公网地址（阻断私网/环回/链路本地，防 SSRF 与 DNS rebinding）。"""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host != _BING_HOST or parts.port is not None:
        raise RuntimeError(f"Bing 请求 URL 超出许可范围: {url[:120]}")
    for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RuntimeError(f"Bing 主机解析到非公网地址，拒绝请求: {ip}")
    return url


def _ordered_ids(text):
    """按首次出现顺序提取 booth 商品 ID（含 URL 编码与 JSON 转义形态）。"""
    seen = {}
    for cand in (text,
                 text.replace("%2F", "/").replace("%3A", ":"),
                 text.replace("\\/", "/")):
        for m in re.finditer(r"items/(\d{4,})", cand):
            seen.setdefault(int(m.group(1)), None)
    return list(seen)


def _booth_links(html):
    links = re.findall(r'https?://[^"\'<>\\ ]*booth\.pm[^"\'<>\\ ]*', html)
    return list(dict.fromkeys(links))


def _decrypt_bing_signature(raw):
    """解密 Bing imageSignature（格式 version|base64密文|timestamp）。

    密文为 XOR + 偏移 3 的字符变换（逆向自 PicImageSearch），失败时原样返回。
    """
    parts = raw.split("|")
    if len(parts) != 3:
        return raw
    ver, enc, ts = parts
    try:
        data = base64.b64decode(enc)
        out = []
        for i, b in enumerate(data):
            out.append(chr((b ^ ord(_BING_SIG_KEY[i % len(_BING_SIG_KEY)])) - 3))
        return f"{ver}|{''.join(out)}|{ts}"
    except Exception:
        return raw


def _multipart(fields):
    """构造 multipart/form-data（全部为无文件名的表单字段）。"""
    boundary = "----boothcliboundary7d1a2f"
    parts = []
    for name, value in fields:
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n")
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


def _bing_post(path, query=None, body=b"", content_type=None,
               extra_headers=None, timeout=30):
    """POST 到 Bing 固定端点，返回 (status, url, text, location)。
    主机/路径为常量，query 仅承载 Bing 下发的 token；重定向一律不跟随。"""
    url = "https://" + _BING_HOST + path
    if query:
        url = url + "?" + urllib.parse.urlencode(query)
    url = _validated_bing_url(url)
    headers = {"User-Agent": _UA,
               "Referer": "https://www.bing.com/images"}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _BING_OPENER.open(req, timeout=timeout) as resp:
            return resp.status, url, resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, url, "", e.headers.get("Location")


def _derived_from_insights(data):
    """从 insights JSON 里提取派生关键词（Bing 对图片内容的文字描述）。"""
    texts = []
    try:
        j = json.loads(data)
    except json.JSONDecodeError:
        return ""
    for g in j.get("bestGuess") or []:
        t = (g or {}).get("text")
        if t:
            texts.append(t)
    vs = j.get("visualSearch") or {}
    for r in vs.get("relatedSearches") or []:
        t = (r or {}).get("text")
        if t:
            texts.append(t)
    return " ".join(texts).strip()


def _bing_get(path_or_url, timeout=30):
    """GET Bing 页面（拒绝重定向跟随，返回 (status, final_url, text, location)）。"""
    url = path_or_url if path_or_url.startswith("https://") else "https://" + _BING_HOST + path_or_url
    url = _validated_bing_url(url)
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.7",
        "Referer": "https://www.bing.com/images",
    })
    try:
        with _BING_OPENER.open(req, timeout=timeout) as resp:
            return resp.status, url, resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, url, "", e.headers.get("Location")


def _bing_upload(image_path):
    """multipart 上传图片，返回 (bcid, blob_url)。上传成功时 Bing 以 302 下发 bcid。"""
    image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    body, ctype = _multipart([("cbir", "sbi"), ("imageBin", image_b64)])
    status, _, text, loc = _bing_post(_BING_UPLOAD_PATH, body=body, content_type=ctype)
    if status not in (200, 302):
        raise RuntimeError(f"Bing 上传返回 HTTP {status}")
    hay = (loc or "") + "\n" + text
    bcid_m = re.search(r"bcid_[A-Za-z0-9\-.]+", hay)
    if not bcid_m:
        raise RuntimeError("Bing 上传响应缺少 bcid（可能被风控，走 playwright 备援）")
    blob_m = re.search(r"vsimg=([^&]+)", loc or "")
    blob = urllib.parse.unquote(blob_m.group(1)) if blob_m else None
    return bcid_m.group(0), blob


_BING_BOUNCE_RE = re.compile(r"FORM=(?:SBIRDI|SBIHMP)")


def bing_search_http(image_path, cookie_jar=None):
    """Bing 视觉搜索·纯 HTTP 快路径，返回 (item_ids有序, derived_query, final_url)。

    协议（逆向自 kitUIN/PicImageSearch）：
    Step1 multipart 上传 base64 图片，从 302 Location 提取 bcid；
    Step2 清 Cookie 后 POST knowledge API（insightsToken=bcid）拿 insights JSON；
    Step3 JSON 无结果时改取 detailV2 详情页 HTML（部分网络下该页会弹回首页，
          此时直接抛错，由调用方回落 playwright 引擎）。
    """
    bcid, blob = _bing_upload(image_path)
    result_url = f"https://www.bing.com/images/search?insightsToken={bcid}"

    body2, ctype2 = _multipart([("knowledgeRequest", json.dumps(
        {"imageInfo": {"imageInsightsToken": bcid, "source": "Gallery"}}))])
    _, _, data, _ = _bing_post(
        _BING_INSIGHTS_PATH,
        query={
            "rshighlight": "true", "textDecorations": "true",
            "internalFeatures": "similarproducts,share", "nbl": "1",
            "safeSearch": "off", "mkt": "en-us", "setLang": "en-us",
            "iss": "SBIUPLOADGET", "IID": "idpins", "SFX": "1",
            "insightsToken": bcid,
        },
        body=body2, content_type=ctype2,
        extra_headers={"Referer": result_url})
    ids = _ordered_ids(data)
    if ids:
        return ids, _derived_from_insights(data), result_url

    # knowledge 空结果 → 试 detailV2 详情页（宽松地区可直出含 booth 链接的 HTML）
    status, _, html, loc = _bing_get(
        f"/images/search?view=detailV2&insightsToken={bcid}"
        + (f"&vsimg={urllib.parse.quote(blob, safe='')}" if blob else ""))
    if status == 200 and not (loc and _BING_BOUNCE_RE.search(loc)):
        ids = _ordered_ids(html)
        if ids:
            return ids, "", result_url
    raise RuntimeError("Bing HTTP 快路径无结果（SBI 页面被弹回或区域受限），走 playwright 备援")


# ---------------------------------------------------------------- playwright 备援

def _open_browser(pw, headless=False):
    prof = PROFILE_DIR
    prof.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(prof), headless=headless, channel="chrome",
        viewport={"width": 1300, "height": 950},
        args=["--disable-blink-features=AutomationControlled"])


def bing_search(image_path, wait_s=24, headless=False):
    """Bing 视觉搜索（playwright 备援）：上传图片，返回 (item_ids有序, derived_query, result_url)。"""
    from playwright.sync_api import sync_playwright
    ids, query, final = [], "", ""
    with sync_playwright() as p:
        b = _open_browser(p, headless=headless)
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


def ascii2d_search(image_path, wait_s=24, headless=False):
    """ascii2d：上传图片（色合い→特徴），返回 (item_ids有序, page_url)。"""
    from playwright.sync_api import sync_playwright
    ids, final = [], ""
    with sync_playwright() as p:
        b = _open_browser(p, headless=headless)
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
