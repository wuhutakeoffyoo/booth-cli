#!/usr/bin/env python3
"""VRC 对口智能搜索助手，供 booth.py 的 smart 命令调用。

把 vrc-booth-bot 侧沉淀的 VRC 搜索策略下沉到 CLI（两仓同源，共享同一套策略）：
- 需求理解：中文/口语描述 → AI 产出单词级标题关键词 + desc_keywords（说明文核实词）
- 自我纠错：知名商品回忆（模型 VRChat 圈知识）兜底
- 检索变体：pykakasi 汉字→假名读音（可选依赖，未装跳过该维度）+ 去空格连写形
- 描述核实：対応素体/仕様 写在商品说明文而非标题，拉详情按说明文匹配置顶
- 网络检索兜底：站内搜不到时 DDG（免 key）/ Exa（EXA_API_KEY，可选）找 booth 商品链接

AI 后端为 OpenAI 兼容 chat/completions，环境变量与 vrc-booth-bot 同名
（同机部署时一份 .env 两边通用）：
  VISION_API_KEY / VISION_BASE_URL / VISION_MODEL / VISION_TIMEOUT
  兜底：AI_FALLBACK_API_KEY / AI_FALLBACK_BASE_URL / AI_FALLBACK_MODEL
零第三方依赖的边界：除标准库外，pykakasi 为**必装依赖**（假名读音变体，
booth smart 依赖它；缺装时给出明确安装提示）。
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

try:
    import pykakasi  # 必装依赖（假名读音变体）；延迟到调用点报错以便给出安装提示
except ImportError:  # pragma: no cover
    pykakasi = None

PYKAKASI_HINT = "booth smart 需要 pykakasi（假名读音变体，必装依赖）: pip install pykakasi"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 需求解析（纯函数）

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
# 简体字特有形（日文不用这些字形），用于混入片假名的外来语查询的中文判定
_SIMPLIFIED_RE = re.compile(
    "[们这说图搜贴价买卖现视频动发经过关软猫丝袜女饰宠头见听记应东车马语读谁错银钱购枪鲨浓妆补儿]")


def looks_chinese(text: str) -> bool:
    """中文判定：先查简体字特有形（混片假名的中文查询也算中文），
    再查假名（有假名无简体 → 日语），最后默认有汉字即中文。"""
    if _SIMPLIFIED_RE.search(text or ""):
        return True
    if _KANA_RE.search(text or ""):
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _is_reasoning_prose(text: str) -> bool:
    """识别推理模型泄漏的思考过程文本（非关键词）。"""
    if len(text) > 30 or "..." in text or text.rstrip().endswith(")"):
        return True
    lowered = text.lower()
    if lowered.startswith(("let me", "the image", "this image", "from the image",
                           "text visible", "i ", "i can", "分析", "图中", "画面中",
                           "also ", "a character", "note:", "usage")):
        return True
    return ("common in" in lowered or "character name" in lowered
            or "i can see" in lowered or "text visible" in lowered)


def parse_translation(content: str) -> tuple:
    """解析翻译输出：(标题搜索关键词, 描述核实关键词)。
    JSON 优先（keywords/desc_keywords），坏 JSON 退回按行拆（desc 为空）。"""
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            kws = data.get("keywords") or []
            if isinstance(kws, list):
                clean = [str(k).strip() for k in kws
                         if str(k).strip() and not _is_reasoning_prose(str(k))]
                dkws = data.get("desc_keywords") or []
                dclean = ([str(k).strip() for k in dkws
                           if str(k).strip() and not _is_reasoning_prose(str(k))]
                          if isinstance(dkws, list) else [])
                return clean, dclean[:3]
        except (json.JSONDecodeError, AttributeError):
            pass
    parts = re.split(r"[\n,，、/|]+", content or "")
    clean = [p.strip(" -·*") for p in parts
             if len(p.strip(" -·*")) >= 2 and not _is_reasoning_prose(p)]
    return clean[:8], []


def parse_recall(content: str) -> list:
    """解析知名商品回忆的模型输出（JSON 优先，兜底按行拆）。"""
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            cands = data.get("candidates") or []
            if isinstance(cands, list):
                return [str(c).strip() for c in cands
                        if str(c).strip() and not _is_reasoning_prose(str(c))]
        except (json.JSONDecodeError, AttributeError):
            pass
    parts = re.split(r"[\n,，、]+", content or "")
    return [p.strip(" -·*") for p in parts
            if len(p.strip(" -·*")) >= 2 and not _is_reasoning_prose(p)][:3]


def expand_reading_variants(keywords: list) -> list:
    """为关键词追加搜索变体（pykakasi 为必装依赖）：
    1. 汉字→平假名读音（信濃→しなの；Booth 标题词形不统一且搜索不做跨字形归一）；
    2. 去空格连写形（ショコラ ドレス→ショコラドレス；Booth 分词为 AND 匹配，
       连写复合词必须整词命中）。"""
    if pykakasi is None:
        raise RuntimeError(PYKAKASI_HINT)
    out = []
    seen = set()
    seen_readings = set()
    kks = pykakasi.kakasi()
    kks.setMode("J", "H")  # 汉字→平假名读音（片假名保持原样）
    conv = kks.getConverter()
    for kw in keywords:
        kw_reading = conv.do(kw)
        if kw_reading in seen_readings:
            continue  # 同读音关键词（リング/指輪/ゆびわ 类）只保留首个，节省槽位
        forms = [kw, kw_reading]
        if " " in kw:
            forms.append(kw.replace(" ", ""))
        for form in forms:
            form = form.strip()
            if form and form not in seen:
                seen.add(form)
                out.append(form)
        seen_readings.add(kw_reading)
    return out


def build_search_terms(kws: list) -> list:
    """单词级检索词 + 连写整词保留（Booth 多词 AND 匹配脆弱，单词命中率最高）。"""
    terms, seen = [], set()
    for kw in (kws or [])[:6]:
        for tok in re.split(r"[\s/、，,]+", str(kw)):
            tok = tok.strip()
            if len(tok) >= 2 and tok not in seen:
                seen.add(tok)
                terms.append(tok)
        whole = str(kw).strip()
        if " " in whole and whole not in seen:
            seen.add(whole)
            terms.append(whole)
    return terms[:6]


# ---------------------------------------------------------------- 描述核实重排

def desc_hit(it: dict, desc_kws: list) -> bool:
    """商品标题或简介（商品说明）含任一核实词（大小写不敏感子串）。"""
    hay = ((it.get("name") or "") + "\n" + (it.get("_desc") or "")).lower()
    return any(str(k).lower() in hay for k in desc_kws if k)


def desc_boost(merged: list, title_kws: list, desc_kws: list) -> list:
    """三级稳定重排：说明文/标题含核实词 → 标题含搜索词 → 其余。"""
    tkws = [str(k).lower() for k in title_kws if k]

    def _tier(it) -> int:
        if desc_kws and desc_hit(it, desc_kws):
            return 0
        name = (it.get("name") or "").lower()
        return 1 if any(k in name for k in tkws) else 2

    return sorted(merged, key=_tier)


# ---------------------------------------------------------------- AI 后端

# 中文/需求描述 → Booth 搜索方案的提示词：标题关键词 + 说明文核实词
_TRANSLATE_PROMPT = (
    "用户在 Booth.pm（日本同人/VRChat 素材市场）找商品，输入的是中文口语/需求描述。"
    "先理解用户真实想要什么，再输出搜索方案，只输出 JSON："
    '{"keywords": ["单词1", "单词2"], "desc_keywords": ["需要到商品说明里核实的词"]}\n'
    "keywords（最多 8 个，按命中可能性从高到低）——用于商品【标题】搜索，"
    "每个必须是单个单词，一个关键词只表达一个概念，"
    "禁止组合成短语（グリモワール 衣装 ✗ → グリモワール ✓）——"
    "Booth 搜索单个精确名词时命中率最高：\n"
    "1. 专有名词/商品名/角色名/素体名按日本市场实际写法输出：外来语给片假名完整转写"
    "（chocolate dress → ショコラドレス 级别的完整形），人名给片假名与常见汉字两种；\n"
    "2. 部位/用途类需求转成日本圈行业词（尾巴→尻尾/しっぽ/テイル，耳朵→みみ/耳，"
    "眼镜→メガネ，动画/表情功能→ギミック/アニメーション）；\n"
    "3. 类型概念用行业单词（3Dモデル/衣装/髪型/アクセサリ/ギミック/テクスチャ等）；\n"
    "desc_keywords（0-3 个）——『适用于/対応/兼容某素体、支持某功能』这类兼容性需求的"
    "核实词：Booth 把对应信息写在商品【说明文】的 対応素体/仕様 段落而非标题，"
    "把要核实的素体名/功能名放这里（按日本市场原名写法）。无此类需求给空数组。"
)

_RECALL_PROMPT = (
    "你是 VRChat 圈的资深玩家，熟悉 Booth.pm 上的知名模型、衣装、髪型、ギミック与热门商品。"
    "用户想找这样的 VRChat 素材：『{desc}』。"
    "根据你对 VRChat 圈的了解，写出最可能的具体商品名（日文原名，含作者名更好），最多 3 个；"
    "不确定就给最接近的通称。只输出 JSON："
    '{"candidates": ["商品名1", "商品名2"]}'
)


class AiError(Exception):
    """AI 调用失败（带 HTTP 状态码或网络错误详情），供 friendly_ai_error 归类。"""

    def __init__(self, code=None, detail=""):
        self.code = code
        self.detail = detail
        super().__init__(detail or f"HTTP {code}")


def ai_backend():
    """从环境变量读取 AI 后端配置，返回参数 dict；未配置返回 None。
    变量名与 vrc-booth-bot 一致（同机部署时一份 .env 两边通用）。"""
    import os
    key = os.environ.get("VISION_API_KEY", "").strip()
    if not key:
        return None
    return {
        "base_url": os.environ.get("VISION_BASE_URL",
                                   "https://opencode.ai/zen/go/v1").strip(),
        "api_key": key,
        "model": os.environ.get("VISION_MODEL", "glm-5.3-flash").strip(),
        "timeout": int(os.environ.get("VISION_TIMEOUT", "60") or 60),
    }


def ai_fallback_backend():
    """兜底 AI 后端（主后端失败时切换）；未配置返回 None。"""
    import os
    key = os.environ.get("AI_FALLBACK_API_KEY", "").strip()
    if not key:
        return None
    return {
        "base_url": os.environ.get("AI_FALLBACK_BASE_URL",
                                   "https://open.bigmodel.cn/api/coding/paas/v4").strip(),
        "api_key": key,
        "model": os.environ.get("AI_FALLBACK_MODEL", "glm-5.3-flash").strip(),
        "timeout": int(os.environ.get("VISION_TIMEOUT", "60") or 60),
    }


def guard_api_base(base_url: str) -> str:
    """出站前校验 API base：仅 https 且主机非本机/私网/保留地址。"""
    parts = urllib.parse.urlsplit(base_url or "")
    host = parts.hostname or ""
    if parts.scheme != "https" or not host:
        raise AiError(detail=f"AI base URL 必须是 https: {base_url}")
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith((".local", ".internal")):
        raise AiError(detail=f"AI base URL 主机不被允许: {host}")
    try:
        if ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_reserved:
            raise AiError(detail=f"AI base URL 指向私网/保留地址: {host}")
    except ValueError:
        pass  # 域名（非 IP 字面量），放行
    return base_url


def _post_chat(url: str, payload: dict, api_key: str, timeout: int,
               retries: int = 2) -> str:
    """POST chat/completions，传输类/5xx 错误自动重试，返回回复文本。
    UA 用浏览器标识：Cloudflare WAF 会拦数据中心 IP + python 默认 UA 的大 body POST。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json",
               "User-Agent": _UA}
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            if not content.strip():
                content = msg.get("reasoning_content") or ""  # 推理模型兜底
            return content
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
            last_err = AiError(code=e.code, detail=detail)
            if e.code >= 500 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_err
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = AiError(detail=f"网络错误: {e}")
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_err
    raise last_err or AiError(detail="请求失败")


def friendly_ai_error(e: Exception) -> str:
    """把 AI 调用异常翻译成准确、可行动的中文反馈。"""
    if isinstance(e, AiError) and e.code:
        code, body = e.code, (e.detail or "").lower()
        if code == 402 or "insufficient" in body or "no balance" in body:
            return ("AI 额度不足：账户按量余额不够（套餐仅覆盖套餐内模型），请充值或换用套餐内模型")
        if code == 429 or "usage limit" in body or "limit exceeded" in body:
            if "5 hour" in body or "5h" in body or "hourly" in body:
                return "AI 已达套餐 5 小时用量上限，等窗口重置后自动恢复"
            if "week" in body:
                return "AI 已达套餐每周用量上限，周一自动恢复"
            if "month" in body or "monthly" in body:
                return "AI 已达套餐每月用量上限，次月自动恢复"
            return "AI 请求被限流（429）：触发用量上限或请求过频，请稍后再试"
        if code == 401:
            return "AI 认证失败：API key 无效或已过期，请检查 VISION_API_KEY"
        if code == 403:
            return "AI 请求被拦截（403）：key 无权限或触发安全策略，请检查账户套餐状态"
        if code >= 500:
            return f"AI 服务端错误（HTTP {code}）：上游故障，请稍后再试"
        return f"AI 接口错误（HTTP {code}）"
    if isinstance(e, AiError):
        if "超时" in e.detail or "timed out" in e.detail.lower():
            return "AI 网络超时：端点响应过慢（模型繁忙），请稍后重试"
        return f"AI 网络异常：{e.detail[:120]}"
    if isinstance(e, TimeoutError):
        return "AI 网络超时：端点响应过慢（模型繁忙），请稍后重试"
    return f"AI 调用失败：{type(e).__name__}"


def translate_keywords(text: str, *, base_url: str, api_key: str, model: str,
                       timeout: int = 60) -> tuple:
    """中文需求 → (日语标题关键词, 描述核实关键词)。输出不含 JSON 时带强化指令重试一次。"""
    guard_api_base(base_url)
    url = base_url.rstrip("/") + "/chat/completions"
    prompt = f"{_TRANSLATE_PROMPT}\n用户需求：{text}"
    for attempt in range(2):
        payload = {
            "model": model,
            "messages": [{"role": "user",
                          "content": prompt + ("\n（再次提醒：只输出 JSON 本体，"
                                               "不要输出任何解释或思考过程）" if attempt else "")}],
            "temperature": 0.2,
            "max_tokens": 2000,
        }
        content = _post_chat(url, payload, api_key, timeout)
        kws, dkws = parse_translation(content)
        if kws and re.search(r"\{.*\}", content, re.S):
            return kws, dkws
    raise AiError(detail=f"翻译输出无法解析: {(content or '')[-160:]}")


def recall_products(desc: str, *, base_url: str, api_key: str, model: str,
                    timeout: int = 60) -> list:
    """利用模型的 VRChat 圈知识回忆可能的知名商品名（自我纠错层）。"""
    guard_api_base(base_url)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user",
                      "content": _RECALL_PROMPT.replace("{desc}", desc)}],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    return parse_recall(_post_chat(url, payload, api_key, timeout))


# ---------------------------------------------------------------- 网络检索兜底

_WEBFIND_HOSTS = ("html.duckduckgo.com", "api.exa.ai")
_ITEM_RE = re.compile(r"booth\.pm/(?:[a-z]{2}/)?items/(\d+)")
_SUB_ITEM_RE = re.compile(r"([a-z0-9-]+)\.booth\.pm/(?:[a-z]{2}/)?items/(\d+)")


def _validated_outbound_url(url: str) -> str:
    """出站前校验：仅 https、主机在白名单、无显式端口、DNS 解析全为公网地址
    （阻断私网/环回/链路本地，防 SSRF 与 DNS rebinding）。"""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _WEBFIND_HOSTS or parts.port is not None:
        raise ValueError(f"网络检索 URL 超出许可范围: {url[:120]}")
    for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"检索主机解析到非公网地址，拒绝请求: {ip}")
    return url


def item_ids_from_urls(urls: list) -> list:
    """从 URL 列表按出现顺序提取商品 ID（去重）。"""
    ids = []
    for u in urls:
        m = _ITEM_RE.search(u) or _SUB_ITEM_RE.search(u)
        if m:
            iid = int(m.group(1) if m.lastindex == 1 else m.group(2))
            if iid not in ids:
                ids.append(iid)
    return ids


def ids_from_ddg_html(html: str) -> list:
    """解析 DDG HTML 结果页里的 booth 商品链接。"""
    urls = []
    for m in re.finditer(r'href="([^"]+)"', html or ""):
        href = urllib.parse.unquote(m.group(1))
        if "booth.pm" in href:
            urls.append(href)
    return item_ids_from_urls(urls)


def ddg_find(keywords: str, timeout: int = 15) -> list:
    """DuckDuckGo HTML 检索 <keywords> booth.pm，返回商品 ID 列表（失败返回空）。"""
    q = urllib.parse.quote(f"{keywords} booth.pm")
    try:
        url = _validated_outbound_url("https://html.duckduckgo.com/html/")
        data = urllib.parse.urlencode({"q": q}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ids_from_ddg_html(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []  # 数据中心 IP 偶被 DDG 挑战，静默降级


def exa_find(keywords: str, api_key: str, timeout: int = 15) -> list:
    """Exa AI 搜索（可选）：限定 booth.pm 域名，返回商品 ID 列表（失败返回空）。"""
    try:
        url = _validated_outbound_url("https://api.exa.ai/search")
        body = json.dumps({"query": keywords, "numResults": 8,
                           "includeDomains": ["booth.pm"]}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "x-api-key": api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return item_ids_from_urls([r.get("url", "") for r in data.get("results", [])])
    except Exception:
        return []


def find_booth_item_ids(keywords: str, *, exa_api_key: str = "",
                        timeout: int = 15) -> list:
    """网络检索兜底入口：DDG 优先，Exa 补充（配了 key 时），合并去重。"""
    ids = ddg_find(keywords, timeout=timeout)
    if exa_api_key:
        for iid in exa_find(keywords, api_key=exa_api_key, timeout=timeout):
            if iid not in ids:
                ids.append(iid)
    return ids
