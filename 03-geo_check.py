# coding=utf-8
# 兼容 Python 3.8+（勿使用 X | Y 类型写法）
"""
geo_check.py —— 网站 GEO（AI 爬虫友好化）自动体检工具
版本：v10（2026-10-10）—— 对齐文档 v10：新增「置信度门控 / P0 封顶 / AI 发现端点 /
      Agent-ready 轻检 / GEO 注入自查」，并把打分升级为「证据优先（unknown 不计 0）」。

公开说明：https://www.geohttp.com/method （计分口径）
           https://github.com/yh6788/dctc_geo_check （本仓库 README）
  检测项来源（资料库章节号，仅作内部对照，不含本机路径）：
  · 验收标准与检测命令 · 量化评分（0–100）· 置信度门控（unknown≠0 / P0 封顶）
  · 问题分级（P0/P1/P2/P3）· 报告模板 · AI 爬虫清单
  · AI 发现端点 / Agent-ready · GEO 注入自查

设计原则（重要）：
  1) 纯确定性：仅用 Python 标准库，无需 requests/bs4/lxml，任何环境可跑；
  2) 只读安全：只发 HTTP GET，不写站点目录、不改数据库、不改模板；
  3) 不依赖 AI：主体不 import 任何大模型；`--ai` 时才按需复用 AI_fenxi.py；
  4) 输出可直接回填文档：默认打印 Markdown 报告（对齐 §十二 模板）；
  5) 证据优先（v10）：区分「已判定通过 / 已判定失败 / 无法判定（unknown）」，
     unknown 既不计 0 也不计满分，只拉低置信度；存在的 P0 会把总分封顶在「及格」。

用法：
  python3 geo_check.py --site https://www.hqbsh.com
  python3 geo_check.py --all                      # 跑内置站点清单
  python3 geo_check.py --site URL --json          # 输出 JSON（供程序消费）
  python3 geo_check.py --site URL --out report.md # 写 Markdown 报告
  python3 geo_check.py --site URL --ai            # 额外调用 AI_fenxi 做评审（可选）

退出码：0 正常；2 存在 P0 问题（便于 cron/CI 判断）。
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# 常量：AI 爬虫清单（来源：文档 §四；分类与 geo-optimizer-skill 的 BOT_TIERS 一致）
# ---------------------------------------------------------------------------

# 引用型核心集（CITATION_BOTS）——决定"被引用"，robots 必须显式 Allow
CITATION_BOTS = ["OAI-SearchBot", "Claude-SearchBot", "PerplexityBot", "Googlebot", "Applebot"]
# 其他检索/按需型
SEARCH_BOTS = ["ChatGPT-User", "Claude-User", "Perplexity-User", "xAI-Bot",
               "Amazonbot", "DuckAssistBot", "YouBot", "PetalBot"]
# 训练型（放行与否自行权衡）
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "Applebot-Extended", "CCBot", "Bytespider",
                 "meta-externalagent", "Meta-ExternalFetcher", "AI2Bot", "cohere-ai"]
# 经典搜索引擎
CLASSIC_BOTS = ["Baiduspider", "bingbot", "Sogou", "360Spider", "YisouSpider"]

# 用于连通性实测的代表性 UA（覆盖各类）
PROBE_UAS = {
    "Googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "bingbot": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Applebot": "Mozilla/5.0 (compatible; Applebot/0.1; +http://www.apple.com/go/applebot)",
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36; compatible; GPTBot/1.2; +https://openai.com/gptbot",
    "OAI-SearchBot": "Mozilla/5.0 (compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)",
    "ChatGPT-User": "Mozilla/5.0 (compatible; ChatGPT-User/1.0; +https://openai.com/bot)",
    "ClaudeBot": "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "Claude-SearchBot": "Mozilla/5.0 (compatible; Claude-SearchBot/1.0; +https://anthropic.com/claude-searchbot)",
    "PerplexityBot": "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
    "CCBot": "CCBot/2.0 (https://commoncrawl.org/faq/)",
    "Bytespider": "Mozilla/5.0 (compatible; Bytespider; spider-feedback@bytedance.com)",
    # 分平台展示。厂商没有单独公布爬虫说明；token 取公开 robots 清单里的写法。
    # 非 200 只让对应平台显示不可达，不把「AI 爬虫实测可达」打成 P0。
    "DoubaoBot": "Mozilla/5.0 (compatible; DoubaoBot/1.0; +https://www.doubao.com/)",
    "DeepSeekBot": "Mozilla/5.0 (compatible; DeepSeekBot/1.0; +https://www.deepseek.com/)",
    "MiniMaxBot": "Mozilla/5.0 (compatible; MiniMaxBot/1.0; +https://www.minimax.io/)",
}

DISPLAY_ONLY_BOTS = {"DoubaoBot", "DeepSeekBot", "MiniMaxBot"}

# 平台 → 代表性 UA 映射（对齐 geo.new 的"分平台就绪度"矩阵，★ v8 新增）
# 每个平台只要有一条 UA 实测可达，即视为该平台"可读"。
PLATFORMS = {
    "Google AI 概览": ["Googlebot"],
    "Gemini": ["Googlebot"],
    "ChatGPT Search": ["OAI-SearchBot", "ChatGPT-User"],
    "Perplexity": ["PerplexityBot"],
    "Claude": ["ClaudeBot", "Claude-SearchBot"],
    "Copilot/Bing": ["bingbot"],
    "豆包": ["DoubaoBot"],
    "DeepSeek": ["DeepSeekBot"],
    "MiniMax": ["MiniMaxBot"],
}

DEFAULT_UA = "Mozilla/5.0 (compatible; geo_check/1.0; GEO audit tool)"

# 权威外链域名（用于"引用权威外链"启发式，§13.1 方法 1）
AUTHORITATIVE_DOMAINS = (
    ".gov", ".edu", "wikipedia.org", "wikidata.org", "lenovo.com", "dell.com",
    "apple.com", "hp.com", "asus.com", "intel.com", "amd.com", "nvidia.com",
    "github.com", "arxiv.org", "stackoverflow.com",
)

# 内置站点清单（--all 示例）。仅含公开 URL，不含服务器归属或本机路径。
SITE_PROFILES = [
    {"name": "hqbsh.com",  "url": "https://www.hqbsh.com/",  "cms": "Discuz! X5.0"},
    {"name": "hqbnb.com",  "url": "https://www.hqbnb.com/",  "cms": "WordPress+SlimSEO"},
    {"name": "mkcmd.com",  "url": "https://www.mkcmd.com/",  "cms": "WordPress+SlimSEO"},
    {"name": "sj868.cn",   "url": "https://www.sj868.cn/",   "cms": "WordPress+SlimSEO"},
    {"name": "szbjb.com",  "url": "https://www.szbjb.com/",  "cms": "WordPress+SlimSEO"},
    {"name": "xgbjb.com",  "url": "https://www.xgbjb.com/",  "cms": "Discuz"},
    {"name": "hqbsh.net",  "url": "https://www.hqbsh.net/",  "cms": "WordPress"},
    {"name": "lecoo.cc",   "url": "https://www.lecoo.cc/",   "cms": "WordPress+SlimSEO"},
    {"name": "openbjb.com", "url": "https://www.openbjb.com/", "cms": "WordPress"},
    {"name": "sjpdd.com",  "url": "https://www.sjpdd.com/",  "cms": "WordPress"},
    {"name": "lsdiy.com",  "url": "https://www.lsdiy.com/",  "cms": "自建(Laravel)"},
    {"name": "lenovopc.net", "url": "https://www.lenovopc.net/", "cms": "PHP-FPM"},
    {"name": "thinkpadpc.com", "url": "https://www.thinkpadpc.com/", "cms": "PHP-FPM"},
]

TIMEOUT = 20


# ---------------------------------------------------------------------------
# HTTP 抓取（标准库；返回状态/头/正文/HTTP 版本）
# ---------------------------------------------------------------------------

def fetch(url, ua=DEFAULT_UA, accept_encoding=None, timeout=TIMEOUT):
    """发 GET，返回 dict：{ok,status,headers,body,version,error,url,requested_url,redirects}。

    自动跟随 3xx。url 为最终落地地址；redirects 为 [{status,from,to}, …]。
    永不抛异常。
    """
    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua)
    if accept_encoding:
        req.add_header("Accept-Encoding", accept_encoding)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 体检场景容忍自签/链问题
    redirects = []

    class _RecordRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            try:
                src = req.get_full_url()
            except Exception:
                src = url
            redirects.append({"status": int(code), "from": src, "to": newurl})
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(
        _RecordRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            body = _decode_body(raw, resp.headers.get("Content-Encoding"))
            version = getattr(resp, "version", None)  # 10=1.0, 11=1.1
            final = resp.geturl() or url
            return {
                "ok": True,
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": body,
                "version": version,
                "error": "",
                "url": final,
                "requested_url": url,
                "redirects": list(redirects),
            }
    except urllib.error.HTTPError as e:
        # 仍尽量读取 body（robots/sitemap 404 等有意义）
        try:
            raw = e.read()
        except Exception:
            raw = b""
        final = url
        try:
            final = e.geturl() or url
        except Exception:
            pass
        return {
            "ok": False,
            "status": e.code,
            "headers": dict(e.headers or {}),
            "body": _decode_body(raw, None),
            "version": None,
            "error": "HTTPError",
            "url": final,
            "requested_url": url,
            "redirects": list(redirects),
        }
    except Exception as e:  # 超时/连接/SSL/解析
        return {
            "ok": False,
            "status": None,
            "headers": {},
            "body": "",
            "version": None,
            "error": "%s: %s" % (type(e).__name__, e),
            "url": url,
            "requested_url": url,
            "redirects": list(redirects),
        }


def _decode_body(raw, content_encoding):
    """按 Content-Encoding 解压并按 charset 解码（仅用于文本分析）。"""
    if raw is None:
        return ""
    data = raw
    try:
        enc = (content_encoding or "").lower()
        if "gzip" in enc:
            data = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                data = gzip.decompress(raw)
            except Exception:
                data = raw
        # br（brotli）标准库不支持解压：不解，按原始处理（不影响头检测）
    except Exception:
        data = raw
    for cs in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return data.decode(cs)
        except Exception:
            continue
    return ""


def head(url, timeout=TIMEOUT):
    """只取响应头（用于压缩/版本检测，避免下载大文件）。"""
    return fetch(url, accept_encoding="gzip, br", timeout=timeout)


def _json_request(url, method="GET", payload=None, headers=None, timeout=TIMEOUT):
    """标准库发 JSON 请求；返回 (status, data_dict, resp_headers)。失败时 data 为 None。

    注意：不发送 Origin / Sec-Fetch-*（geo.new 首次无 token 请求会拒绝浏览器型请求）；
    但必须带 User-Agent，否则被 Cloudflare 以 Error 1010 拦截。
    """
    h = {"Accept": "application/json", "User-Agent": DEFAULT_UA}
    if headers:
        h.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in h.items():
        req.add_header(k, v)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                obj = None
            return resp.status, obj, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            obj = None
        return e.code, obj, dict(e.headers or {})
    except Exception:
        return None, None, {}


# ---------------------------------------------------------------------------
# HTML 解析：统计语义标签 / meta / script(JSON-LD) / link / html 属性
# ---------------------------------------------------------------------------

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag_counts = {}
        self.html_attrs = {}       # 第一个 <html> 的属性（主文档）
        self._first_html_seen = False
        self.metas = []          # 每个 <meta> 的属性 dict
        self.links = []          # 每个 <link> 的属性 dict
        self.jsonld = []         # JSON-LD 原始字符串
        self.times = []          # <time datetime=...>
        self.imgs = []           # 每个 <img> 的属性 dict（★ v10：CLS / Agent-ready）
        # ★ v11：<h1> 文本 / 出现位置；body 内 <meta>
        self.h1_texts = []
        self.meta_in_body = 0    # 出现在 </head> 之后的 <meta> 数量
        self._in_body = False
        self._in_h1 = False
        self._h1_buf = []
        self._in_jsonld = False
        self._jsonld_buf = []
        # ★ 观察加厚：title / h1–h6 / script
        self.title_text = ""
        self.headings = []       # [(level, text), ...]
        self.scripts = []        # [{src, inline_len}]
        self._in_title = False
        self._title_buf = []
        self._in_hx = 0          # 1–6 while inside heading
        self._hx_buf = []
        self._in_script = False
        self._script_src = ""
        self._script_buf = []
        self._script_is_jsonld = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        if tag == "html" and not self._first_html_seen:
            # 只记第一个 <html> —— 主文档属性（嵌套时第二个不算）
            self.html_attrs = a
            self._first_html_seen = True
        elif tag == "body":
            self._in_body = True
        elif tag == "title":
            self._in_title = True
            self._title_buf = []
        elif tag == "h1":
            self._in_h1 = True
            self._h1_buf = []
            self._in_hx = 1
            self._hx_buf = []
        elif tag in ("h2", "h3", "h4", "h5", "h6"):
            self._in_hx = int(tag[1])
            self._hx_buf = []
        elif tag == "meta":
            self.metas.append(a)
            if self._in_body:
                self.meta_in_body += 1
        elif tag == "link":
            self.links.append(a)
        elif tag == "time":
            self.times.append(a.get("datetime", ""))
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "script":
            stype = a.get("type", "").lower()
            self._script_is_jsonld = (stype == "application/ld+json")
            if self._script_is_jsonld:
                self._in_jsonld = True
                self._jsonld_buf = []
            self._in_script = True
            self._script_src = a.get("src", "")
            self._script_buf = []

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
        if self._in_h1:
            self._h1_buf.append(data)
        if self._in_title:
            self._title_buf.append(data)
        if self._in_hx:
            self._hx_buf.append(data)
        if self._in_script and not self._script_src:
            self._script_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            if self._in_jsonld:
                self.jsonld.append("".join(self._jsonld_buf))
                self._in_jsonld = False
            if self._in_script:
                inline = "".join(self._script_buf)
                self.scripts.append({
                    "src": self._script_src,
                    "inline_len": 0 if self._script_src else len(inline),
                    "jsonld": self._script_is_jsonld,
                })
            self._in_script = False
            self._script_src = ""
            self._script_buf = []
            self._script_is_jsonld = False
        elif tag == "title" and self._in_title:
            self.title_text = re.sub(r"\s+", " ", "".join(self._title_buf)).strip()
            self._in_title = False
            self._title_buf = []
        elif tag == "h1" and self._in_h1:
            text = re.sub(r"\s+", " ", "".join(self._h1_buf)).strip()
            self.h1_texts.append(text)
            self.headings.append((1, text))
            self._in_h1 = False
            self._h1_buf = []
            self._in_hx = 0
            self._hx_buf = []
        elif tag in ("h2", "h3", "h4", "h5", "h6") and self._in_hx:
            text = re.sub(r"\s+", " ", "".join(self._hx_buf)).strip()
            self.headings.append((self._in_hx, text))
            self._in_hx = 0
            self._hx_buf = []


def parse_html(html):
    p = PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return p


def count_raw(html, pattern):
    """原始正则计数（用于嵌套文档等 §五 检测 8）。"""
    return len(re.findall(pattern, html, re.I))


# ---------------------------------------------------------------------------
# 单项检测：全部返回 {status: bool, value, expected, evidence, level}
# ---------------------------------------------------------------------------

def _ok(value, expected, evidence="", level="P0"):
    return {"status": True, "known": True, "value": value, "expected": expected, "evidence": evidence, "level": level}


def _fail(value, expected, evidence="", level="P0"):
    return {"status": False, "known": True, "value": value, "expected": expected, "evidence": evidence, "level": level}


def _unknown(value, expected, evidence="", level="P2"):
    """无法判定（网络错误 / 不适用）：既不计 0 也不计满分，只拉低置信度（§一.6）。"""
    return {"status": False, "known": False, "value": value, "expected": expected,
            "evidence": evidence, "level": level}


def _is_conn_error(resp):
    """该 HTTP 响应是否为『连接层失败』（拿不到任何状态码）——用于判 unknown。"""
    return isinstance(resp, dict) and resp.get("status") is None and not resp.get("ok")


def check_jsonld(html, parser):
    """§五 检测 1 + 13：JSON-LD 数量与合法性。"""
    blocks = parser.jsonld
    types, invalid = [], 0
    for b in blocks:
        try:
            d = json.loads(b)
            types.extend(_extract_types(d))
        except Exception:
            invalid += 1
    valid = len(blocks) - invalid
    ev = "块数=%d 合法=%d 类型=%s" % (len(blocks), valid, ",".join(sorted(set(types))) or "无")
    res = _ok(len(blocks), "≥1", ev) if (blocks and invalid == 0) else _fail(len(blocks), "≥1", ev, "P0")
    res["types"] = sorted(set(types))
    res["invalid"] = invalid
    return res


def _extract_types(obj):
    """递归提取 @type（含 @graph）。"""
    out = []
    if isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend([x for x in t if isinstance(x, str)])
        for v in obj.values():
            if isinstance(v, (dict, list)):
                out.extend(_extract_types(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_extract_types(v))
    return out


def check_semantic_tags(parser):
    """§五 检测 2：语义化标签。"""
    need = ["main", "article", "footer", "header", "nav", "time"]
    counts = {t: parser.tag_counts.get(t, 0) for t in need}
    has = all(counts.get(t, 0) >= 1 for t in ("main", "article", "footer", "time"))
    ev = " ".join("%s=%d" % (k, v) for k, v in counts.items())
    return _ok(counts, "main/article/footer/time ≥1", ev, "P1") if has else \
        _fail(counts, "main/article/footer/time ≥1", ev, "P1")


def check_main_heading(parser):
    """★ v11：主标题 <h1>（对齐 geo.new S07/G06「Main heading / 内容组织」）。

    每个页面应有且仅有一个描述主主题的 <h1>：
      · 0 个 → AI 难以判定页面主主题（geo.new 判 S07 fail）；
      · ≥1 个 → 通过，但多 H1 时给出提示证据。
    注意：按 **<h1> 标签数**计数（logo 型 h1 内只有 <img>、无文字，也算有效）。
    """
    n = int(parser.tag_counts.get("h1", 0) or 0)
    texts = [t for t in (parser.h1_texts or []) if t]
    first = (texts[0][:60] if texts else "(仅图像/无文本)")
    ev = "h1=%d%s" % (n, ("；首个=「%s」" % first) if n else "")
    if n >= 1:
        if n > 1:
            ev += "（⚠️ 多主标题，建议只保留一个页面级 <h1>）"
        return _ok(n, "≥1 个 <h1>", ev, "P2")
    return _fail(n, "≥1 个 <h1>", ev, "P2")


def check_meta_in_body(parser):
    """★ v11：<meta> 不得出现在 <body> 内（对齐 geo.new HTML-06）。

    body 内的 <meta>（常见于主题/编辑器把标签误插到正文）会被浏览器部分忽略，
    属 HTML 结构错误，且可能干扰解析。
    """
    n = int(getattr(parser, "meta_in_body", 0) or 0)
    ev = "body 内 <meta>=%d（应全部在 <head>）" % n
    return _ok(n, "0", ev, "P2") if n == 0 else _fail(n, "0", ev, "P2")


# 安全加固响应头（对齐 geo.new SEC-03；CSP 或 frame-ancestors 二者其一即可满足 X-Frame 项）
SECURITY_HEADERS = (
    ("content-security-policy", "CSP 或 frame-ancestors"),
    ("x-content-type-options", "X-Content-Type-Options: nosniff"),
    ("referrer-policy", "Referrer-Policy"),
    ("permissions-policy", "Permissions-Policy"),
)


def check_security_headers(resp):
    """★ v11：安全响应头（对齐 geo.new SEC-03）。

    resp 为首页 fetch 的返回 dict（需含 headers）。检查标准加固头是否下发：
    CSP（或 frame-ancestors）、X-Content-Type-Options、Referrer-Policy、Permissions-Policy。
    至少下发 3/4 即通过（不强制 CSP 的严格策略）。
    """
    h = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
    present = [label for key, label in SECURITY_HEADERS if h.get(key)]
    has_frame = bool(h.get("x-frame-options")) or ("frame-ancestors" in (h.get("content-security-policy", "") or ""))
    need = 4
    got = len(present) + (1 if (has_frame and not h.get("content-security-policy")) else 0)
    ev = "已下发=%s（%d/4）" % ("、".join(present) or "无", min(got, need))
    return _ok(got, "≥3 项安全头", ev, "P2") if got >= 3 else _fail(got, "≥3 项安全头", ev, "P2")


def check_canonical(parser):
    """§五 检测 3：canonical。"""
    found = [l.get("href", "") for l in parser.links if "canonical" in l.get("rel", "").lower()]
    ev = "canonical=%s" % (found[0] if found else "无")
    return _ok(found, "有 canonical", ev, "P1") if found else \
        _fail(False, "有 canonical", ev, "P1")


def check_og(parser):
    """§五 检测 4：Open Graph / Twitter Card。"""
    og = [m for m in parser.metas if m.get("property", "").startswith("og:")]
    tw = [m for m in parser.metas if m.get("name", "").startswith("twitter:")]
    n = len(og) + len(tw)
    ev = "og=%d twitter=%d" % (len(og), len(tw))
    return _ok(n, "≥4", ev, "P1") if n >= 4 else _fail(n, "≥4", ev, "P1")


def check_lang(parser):
    """§五 检测 9：<html lang>。"""
    lang = parser.html_attrs.get("lang", "")
    return _ok(lang, "含 lang", "lang=%s" % (lang or "无"), "P0") if lang else \
        _fail("", "含 lang", "lang=无", "P0")


def check_nested_doc(html):
    """§五 检测 8：嵌套文档（<html>/<head>/<!DOCTYPE> 各应恰好 1 次）。"""
    n_html = count_raw(html, r"<html[\s>]")
    n_head = count_raw(html, r"<head[\s>]")
    n_dt = count_raw(html, r"<!DOCTYPE")
    total = n_html + n_head + n_dt
    ev = "<html>=%d <head>=%d <!DOCTYPE>=%d 合计=%d（期望 3）" % (n_html, n_head, n_dt, total)
    good = (n_html == 1 and n_head == 1 and n_dt == 1)
    return _ok(total, "3", ev, "P0") if good else _fail(total, "3", ev, "P0")


def check_compression(url):
    """§五 检测 5：压缩（gzip/br）。连接层失败 → unknown（§一.6）。"""
    r = head(url)
    if _is_conn_error(r):
        return _unknown("", "含 gzip 或 br", "抓取失败，无法判定压缩（unknown）", "P2")
    ce = r["headers"].get("Content-Encoding", "")
    ev = "Content-Encoding=%s" % (ce or "无")
    return _ok(ce, "含 gzip 或 br", ev, "P2") if ce else _fail(ce, "含 gzip 或 br", ev, "P2")


def check_http_version(url):
    """§五 检测 6：协议版本。

    urllib 只能到 HTTP/1.1，无法识别 h2。优先用系统 curl（可探测 h2），
    无 curl 时退回 urllib 结果，并明确标注"未探测"避免恒判失败。
    """
    ver, note = _http_version_via_alpn(url)
    if ver is None:
        ver = _http_version_via_curl(url)
        if ver is not None:
            note = "（curl 判定）"
        else:
            r = fetch(url, timeout=TIMEOUT)
            if _is_conn_error(r):
                return _unknown("", "2 或 3（非 1.1）", "抓取失败，无法判定协议版本（unknown）", "P2")
            v = r.get("version")
            ver = {10: "1.1", 11: "1.1"}.get(v, "未知")  # urllib 无 h2 能力
            note = "（urllib 无法探测 h2，建议复核）"
    ev = "http_version=%s%s" % (ver, note)
    good = ver in ("2", "2.0", "3", "3.0")
    return _ok(ver, "2 或 3（非 1.1）", ev, "P2") if good else \
        _fail(ver, "2 或 3（非 1.1）", ev, "P2")


def _http_version_via_alpn(url):
    """TLS ALPN 握手判定 h2（权威，不依赖 curl 是否带 HTTP2 特性）。

    返回 (ver, note)；无法握手返回 (None, None)。
    """
    import socket
    import ssl
    import urllib.parse
    u = urllib.parse.urlparse(url)
    if u.scheme != "https":
        return None, None
    try:
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        with socket.create_connection((u.hostname, u.port or 443), timeout=15) as s:
            with ctx.wrap_socket(s, server_hostname=u.hostname) as ss:
                sel = ss.selected_alpn_protocol()
    except Exception:
        return None, None
    if sel == "h2":
        return "2", "（ALPN 选中 h2）"
    if sel == "http/1.1":
        return "1.1", "（ALPN 仅 1.1）"
    return None, None


def _http_version_via_curl(url):
    """用 curl 探测 http 版本（curl 能区分 h2）；不可用返回 None。"""
    import shutil
    import subprocess
    if not shutil.which("curl"):
        return None
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_version}", "--max-time", "15", url],
            capture_output=True, text=True, timeout=25)
        v = (out.stdout or "").strip()
        return v or None
    except Exception:
        return None


def _parse_robots(body):
    """把 robots.txt 解析为组列表：[{"agents":[..], "allow":bool_covers_root, "disallow_root":bool}]。

    简化实现：按连续 User-agent 行分组，规则聚合到组。
    """
    groups = []
    cur = None
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"(?i)^User-agent:\s*(\S+)", line)
        if m:
            agent = m.group(1).strip()
            if cur is None or cur["closed"]:
                cur = {"agents": [agent], "allow_root": False, "disallow_root": False, "closed": False}
                groups.append(cur)
            else:
                cur["agents"].append(agent)
            continue
        if cur is None:
            continue
        m = re.match(r"(?i)^(Allow|Disallow):\s*(\S*)", line)
        if m:
            field, path = m.group(1).lower(), m.group(2)
            if path in ("/", "*"):
                if field == "disallow":
                    cur["disallow_root"] = True
                else:
                    cur["allow_root"] = True
            cur["closed"] = True  # 规则行之后，新的 User-agent 开新组
    return groups


def _robots_blocked(groups, bot):
    """该 bot 是否被明确阻断（/ 全禁）。"""
    # 1) 针对该 bot 的组
    for g in groups:
        if bot in g["agents"] and g["disallow_root"] and not g["allow_root"]:
            return True
    # 2) 通配组全禁（且无该 bot 专属放行）
    has_own_allow = any(bot in g["agents"] and g["allow_root"] for g in groups)
    if not has_own_allow:
        for g in groups:
            if "*" in g["agents"] and g["disallow_root"] and not g["allow_root"]:
                return True
    return False


def check_robots(base):
    """§五 检测 10 + §四：robots 存在、声明 Sitemap、引用型爬虫放行情况。

    分级（对齐 §〇.2"阻断优先"）：
      · 被明确阻断（Disallow: /）        → P0（致命，直接断掉被引用来源）
      · 未被阻断但未"逐条显式 Allow"     → P1（优化项；实测可达，非阻断）
    """
    r = fetch(base.rstrip("/") + "/robots.txt")
    if _is_conn_error(r):
        return {"exists": _unknown(False, "存在", "抓取失败，无法判定 robots（unknown）", "P1"),
                "sitemap": _unknown(False, "有 Sitemap 行", "抓取失败，无法判定（unknown）", "P1"),
                "citation_allowed": _unknown([], "引用型未被阻断", "抓取失败，无法判定（unknown）", "P1"),
                "training_policy": _unknown(False, "训练爬虫策略", "抓取失败（unknown）", "P3"),
                "crawl_delay": _unknown(False, "Crawl-delay", "抓取失败（unknown）", "P3")}
    body = r["body"]
    if not (r["ok"] or r["status"] == 200):
        return {"exists": _fail(False, "存在", "HTTP %s" % r["status"], "P1"),
                "sitemap": _fail(False, "有 Sitemap 行", "robots 不可用", "P1"),
                "citation_allowed": _fail([], "引用型未被阻断", "robots 不可用", "P1"),
                "training_policy": _fail(False, "训练爬虫策略", "robots 不可用", "P3"),
                "crawl_delay": _fail(False, "Crawl-delay", "robots 不可用", "P3")}
    has_sitemap = bool(re.search(r"(?im)^\s*Sitemap:", body))
    groups = _parse_robots(body)
    blocked, explicit, reachable = [], [], []
    for bot in CITATION_BOTS:
        if _robots_blocked(groups, bot):
            blocked.append(bot)
        else:
            reachable.append(bot)
            seg = _robots_segment(body, bot)
            if seg is not None and re.search(r"(?im)^\s*Allow:\s*/", seg):
                explicit.append(bot)
    if blocked:
        cita = _fail(blocked, "引用型未被阻断",
                     "被 Disallow: / 阻断=%s；可达=%s" % (",".join(blocked), ",".join(reachable) or "无"), "P0")
    elif len(explicit) == len(CITATION_BOTS):
        cita = _ok(explicit, "引用型逐条显式 Allow",
                   "全部显式 Allow=%s" % ",".join(explicit), "P0")
    else:
        cita = _fail([b for b in CITATION_BOTS if b not in explicit], "引用型逐条显式 Allow",
                     "实测可达但未逐条显式 Allow（缺=%s）；靠 User-agent: * 兜底" %
                     ",".join(b for b in CITATION_BOTS if b not in explicit), "P1")
    # 训练型策略（观察用，不计分）：是否对 TRAINING_BOTS 写了独立段落
    train_named, train_blocked = [], []
    for bot in TRAINING_BOTS[:6]:
        seg = _robots_segment(body, bot)
        if seg is not None:
            train_named.append(bot)
            if _robots_blocked(groups, bot):
                train_blocked.append(bot)
    train_ev = "独立段落=%s；其中 Disallow:/=%s" % (
        ",".join(train_named) or "无", ",".join(train_blocked) or "无")
    # 有独立策略（无论放行或拒绝）即视为「已声明训练策略」
    train_ok = bool(train_named)
    training_policy = _ok(True, "训练爬虫有独立策略", train_ev, "P3") if train_ok else \
        _fail(False, "训练爬虫有独立策略", train_ev + "（可选用 User-agent 段声明 GPTBot 等）", "P3")
    delay_m = re.search(r"(?im)^\s*Crawl-delay:\s*(\S+)", body)
    if delay_m:
        crawl_delay = _ok(delay_m.group(1), "Crawl-delay 声明", "Crawl-delay=%s" % delay_m.group(1), "P3")
    else:
        crawl_delay = _ok(True, "无强制 Crawl-delay", "未声明 Crawl-delay（常见默认）", "P3")
    return {
        "exists": _ok(True, "存在", "robots 可访问", "P1"),
        "sitemap": _ok(True, "有 Sitemap 行", "Sitemap 已声明", "P1") if has_sitemap else
                   _fail(False, "有 Sitemap 行", "未声明 Sitemap", "P1"),
        "citation_allowed": cita,
        "training_policy": training_policy,
        "crawl_delay": crawl_delay,
    }


def _robots_segment(body, bot):
    """返回 robots 中指定 bot 的 User-agent 段落文本；无独立段落返回 None。"""
    lines = body.splitlines()
    seg, capturing = [], False
    for ln in lines:
        m = re.match(r"(?i)^\s*User-agent:\s*(\S+)", ln)
        if m:
            if capturing:
                break
            capturing = (m.group(1).strip() == bot)
            continue
        if capturing:
            seg.append(ln)
    return "\n".join(seg) if capturing else None


def check_sitemap(base):
    """§五 检测 12：sitemap 存在与时效（支持 sitemap index）。"""
    r = fetch(base.rstrip("/") + "/sitemap.xml")
    if _is_conn_error(r):
        return {"exists": _unknown(False, "存在", "抓取失败，无法判定 sitemap（unknown）", "P0"),
                "fresh": _unknown(False, "lastmod 新鲜", "抓取失败，无法判定（unknown）", "P2"), "latest": ""}
    if not (r["ok"] or r["status"] == 200):
        return {"exists": _fail(False, "存在", "HTTP %s" % r["status"], "P0"),
                "fresh": _fail(False, "lastmod 新鲜", "不可用", "P2"), "latest": ""}
    body = r["body"]
    lastmods = re.findall(r"<lastmod>\s*([^<\s]+)", body)
    # 若为 sitemap index，抓第一个子 sitemap 再取 lastmod
    if not lastmods:
        sub = re.search(r"<loc>\s*([^<\s]+sitemap[^<\s]*\.xml)", body)
        if sub:
            sb = fetch(sub.group(1))
            if sb["body"]:
                lastmods = re.findall(r"<lastmod>\s*([^<\s]+)", sb["body"])
    latest = max(lastmods) if lastmods else ""
    fresh = False
    if latest:
        try:
            dt = datetime.datetime.fromisoformat(latest.replace("Z", "+00:00")[:19])
            fresh = (datetime.datetime.now() - dt.replace(tzinfo=None)).days <= 90
        except Exception:
            fresh = False
    n_url = len(re.findall(r"<loc>", body))
    ev = "URL 数=%d 最新 lastmod=%s" % (n_url, latest or "无")
    exists = _ok(True, "存在", ev, "P0")
    if latest:
        fresh_res = _ok(fresh, "最近 90 天内有更新", ev, "P2") if fresh else \
            _fail(False, "最近 90 天内有更新", ev, "P2")
    else:
        # 无 lastmod 信号：判不新鲜但不误报"陈旧"
        fresh_res = _fail(False, "含 lastmod 且最近 90 天内有更新", ev + "（未提供 lastmod）", "P2")
    return {"exists": exists, "fresh": fresh_res, "latest": latest}


def check_ai_crawler_reach(url):
    """§五 检测 11 + §四：AI 爬虫实测连通性（**按爬虫分级**判定）。

    §四 把爬虫分为「引用型 / 检索型」与「训练型」：
      · **引用型（CITATION_BOTS）/ 检索型** 被非 200 拦截 → **P0**（断掉"被引用"来源）；
      · **训练型（TRAINING_BOTS）** 被拦 → **不算问题**（放行与否自行权衡），仅作信息提示；
      · 连接层失败（None，重试仍失败）→ unknown（本机网络，§一.6）。
    """
    results = {}
    blocked_critical, blocked_training, conn_err = [], [], []
    for name, ua in PROBE_UAS.items():
        status = None
        for _ in range(3):  # 最多 3 次，抵御瞬时抖动
            r = fetch(url, ua=ua)
            status = r["status"]
            if status is not None:
                break
        results[name] = status
        if name in DISPLAY_ONLY_BOTS:
            continue
        if status is None:
            conn_err.append(name)
        elif status != 200:
            (blocked_training if name in TRAINING_BOTS else blocked_critical).append(name)
    ev = " ".join("%s=%s" % (k, v if v is not None else "连接错误") for k, v in results.items())
    if blocked_training:
        ev += "（训练型 %s 被拦，属可选，不计问题；§四）" % ",".join(blocked_training)
    if blocked_critical:
        return _fail(blocked_critical, "引用型/检索型全部 200",
                     ev + "（引用/检索型被拦截）", "P0")
    if conn_err:
        return _unknown(conn_err, "引用型/检索型全部 200",
                        ev + "（连接错误，无法判定，疑本机网络）", "P2")
    return _ok(results, "引用型/检索型全部 200", ev, "P0")


# ---- 内容可引用性（启发式，§一.4 / §13.1 / §13.4）---------------------------

def check_content_citability(html, parser):
    """启发式评估：答案前置 / 表格与外链 / time + 价格日期。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # ① 答案前置 / 答案胶囊：正文前部存在 20–300 字的完整陈述段（或含 FAQ/QA）
    front = ""
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
    paras = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
    paras = [p for p in paras if 20 <= len(p) <= 300]
    if paras:
        front = paras[0]
    types = _extract_types_of(parser)
    has_faq = any(t in ("FAQPage", "QAPage") for t in types)
    has_answer = bool(front) or has_faq

    # ② 数据表格 / 权威外链
    # 只把"含 <th> 或 <thead> 的表"视为数据表，排除 Discuz 布局表（cellspacing/cellpadding）
    data_table = (parser.tag_counts.get("th", 0) >= 1 or
                  parser.tag_counts.get("thead", 0) >= 1)
    ext_auth = 0
    for m2 in re.finditer(r'href=["\'](https?://[^"\']+)', html, re.I):
        u = m2.group(1).lower()
        if any(d in u for d in AUTHORITATIVE_DOMAINS):
            ext_auth += 1
    has_table_links = data_table or ext_auth > 0

    # ③ <time datetime> 与价格日期标注
    has_time = len([t for t in parser.times if t]) >= 1
    price_date = bool(re.search(r"20\d{2}[.\-年/]\s*\d{1,2}", text)) and \
                 bool(re.search(r"(价|￥|¥|元)", text))
    has_time_price = has_time and price_date

    return {
        "answer_first": _ok(True, "有答案前置/FAQ", "首段=%.40s" % (front or "FAQ"), "P1")
                        if has_answer else
                        _fail(False, "有答案前置/FAQ", "未识别到 20–300 字首段或 FAQ", "P1"),
        "table_links": _ok(True, "有数据表格或权威外链",
                           "数据表(th/thead)=%s 权威外链=%d" % (data_table, ext_auth), "P2")
                       if has_table_links else
                       _fail(False, "有数据表格或权威外链", "数据表=无 权威外链=0", "P2"),
        "time_price": _ok(True, "<time> + 价格日期",
                          "time=%d 价格日期=%s" % (len(parser.times), price_date), "P1")
                      if has_time_price else
                      _fail(False, "<time> + 价格日期",
                            "time=%d 价格日期=%s（需同时满足）" % (len(parser.times), price_date), "P1"),
    }


def _extract_types_of(parser):
    types = []
    for b in parser.jsonld:
        try:
            types.extend(_extract_types(json.loads(b)))
        except Exception:
            pass
    return types


# ---- 实体信号（对齐 geo.new 的 Entity Signals 轴，★ v8 新增）-----------------

def _jsonld_nodes(parser):
    """把所有 JSON-LD 解析为若干"节点 dict"（递归展开 @graph / 数组）。"""
    nodes = []

    def walk(obj):
        if isinstance(obj, dict):
            if "@type" in obj:
                nodes.append(obj)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for b in parser.jsonld:
        try:
            walk(json.loads(b))
        except Exception:
            pass
    return nodes


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _node_name(node):
    n = node.get("name") or node.get("legalName") or ""
    return n if isinstance(n, str) else ""


def check_entity_signals(parser):
    """实体信号：Organization 的 sameAs / @id / name / url（§13.2 知识图谱四支柱）。

    返回 dict，每个子项为独立 check（供评分与问题清单引用）。
    分级：Organization 缺失或 sameAs 缺失 → P1（显著影响"这是什么实体"的判定）。
    """
    nodes = _jsonld_nodes(parser)
    # 兼容无 @type 但含 @graph 的写法的兜底：直接扫描 org-ish 字段
    org = None
    for nd in nodes:
        if "Organization" in _as_list(nd.get("@type")):
            org = nd
            break

    if org is None:
        org_missing = _fail(False, "有 Organization 实体", "未发现 Organization 节点", "P1")
        return {
            "org": org_missing,
            "sameas": _fail(False, "≥2 条 sameAs", "无 Organization，无法评估", "P1"),
            "id": _fail(False, "Organization 有 @id", "无 Organization，无法评估", "P1"),
            "profile": _fail(False, "有 name 且关联 url", "无 Organization，无法评估", "P1"),
        }

    same_as = _as_list(org.get("sameAs"))
    has_id = bool(org.get("@id"))
    has_name = bool(_node_name(org))
    has_url = bool(org.get("url"))

    return {
        "org": _ok(True, "有 Organization 实体", "Organization 已声明", "P1"),
        "sameas": _ok(len(same_as), "≥2 条 sameAs",
                      "sameAs=%d %s" % (len(same_as), (same_as[:3] if same_as else "无")), "P1")
                  if len(same_as) >= 2 else
                  _fail(len(same_as), "≥2 条 sameAs",
                        "sameAs=%d（权威源如官微/百科/工商登记）" % len(same_as), "P1"),
        "id": _ok(bool(has_id), "Organization 有 @id",
                  "has @id" if has_id else "无 @id（实体无稳定标识）", "P1") if has_id else
              _fail(False, "Organization 有 @id", "无 @id（实体无稳定标识）", "P1"),
        "profile": _ok(bool(has_name and has_url), "有 name 且关联 url",
                       "name=%s url=%s" % (_node_name(org) or "无", org.get("url") or "无"), "P1")
                   if (has_name and has_url) else
                   _fail(False, "有 name 且关联 url",
                         "name=%s url=%s" % (_node_name(org) or "无", org.get("url") or "无"), "P1"),
    }


def check_platform_readiness(ai_crawl):
    """分平台就绪度：把爬虫实测结果映射为各 AI 平台"可读/不可读"矩阵（★ v8 新增）。

    输入为 check_ai_crawler_reach 的返回（其 value 是 {UA: status} 字典）。
    仅作展示；不直接参与 0–100 计分（避免与"AI 爬虫实测可达"重复计分）。
    """
    raw = ai_crawl.get("value")
    results = raw if isinstance(raw, dict) else {}   # 网络全失败时 value 为 list，兼容之
    matrix = {}
    for plat, uas in PLATFORMS.items():
        st = [results.get(ua) for ua in uas]
        ok = any(s == 200 for s in st)
        matrix[plat] = {"ok": ok, "uas": uas, "status": st}
    return matrix


# ---- AI 发现端点 / Agent-ready / 注入自查（★ v10 新增）----------------------

# 端点 → (路径, 标签, 满分微调值)；对齐 §三 S 与 §17.2
AI_ENDPOINTS = [
    ("/llms.txt",               "llms.txt",        2),   # Google 忽略但 agent 生态已落地
    ("/.well-known/ai.txt",     "ai.txt",          1),
    ("/ai/summary.json",        "ai/summary.json", 1),
    ("/ai/faq.json",            "ai/faq.json",     1),
    ("/ai/service.json",        "ai/service.json", 1),
]


def check_ai_endpoints(origin):
    """§三 S / §17.2：AI 发现端点（llms.txt、ai.txt、ai/*.json）是否存在。

    属「低优先可做」：做了对 Google 无益无害，对 agent / 其它系统有用。
    仅作展示 + **微调项**（合计最多 +5，见 §一.4），不计入基础 100。
    连接失败按「不存在」计（不阻断、不致 unknown）。
    """
    found, missing = [], []
    points = 0
    for path, label, wt in AI_ENDPOINTS:
        r = fetch(origin.rstrip("/") + path)
        ok = (r["status"] == 200) and bool((r["body"] or "").strip())
        if ok:
            found.append(label)
            points += wt
        else:
            missing.append("%s(HTTP %s)" % (label, r["status"]))
    ev = "已有=%s ｜ 缺=%s" % (",".join(found) or "无", ", ".join(missing) or "无")
    return {
        "known": True,
        "found": found,
        "missing": missing,
        "points": min(5, points),
        "evidence": ev,
    }


def check_injection_risks(html, parser):
    """§13.6：防御 GEO 投毒 / 注入 / 隐藏文字（高信号模式）。

    只收敛为**高置信**模式（提示注入短语 / data-ai-* 属性 / 不可见 Unicode）；
    CSS 隐藏文字仅作**提示**（display:none 在正常 UI 中很常见，不扣分）。
    命中 → P2 问题 + 微调 −5（§一.4 微调项）。
    """
    low = html.lower()
    hits = []
    for pat, desc in (("ignore previous instructions", "英文提示注入短语"),
                      ("ignore all previous", "英文提示注入短语"),
                      ("ignore the above", "英文提示注入短语"),
                      ("disregard previous", "英文提示注入短语"),
                      ("忽略以上", "中文提示注入短语"),
                      ("忽略前述", "中文提示注入短语")):
        if pat in low:
            hits.append(desc)
            break
    if re.search(r"\bdata-(ai|prompt|llm)-", low):
        hits.append("data-ai-*/data-prompt-*/data-llm-* 属性")
    if re.search(r"[\u200b\u200c\u200d\u202a-\u202e\u2060\ufeff]", html):
        hits.append("不可见 Unicode 控制符（零宽/双向）")
    css_hidden = bool(re.search(
        r'style="[^"]*(display\s*:\s*none|font-size\s*:\s*0|opacity\s*:\s*0)', low))
    found = bool(hits)
    ev = ("命中=%s" % "；".join(hits)) if found else "未发现高信号注入模式"
    if css_hidden:
        ev += "（另检测到 inline CSS 隐藏样式，正常 UI 常见，仅提示）"
    return {
        "known": True,
        "found": found,
        "hits": hits,
        "css_hidden_hint": css_hidden,
        "points": -5 if found else 0,
        "evidence": ev,
    }


def check_agent_ready(parser):
    """§17.1：Agent-ready 轻检（仅展示，不计分）。

    agent 依据元素定位做操作，**CLS（布局偏移）会破坏定位**；无尺寸图片是 CLS 主因之一。
    故统计 <img> 缺 width/height 的数量，作为「Agent 稳定性」信号。
    """
    total = len(parser.imgs)
    missing = [im for im in parser.imgs
               if not (im.get("width") and im.get("height"))]
    n_missing = len(missing)
    good = (total == 0) or (n_missing == 0)
    return {
        "known": True,
        "img_total": total,
        "img_missing_dims": n_missing,
        "ok": good,
        "evidence": "<img> 共 %d，缺 width/height %d（CLS/agent 定位风险）" % (total, n_missing),
    }


# ---- 观察项加厚（★ 不计分；默认不进入基础分）---------------------------------

DOM_SIZE_WARN = 1500  # 元素数超过此值给出提示
IMG_SAMPLE = 12       # 抽样检查现代格式的 <img> 上限


def check_obs_faq_visible(html, parser):
    """页内可见 FAQ/问答（非仅 JSON-LD FAQPage）。"""
    low = html.lower()
    hits = []
    if re.search(r"<details[\s>]", low) and re.search(r"<summary[\s>]", low):
        hits.append("details/summary")
    if re.search(r"(id|class)=[\"'][^\"']*(faq|问答|常见问题)[^\"']*[\"']", low):
        hits.append("faq 标记")
    if re.search(r"<h[1-6][^>]*>[^<]*(faq|常见问题|问与答)[^<]*</h[1-6]>", low):
        hits.append("标题含 FAQ")
    types = _extract_types_of(parser)
    only_ld = any(t in ("FAQPage", "QAPage") for t in types) and not hits
    ok = bool(hits)
    if only_ld:
        ev = "仅有 JSON-LD FAQPage/QAPage，正文未见可见问答区块"
    elif ok:
        ev = "可见信号=%s" % "、".join(hits)
    else:
        ev = "未见 details/FAQ 标记/问答标题"
    return _ok(True, "可见 FAQ", ev, "P2") if ok else _fail(False, "可见 FAQ", ev, "P2")


def check_obs_author_visible(html):
    """可见作者或组织署名（非仅 schema）。"""
    low = html.lower()
    hits = []
    if re.search(r"(itemprop|property|name)=[\"']author[\"']", low):
        hits.append("author 属性")
    if re.search(r"<[^>]*(class|rel)=[\"'][^\"']*(author|byline|作者)[^\"']*[\"']", low):
        hits.append("作者样式/rel")
    if re.search(r"(作者|编辑|撰稿|记者)\s*[：:：]", html):
        hits.append("中文署名")
    if re.search(r"\bby\s+[A-Z][a-z]+", html):
        hits.append("byline")
    ok = bool(hits)
    ev = ("可见信号=%s" % "、".join(hits)) if ok else "未见作者/组织署名标记"
    return _ok(True, "可见作者", ev, "P2") if ok else _fail(False, "可见作者", ev, "P2")


def check_obs_date_granular(html, parser):
    """可见完整更新日期（年月日）。"""
    times = [t for t in (parser.times or []) if t]
    full_dt = [t for t in times if re.search(r"20\d{2}-\d{2}-\d{2}", t)]
    text_hit = bool(re.search(r"20\d{2}\s*[年./\-]\s*\d{1,2}\s*[月./\-]?\s*\d{1,2}", html))
    ok = bool(full_dt) or text_hit
    ev = "time@datetime 全日=%d；正文年月日=%s" % (len(full_dt), text_hit)
    return _ok(True, "完整日期", ev, "P2") if ok else _fail(False, "完整日期", ev, "P2")


def check_obs_img_modern(parser):
    """抽样图片是否 WebP/AVIF（提示，不计分）。"""
    imgs = list(parser.imgs or [])[:IMG_SAMPLE]
    if not imgs:
        return _ok(True, "现代图片格式", "无 <img>，跳过", "P3")
    modern = 0
    for im in imgs:
        blob = " ".join([
            str(im.get("src") or ""),
            str(im.get("srcset") or ""),
            str(im.get("type") or ""),
        ]).lower()
        if ".webp" in blob or ".avif" in blob or "image/webp" in blob or "image/avif" in blob:
            modern += 1
    ok = modern > 0
    ev = "抽样 %d 张，含 WebP/AVIF %d" % (len(imgs), modern)
    return _ok(True, "现代图片格式", ev, "P3") if ok else _fail(False, "现代图片格式", ev, "P3")


def check_obs_dom_size(parser):
    """DOM 元素数量阈值提示（提示，不计分）。"""
    total = sum(int(v or 0) for v in (parser.tag_counts or {}).values())
    ok = total <= DOM_SIZE_WARN
    ev = "估算标签出现合计=%d（告警线 %d）" % (total, DOM_SIZE_WARN)
    return _ok(True, "DOM 体积", ev, "P3") if ok else _fail(False, "DOM 体积", ev, "P3")


def check_obs_title_length(parser):
    """标题长度（CORE-02）：建议约 15–65 字符。"""
    title = (parser.title_text or "").strip()
    n = len(title)
    ok = 15 <= n <= 65
    ev = "title 长度=%d%s" % (n, ("「%s」" % title[:40]) if title else "（空）")
    return _ok(n, "15–65 字符", ev, "P2") if ok else _fail(n, "15–65 字符", ev, "P2")


def check_obs_meta_desc_length(parser):
    """meta description 长度（CORE-04）：建议约 50–160 字符。"""
    desc = ""
    for m in parser.metas or []:
        name = (m.get("name") or m.get("property") or "").lower()
        if name in ("description", "og:description"):
            desc = (m.get("content") or "").strip()
            if name == "description":
                break
    n = len(desc)
    ok = 50 <= n <= 160
    ev = "description 长度=%d" % n
    if desc:
        ev += "「%s」" % desc[:50]
    else:
        ev += "（缺失）"
    return _ok(n, "50–160 字符", ev, "P2") if ok else _fail(n, "50–160 字符", ev, "P2")


def check_obs_favicon(parser):
    """favicon（CORE-11）。"""
    hits = []
    for lk in parser.links or []:
        rel = (lk.get("rel") or "").lower()
        href = lk.get("href") or ""
        if "icon" in rel and href:
            hits.append(rel)
    ok = bool(hits)
    ev = ("rel=%s" % ",".join(hits[:4])) if ok else "未见 rel=icon / shortcut icon"
    return _ok(True, "有 favicon", ev, "P3") if ok else _fail(False, "有 favicon", ev, "P3")


def check_obs_heading_order(parser):
    """标题层级是否乱跳（CONT-05）：不允许从 h1 直接跳到 h3+。"""
    levels = [lv for lv, _tx in (parser.headings or [])]
    if not levels:
        return _fail([], "标题层级连续", "未见 h1–h6", "P2")
    jumps = []
    prev = levels[0]
    for lv in levels[1:]:
        if lv > prev + 1:
            jumps.append("%s→%s" % (prev, lv))
        prev = lv
    ok = not jumps
    ev = "顺序=%s；跳级=%s" % (
        "→".join(str(x) for x in levels[:12]) + ("…" if len(levels) > 12 else ""),
        ",".join(jumps) or "无",
    )
    return _ok(True, "层级不跳级", ev, "P2") if ok else _fail(False, "层级不跳级", ev, "P2")


def check_obs_question_headings(parser):
    """问句型标题（CIT-05）：至少一条 h2–h6 含问号或疑问词。"""
    q_words = ("吗", "么", "何", "怎", "哪", "谁", "几", "是否", "what", "why", "how", "when", "where", "who", "which")
    hits = []
    for lv, tx in (parser.headings or []):
        if lv < 2:
            continue
        low = tx.lower()
        if "?" in tx or "？" in tx or any(w in low for w in q_words):
            hits.append("h%d:%s" % (lv, tx[:24]))
    ok = bool(hits)
    ev = ("命中=%s" % "；".join(hits[:4])) if ok else "h2–h6 未见问句型标题"
    return _ok(True, "有问句标题", ev, "P2") if ok else _fail(False, "有问句标题", ev, "P2")


def check_obs_lazy_loading(parser):
    """懒加载是否过早（IMG-05）：首部图片不宜 loading=lazy。"""
    imgs = list(parser.imgs or [])
    if not imgs:
        return _ok(True, "懒加载位置合理", "无 <img>", "P3")
    early = imgs[:3]
    lazy_early = [im for im in early if (im.get("loading") or "").lower() == "lazy"]
    ok = not lazy_early
    ev = "前 %d 张中 loading=lazy 共 %d" % (len(early), len(lazy_early))
    return _ok(True, "首屏勿过早 lazy", ev, "P3") if ok else _fail(False, "首屏勿过早 lazy", ev, "P3")


def check_obs_script_budget(parser):
    """外链脚本数 / 内联体积（PERF-14/15 合并观察）。"""
    scripts = [s for s in (parser.scripts or []) if not s.get("jsonld")]
    external = [s for s in scripts if s.get("src")]
    inline_lens = [int(s.get("inline_len") or 0) for s in scripts if not s.get("src")]
    inline_total = sum(inline_lens)
    n_ext = len(external)
    ok = n_ext <= 25 and inline_total <= 80000
    ev = "外链 script=%d；内联合计=%d 字节" % (n_ext, inline_total)
    return _ok(True, "脚本预算内", ev, "P3") if ok else _fail(False, "脚本预算内", ev, "P3")


def check_obs_llms_quality(ai_endpoints):
    """llms.txt 质量（ACC-20）：存在且含标题或链接。"""
    found = ai_endpoints.get("found") or []
    if "llms.txt" not in found:
        return _fail(False, "llms.txt 可用且有结构", "未检出 llms.txt", "P3")
    # 端点检查只记有无；质量用 evidence 字符串兜底提示
    ev = ai_endpoints.get("evidence") or "llms.txt 已存在"
    # 若仅存在，给通过但提示可加章节
    return _ok(True, "llms.txt 可用", ev + "（建议含 # 标题与页面链接）", "P3")


def check_observe_extras(html, parser, ai_endpoints=None):
    """观察加厚（不计分）。"""
    out = {
        "faq_visible": check_obs_faq_visible(html, parser),
        "author_visible": check_obs_author_visible(html),
        "date_granular": check_obs_date_granular(html, parser),
        "img_modern": check_obs_img_modern(parser),
        "dom_size": check_obs_dom_size(parser),
        "title_length": check_obs_title_length(parser),
        "meta_desc_length": check_obs_meta_desc_length(parser),
        "favicon": check_obs_favicon(parser),
        "heading_order": check_obs_heading_order(parser),
        "question_headings": check_obs_question_headings(parser),
        "lazy_loading": check_obs_lazy_loading(parser),
        "script_budget": check_obs_script_budget(parser),
    }
    if ai_endpoints is not None:
        out["llms_quality"] = check_obs_llms_quality(ai_endpoints)
    return out


# ---- geo.new 第三方审计（★ v9 新增：数据获取诊断节点）----------------------

GEO_NEW_API = "https://geo.new/api/v1/audits"
# 复用同一会话：同站重复提交会"附加到进行中的运行/复用 1 小时内完成报告"，
# 而不是被当作陌生会话限流（TARGET_COOLDOWN）。★ v9
_GEO_NEW_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".geo_new_session")


def _load_geo_new_session():
    try:
        with open(_GEO_NEW_SESSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _save_geo_new_session(session):
    if not session:
        return
    try:
        fd = os.open(_GEO_NEW_SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(session)
    except Exception:
        pass


def check_geo_new(url, wait=90, fresh=False):
    """【数据获取诊断节点】调用 geo.new 公开 API 完成一次单页审计。

    流程（对齐官方 /docs/api）：
      POST /api/v1/audits {"url":...} → 202 {audit_id, session, snapshot_url, poll_after_seconds}
      GET  snapshot_url (Authorization: Bearer <session>) → 200，读 status 直到 complete/partial/failed
    关键：
      · 首次无 token 请求**不发** Origin / Sec-Fetch-*（否则返回 ORIGIN_REJECTED）；
      · **必须带 User-Agent**（否则被 Cloudflare Error 1010 拦截）；
      · **持久化并复用 session**（`.geo_new_session`）——同站重复提交会复用/附加，
        避免被当作陌生会话触发 TARGET_COOLDOWN（每 URL 5 分钟一次）。
    免 key 匿名会话每小时 5 次审计；读报告免费。
    网络：自动遵循标准代理环境变量（HTTPS_PROXY/https_proxy/ALL_PROXY）；
    实测直连 geo.new 即通，默认无需代理，受限网络设 HTTPS_PROXY 即可。

    返回 dict（失败不抛异常，主流程照常产出确定性报告）：
      {enabled, ok, error, audit_id, report_url, status, score, grade, coverage,
       categories[], platforms[], blockers[], open_count, top_actions[], connection{}, screenshot{}}
    """
    payload = {"url": url}
    if fresh:
        payload["fresh"] = True
    session = _load_geo_new_session()
    headers = {}
    if session:
        headers["Authorization"] = "Bearer %s" % session
    st_code, env, _ = _json_request(GEO_NEW_API, method="POST", payload=payload,
                                    headers=headers, timeout=30)
    if not isinstance(env, dict) or not env.get("audit_id"):
        err = ""
        if isinstance(env, dict):
            e = env.get("error") or {}
            err = (e.get("code") if isinstance(e, dict) else "") or env.get("code") or ""
        hint = "（每 URL 5 分钟内限一次；稍后重试或复用已有报告）" if st_code == 429 else ""
        return {"enabled": True, "ok": False, "audit_id": "", "report_url": "",
                "error": "提交失败（HTTP %s %s）%s" % (st_code, err, hint), "categories": [],
                "platforms": [], "blockers": [], "top_actions": [], "open_count": 0}

    audit_id = env["audit_id"]
    session = env.get("session", "") or session
    _save_geo_new_session(session)
    snap_url = env.get("snapshot_url") or ("%s/%s" % (GEO_NEW_API, audit_id))
    report_url = env.get("report_url", "")
    poll = env.get("poll_after_seconds", 2)
    auth = {"Authorization": "Bearer %s" % session}

    snap, terminal = None, ("complete", "partial", "failed")
    deadline = time.time() + max(0, wait)
    while True:
        _, data, _ = _json_request(snap_url, headers=auth, timeout=30)
        if isinstance(data, dict):
            snap = data
            if data.get("status") in terminal:
                break
        if time.time() >= deadline:
            break
        time.sleep(max(2, min(poll, 5)))

    if snap is None:
        return {"enabled": True, "ok": False, "audit_id": audit_id, "report_url": report_url,
                "error": "读取快照失败", "categories": [], "platforms": [], "blockers": [],
                "top_actions": [], "open_count": 0}

    status_val = snap.get("status")
    cat = (snap.get("scores") or {}).get("catalog") or {}
    findings = snap.get("findings") or []
    open_findings = [f for f in findings
                     if str(f.get("status", "")).lower() in ("warn", "warning", "fail", "failed")]
    open_findings.sort(key=lambda f: (f.get("impact_rank") or 99))
    top = [{"id": f.get("id") or f.get("rule_id", ""), "title": f.get("title", ""),
            "severity": f.get("severity", ""), "effort": f.get("effort", ""),
            "fix": f.get("recommended_fix", ""), "evidence": f.get("explanation", "")}
           for f in open_findings[:8]]

    err = ""
    if cat.get("value") is None:
        ec = (snap.get("error") or {}).get("code") if isinstance(snap.get("error"), dict) else snap.get("error")
        err = "审计状态=%s%s" % (status_val, ("（%s）" % ec) if ec else "")
    return {
        "enabled": True,
        "ok": cat.get("value") is not None,
        "error": err,
        "audit_id": audit_id,
        "report_url": report_url,
        "status": status_val,
        "score": cat.get("value"),
        "grade": cat.get("grade", ""),
        "coverage": cat.get("coverage"),
        "categories": [{"id": c.get("id"), "title": c.get("title"), "weight": c.get("weight"),
                        "value": c.get("value")} for c in (cat.get("categories") or [])],
        "platforms": [{"id": p.get("id"), "title": p.get("title"), "value": p.get("value"),
                       "status": p.get("status")} for p in (cat.get("platforms") or [])],
        "blockers": cat.get("blockers") or [],
        "open_count": len(open_findings),
        "top_actions": top,
        "connection": snap.get("connection") or {},
        "screenshot": snap.get("screenshot") or {},
    }


# ---------------------------------------------------------------------------
# 评分（§一.4，0–100）与分级（§〇.2）
# ---------------------------------------------------------------------------

# 内容加分池：每项 1 分，整池最多 5。站外指标不进池。
BONUS_POOL_KEYS = (
    "title_length", "meta_desc_length", "heading_order",
    "question_headings", "favicon", "lazy_loading", "script_budget",
)
BONUS_POOL_CAP = 5


def compute_score(checks):
    """五轴基础分（v11）。

    引用可达 28、结构化 18、内容可引用 24（含加分池最多 5）、
    页内实体 18、技术与渲染 12。合计 100。
    unknown 不进分子也不进分母。微调与 P0 封顶在 finalize_score()。
    """
    items = []   # (label, max, earned, known)

    def add(label, mx, earned, known=True):
        earned = max(0, min(int(earned), mx))
        items.append((label, mx, earned, bool(known)))

    def kof(r):
        return bool((r or {}).get("known", True))

    # 引用可达 28
    cita = checks["robots"]["citation_allowed"]
    cita_ev = cita.get("evidence", "")
    if not kof(cita):
        cita_pts = 0
    elif cita["status"]:
        cita_pts = 12
    elif "被 Disallow" in cita_ev or "被阻断" in cita_ev:
        cita_pts = 0
    else:
        cita_pts = 6
    add("引用型爬虫显式 Allow", 12, cita_pts, kof(cita))

    ai = checks["ai_crawl"]
    add("AI 爬虫实测可达", 10, 10 if ai["status"] else 0, kof(ai))

    add("sitemap 有效且声明", 6,
        (6 if (checks["sitemap"]["exists"]["status"] and checks["robots"]["sitemap"]["status"]) else 0),
        kof(checks["sitemap"]["exists"]) and kof(checks["robots"]["sitemap"]))

    # 结构化 18（实体另轴）
    jl = checks["jsonld"]
    add("有 JSON-LD 且合法", 8, 8 if jl["status"] else 0, kof(jl))
    key_types = {"Organization", "WebSite", "Article", "Product", "BreadcrumbList", "BlogPosting"}
    hit = len(key_types & set(jl.get("types", [])))
    add("关键类型齐全", 5, int(round(5 * min(hit, 4) / 4)), kof(jl))
    all_types = " ".join(jl.get("types", []))
    add("问答 FAQPage/QAPage", 5, 5 if ("FAQPage" in all_types or "QAPage" in all_types) else 0, kof(jl))

    # 内容可引用 19 + 加分池最多 5 = 24
    c = checks["content"]
    add("答案前置/胶囊", 8, 8 if c["answer_first"]["status"] else 0, kof(c["answer_first"]))
    add("表格/外链", 6, 6 if c["table_links"]["status"] else 0, kof(c["table_links"]))
    add("time + 价格日期", 5, 5 if c["time_price"]["status"] else 0, kof(c["time_price"]))
    obs = checks.get("obs_extra") or {}
    bonus_hits, bonus_known = [], 0
    for key in BONUS_POOL_KEYS:
        item = obs.get(key) or {}
        if not item or not kof(item):
            continue
        bonus_known += 1
        if item.get("status"):
            bonus_hits.append(key)
    if bonus_known:
        pool_max = min(BONUS_POOL_CAP, bonus_known)
        add("内容加分池", pool_max, min(pool_max, len(bonus_hits)), True)
    else:
        pool_max = 0

    # 页内实体 18
    ent = checks.get("entity") or {}
    if ent:
        add("实体 Organization 声明", 4, 4 if ent["org"]["status"] else 0, kof(ent["org"]))
        add("实体 sameAs 权威锚点", 5, 5 if ent["sameas"]["status"] else 0, kof(ent["sameas"]))
        idok, profok = ent["id"]["status"], ent["profile"]["status"]
        add("实体 @id/profile", 3,
            3 if (idok and profok) else (1 if (idok or profok) else 0),
            kof(ent["id"]) and kof(ent["profile"]))
    author = obs.get("author_visible") or {}
    faq_vis = obs.get("faq_visible") or {}
    if author:
        add("可见作者署名", 3, 3 if author.get("status") else 0, kof(author))
    if faq_vis:
        add("可见 FAQ/问答", 3, 3 if faq_vis.get("status") else 0, kof(faq_vis))

    # 技术与渲染 12
    add("无嵌套文档", 3, 3 if checks["nested"]["status"] else 0, kof(checks["nested"]))
    add("语言声明", 2, 2 if checks["lang"]["status"] else 0, kof(checks["lang"]))
    add("canonical", 2, 2 if checks["canonical"]["status"] else 0, kof(checks["canonical"]))
    add("Open Graph / Twitter", 2, 2 if checks["og"]["status"] else 0, kof(checks["og"]))
    add("压缩", 2, 2 if checks["compression"]["status"] else 0, kof(checks["compression"]))
    add("HTTP/2", 1, 1 if checks["httpver"]["status"] else 0, kof(checks["httpver"]))

    known_max = sum(mx for _, mx, _, k in items if k)
    total_max = sum(mx for _, mx, _, _ in items)
    earned = sum(e for _, _, e, k in items if k)
    score = int(round(100.0 * earned / known_max)) if known_max else 0
    coverage = (known_max / 100.0) if known_max else 0.0
    # 加分池权重随已知项收缩，total_max 可能小于 100；覆盖率仍按满轴 100 计已知权重。
    if known_max > 100:
        known_max = 100
    coverage = (known_max / 100.0)
    breakdown = {label: e for label, mx, e, k in items}
    unknown = [label for label, mx, e, k in items if not k]
    meta = {"known_max": known_max, "total_max": 100,
            "coverage": round(coverage, 3), "unknown": unknown,
            "bonus": {"earned": min(pool_max, len(bonus_hits)) if bonus_known else 0,
                      "max": pool_max, "hits": bonus_hits}}
    return score, breakdown, meta


def finalize_score(base_score, problems, checks):
    """★ v10：在基础分上施加「微调项（±5）」与「P0 封顶」（§一.4 / §一.6）。

    微调项：
      · AI 发现端点（§三 S / §17.2）：已有 llms.txt / ai.* → 最多 +5；
      · GEO 注入自查（§13.6）：命中 → −5。
      （数值各自封顶，合计限制在 −5 ~ +5。）
    P0 封顶（§一.6 铁律 3）：存在**未修复的 P0** 时，总分强制 ≤74（“及格”档），
      不让小项把致命问题平均掉。
    返回 (final_score, info)。
    """
    micro, details = 0, []
    ep = checks.get("ai_endpoints") or {}
    if ep.get("points"):
        micro += ep["points"]
        details.append("AI 端点 +%d" % ep["points"])
    inj = checks.get("injection") or {}
    if inj.get("points"):
        micro += inj["points"]
        details.append("注入风险 %d" % inj["points"])
    micro = max(-5, min(5, micro))
    score = max(0, min(100, base_score + micro))
    p0 = [p for p in problems if p["level"] == "P0"]
    capped = bool(p0) and score > 74
    if capped:
        score = 74
    info = {"micro": micro, "micro_details": details,
            "capped_by_p0": capped, "p0": [p["desc"] for p in p0]}
    return score, info


def score_band(score):
    """分档（★ v8：对齐 geo.new 的 Poor/Fair/Good/Excellent 区间）。

    geo.new 把 77 判为 Good，故本库同步把"良好"下移到 75，
    避免同一分数在两个工具里给出矛盾的档位标签。
    """
    if score >= 90:
        return "优秀"
    if score >= 75:
        return "良好"
    if score >= 50:
        return "及格"
    return "待整改"


def collect_problems(checks):
    """汇总所有「已判定但未通过」项为问题列表（含等级），按 P0→P3 排序。

    ★ v10：**unknown（无法判定）不算问题**，其项改由 meta.unknown 单列（§一.6）。
    """
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    problems = []

    def failed(r):
        return bool(r) and (not r["status"]) and r.get("known", True)

    mapping = [
        ("jsonld", "全站/页面无 JSON-LD 或非法"),
        ("nested", "HTML 文档嵌套（多个 <html>/<head>/DOCTYPE）"),
        ("lang", "主文档缺 <html lang>"),
        ("semantic", "无语义化标签（main/article/footer/time）"),
        ("h1", "缺主标题 <h1> / 主主题不明（geo.new S07/G06）"),
        ("meta_in_body", "有 <meta> 出现在 <body> 内（geo.new HTML-06）"),
        ("sec_headers", "缺安全响应头（CSP/nosniff/Referrer-Policy 等，geo.new SEC-03）"),
        ("canonical", "缺 canonical"),
        ("og", "缺 Open Graph / Twitter Card"),
        ("compression", "无 gzip/br 压缩"),
        ("httpver", "仅 HTTP/1.1（未启用 HTTP/2）"),
    ]
    for key, desc in mapping:
        r = checks.get(key)
        if failed(r):
            problems.append({"level": r["level"], "key": key, "desc": desc, "evidence": r["evidence"]})
    # 子检查
    if failed(checks["robots"]["citation_allowed"]):
        ca = checks["robots"]["citation_allowed"]
        problems.append({"level": ca.get("level", "P0"), "key": "robots.citation",
                         "desc": "robots 未放行引用型爬虫（被阻断=P0 / 仅未显式列举=P1）",
                         "evidence": ca["evidence"]})
    if failed(checks["robots"]["sitemap"]):
        problems.append({"level": "P1", "key": "robots.sitemap", "desc": "robots 未声明 Sitemap",
                         "evidence": checks["robots"]["sitemap"]["evidence"]})
    if failed(checks["sitemap"]["exists"]):
        problems.append({"level": "P0", "key": "sitemap.exists", "desc": "sitemap 不可用",
                         "evidence": checks["sitemap"]["exists"]["evidence"]})
    if failed(checks["sitemap"]["fresh"]):
        problems.append({"level": "P2", "key": "sitemap.fresh", "desc": "sitemap 时效陈旧",
                         "evidence": checks["sitemap"]["fresh"]["evidence"]})
    if failed(checks["ai_crawl"]):
        problems.append({"level": "P0", "key": "ai_crawl", "desc": "部分 AI 爬虫实测不可达",
                         "evidence": checks["ai_crawl"]["evidence"]})
    for k, desc in (("answer_first", "内容缺答案前置/FAQ"),
                    ("table_links", "缺数据表格/权威外链"),
                    ("time_price", "缺 <time> 或价格日期标注")):
        r = checks["content"].get(k)
        if failed(r):
            problems.append({"level": r["level"], "key": "content." + k, "desc": desc, "evidence": r["evidence"]})
    # 实体信号（★ v8 新增）
    ent = checks.get("entity", {})
    for k, desc in (("org", "无 Organization 实体声明"),
                    ("sameas", "Organization 缺 sameAs 权威锚点"),
                    ("id", "Organization 缺 @id 稳定标识"),
                    ("profile", "Organization 缺 name/url")):
        r = ent.get(k)
        if failed(r):
            problems.append({"level": r["level"], "key": "entity." + k, "desc": desc, "evidence": r["evidence"]})
    # GEO 注入 / 隐藏文字自查（★ v10，§13.6）
    inj = checks.get("injection") or {}
    if inj.get("found"):
        problems.append({"level": "P2", "key": "injection",
                         "desc": "疑似 GEO 注入 / 隐藏文字信号（§13.6 自查）",
                         "evidence": inj.get("evidence", "")})
    problems.sort(key=lambda x: order.get(x["level"], 9))
    return problems


def collect_unknown(meta, checks):
    """★ v10：汇总「无法判定（unknown）」的项，供报告与 JSON 展示（§一.6）。"""
    out = list(meta.get("unknown", []))
    for grp, key in (("robots", "citation_allowed"), ("robots", "sitemap"),
                     ("sitemap", "exists"), ("sitemap", "fresh")):
        r = (checks.get(grp) or {}).get(key)
        if r and not r.get("known", True):
            out.append("%s.%s" % (grp, key))
    # ★ v11：顶层检测项若 unknown（如首页抓取失败）也一并单列
    for key in ("h1", "meta_in_body", "sec_headers", "lang", "semantic",
                "canonical", "og", "jsonld", "nested"):
        r = checks.get(key)
        if isinstance(r, dict) and not r.get("known", True):
            out.append(key)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# 报告输出（Markdown，对齐 §十二 模板）
# ---------------------------------------------------------------------------

def render_markdown(profile, checks, score, breakdown, problems, url_checked, meta=None, finfo=None):
    L = []
    meta = meta or {}
    finfo = finfo or {}
    band = score_band(score)
    L.append("# GEO 体检报告 · %s" % profile.get("name", ""))
    L.append("")
    L.append("- 站点：`%s`　CMS：%s" % (profile.get("url", ""), profile.get("cms", "未知")))
    L.append("- 检测页面：`%s`" % url_checked)
    L.append("- 体检时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("- **得分：%d/100（%s）**" % (score, band))
    # ★ v10：置信度门控 / P0 封顶 / 微调（§一.6）
    cov = meta.get("coverage")
    if cov is not None:
        conf = "%.0f%%" % (cov * 100)
        note = "（正常）" if cov >= 0.7 else "（⚠️ 偏低：多项无法判定 → 结论置信度低）"
        L.append("- **结论置信度：%s**%s" % (conf, note))
    if finfo.get("capped_by_p0"):
        L.append("- ⚠️ **总分已按 P0 封顶（§一.6）**：存在未修复 P0 → 上限 74（及格档）")
        for d in finfo.get("p0", []):
            L.append("  - P0 未修复：%s" % d)
    if finfo.get("micro"):
        L.append("- 微调项：%+d 分（%s）" % (finfo["micro"], "；".join(finfo.get("micro_details", []))))
    L.append("")
    L.append("> 判级标准见文档 §〇.2；评分口径见 §一.4；**置信度门控见 §一.6**；检测命令见 §五。")
    L.append("")
    L.append("## A. 已做对")
    L.append("")
    good = []
    for key, label in _check_labels(checks):
        if key.startswith("__"):
            good.append("- ✅ %s" % label)   # 虚拟标签：仅当为真时才会被产出
        elif checks.get(key, {}).get("status"):
            good.append("- ✅ %s" % label)
    L.extend(good or ["- （无）"])
    L.append("")
    # 未判定项（unknown）——既不计 0 也不计满分，单列（§一.6）
    unknown = meta.get("unknown", [])
    if unknown:
        L.append("> ⚪ **无法判定（unknown，不计 0 也不计满分）**：%s" % "、".join(unknown))
        L.append("")
    L.append("## B. 问题清单（按 P0→P3 排序）")
    L.append("")
    if problems:
        L.append("| 等级 | 问题 | 证据 |")
        L.append("|:---:|------|------|")
        icon = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🟡 P2", "P3": "⚪ P3"}
        for p in problems:
            L.append("| %s | %s | %s |" % (icon.get(p["level"], p["level"]), p["desc"],
                                           p["evidence"].replace("|", "\\|")))
    else:
        L.append("- 未发现 P0–P3 问题。")
    L.append("")

    L.append("## C. 得分明细")
    L.append("")
    L.append("| 项 | 得分 |")
    L.append("|----|:---:|")
    for k, v in breakdown.items():
        L.append("| %s | %d |" % (k, v))
    base = meta.get("base_score", score)
    L.append("| **基础分合计（已判定项归一）** | **%d** |" % base)
    if finfo.get("micro"):
        L.append("| 微调项（端点/注入，§一.4） | %+d |" % finfo["micro"])
    final_label = "**最终（P0 封顶）**" if finfo.get("capped_by_p0") else "**最终**"
    L.append("| %s | **%d** |" % (final_label, score))
    L.append("")
    # D. 分平台就绪度（★ v8 新增，对齐 geo.new 的 Platform Readiness）
    plat = checks.get("platform")
    if plat:
        L.append("## D. 分平台就绪度（AI 可读性实测）")
        L.append("")
        L.append("| AI 平台 | 就绪 | 代表 UA（实测状态） |")
        L.append("|------|:---:|------|")
        for name, info in plat.items():
            mark = "✅ 可读" if info["ok"] else "⛔ 不可达"
            ua_txt = "，".join("%s=%s" % (u, s if s is not None else "连接错误")
                              for u, s in zip(info["uas"], info["status"]))
            L.append("| %s | %s | %s |" % (name, mark, ua_txt))
        L.append("")
        L.append("> 说明：只要该平台任一代表 UA 实测返回 200，即视为该平台可读；"
                 "本表仅作展示，不重复计入 0–100 总分。")
        L.append("")

    # E. 实体信号（★ v8 新增，对齐 geo.new 的 Entity Signals）
    ent = checks.get("entity") or {}
    if ent:
        L.append("## E. 实体信号（★ v8 新增，对齐 geo.new Entity Signals）")
        L.append("")
        L.append("| 项 | 状态 | 证据 |")
        L.append("|----|:---:|------|")
        for key, label in (("org", "Organization 声明"), ("sameas", "sameAs 权威锚点"),
                           ("id", "@id 稳定标识"), ("profile", "name/url 关联")):
            r = ent.get(key, {})
            L.append("| %s | %s | %s |" % (label, "✅" if r.get("status") else "❌",
                                           str(r.get("evidence", "")).replace("|", "\\|")))
        L.append("")

    # G. AI 发现端点 / Agent-ready / 注入自查（★ v10 新增，§三 S / §17 / §13.6）
    ep = checks.get("ai_endpoints") or {}
    ag = checks.get("agent_ready") or {}
    inj = checks.get("injection") or {}
    if ep or ag or inj:
        L.append("## G. AI 发现端点 · Agent-ready · 注入自查（★ v10 新增）")
        L.append("")
        if ep:
            L.append("**① AI 发现端点（§三 S / §17.2）**：已有 %s ｜ 缺失 %s"
                     % ("、".join(ep.get("found") or []) or "无",
                        "、".join(ep.get("missing") or []) or "无"))
            L.append("")
            L.append("> `llms.txt` 等对 **Google 无益无害**（§16.2），但对 **agent / 其它系统** 有用；"
                     "成本极低，属「低优先可做」。合计最多 **+5 分微调**。")
            L.append("")
        if ag:
            mark = "✅" if ag.get("ok") else "⚠️"
            L.append("**② Agent-ready 轻检（§17.1）**：%s %s" % (mark, ag.get("evidence", "")))
            L.append("")
        if inj:
            mark = "❌ 命中" if inj.get("found") else "✅ 未发现"
            L.append("**③ GEO 注入 / 隐藏文字自查（§13.6）**：%s —— %s" % (mark, inj.get("evidence", "")))
            L.append("")
            if inj.get("found"):
                L.append("> ⚠️ 命中项会造成 AI 对整站的不信任，且可能来自广告位 / 第三方插件注入，"
                         "建议排查（−5 微调）。")
                L.append("")
    return "\n".join(L)


def render_geo_new(gn, our_score=None):
    """渲染 geo.new 第三方审计节点（★ v9 新增）。gn 为 check_geo_new 的返回。"""
    L = []
    L.append("## F. geo.new 第三方审计节点（★ v9 新增）")
    L.append("")
    if not gn or not gn.get("enabled"):
        L.append("- （未启用；加 `--geo-new` 开启）")
        L.append("")
        return "\n".join(L)
    if not gn.get("ok"):
        L.append("- ⚠️ 未取得有效分数：%s" % (gn.get("error") or "未知"))
        if gn.get("audit_id"):
            L.append("- audit_id：`%s`" % gn["audit_id"])
        if gn.get("report_url"):
            L.append("- 报告链接：%s" % gn["report_url"])
        L.append("")
        return "\n".join(L)

    L.append("> 数据来源：**`geo.new` 公开 API**（`POST /api/v1/audits` → 轮询快照，规则集 176 项）。"
             "★ 作为本报告的**独立交叉验证**，非本程序自身计算。")
    L.append("")
    L.append("- geo.new 总分：**%s/100（%s）**，覆盖率 %s" %
             (gn.get("score"), gn.get("grade"),
              ("%.0f%%" % (gn["coverage"] * 100)) if isinstance(gn.get("coverage"), float) else "?"))
    if our_score is not None:
        diff = gn["score"] - our_score
        L.append("- 与本程序对照：本程序 **%d** ｜ geo.new **%s** ｜ 差 **%+d**" %
                 (our_score, gn["score"], diff))
    conn = gn.get("connection") or {}
    if conn.get("ttfb_ms") is not None:
        L.append("- 抓取实测：HTTP %s ｜ TTFB %sms ｜ %s bytes ｜ 截图 %s×%s" %
                 (conn.get("status"), conn.get("ttfb_ms"), conn.get("html_bytes"),
                  (gn.get("screenshot") or {}).get("width", "?"),
                  (gn.get("screenshot") or {}).get("height", "?")))
    if gn.get("audit_id"):
        L.append("- audit_id：`%s` ｜ 报告：%s" % (gn["audit_id"], gn.get("report_url", "")))
    L.append("")
    if gn.get("categories"):
        L.append("| geo.new 类别 | 权重 | 分 |")
        L.append("|------|:---:|:---:|")
        for c in gn["categories"]:
            L.append("| %s | %s | %s |" % (c.get("title"), c.get("weight"), c.get("value")))
        L.append("")
    if gn.get("platforms"):
        L.append("| geo.new 平台就绪 | 分 | 评级 |")
        L.append("|------|:---:|:---:|")
        for p in gn["platforms"]:
            L.append("| %s | %s | %s |" % (p.get("title"), p.get("value"), p.get("status")))
        L.append("")
    L.append("- geo.new 未通过项：**%d** 项（按影响排序，列 Top-8）" % gn.get("open_count", 0))
    L.append("")
    if gn.get("top_actions"):
        L.append("| # | 规则 | 问题 | 建议 |")
        L.append("|:---:|:---:|------|------|")
        for i, a in enumerate(gn["top_actions"], 1):
            L.append("| %d | %s | %s | %s |" % (i, a.get("id", ""), a.get("title", ""),
                                                (a.get("fix") or "").replace("|", "\\|")))
        L.append("")
    return "\n".join(L)


def _check_labels(checks):
    labels = [
        ("jsonld", "有合法 JSON-LD"), ("nested", "无 HTML 嵌套"),
        ("lang", "<html lang> 存在"), ("semantic", "语义化标签齐"),
        ("h1", "有主标题 <h1>"), ("meta_in_body", "<meta> 均在 <head>"),
        ("sec_headers", "有安全响应头"),
        ("canonical", "有 canonical"), ("og", "有 OG/Twitter"),
        ("compression", "已开启压缩"), ("httpver", "HTTP/1.1+ 版本正常"),
    ]
    ent = checks.get("entity") or {}
    if ent.get("org", {}).get("status"):
        labels.append(("__entity_org__", "有 Organization 实体"))
        if ent.get("sameas", {}).get("status"):
            labels.append(("__entity_sameas__", "Organization 有 sameAs 权威锚点"))
    return labels


# ---------------------------------------------------------------------------
# 运行单个站点
# ---------------------------------------------------------------------------

def discover_content_url(base, home_html):
    """从首页找一个内容页链接（帖子/文章），找不到则回退首页。"""
    for pat in (r'href="([^"]*thread-\d+-1-1\.html)"', r'href="([^"]*/archiver/[^"]*)"',
                r'href="([^"]*/\d{4}/\d{2}/[^"]+\.html)"', r'href="([^"]*/\?p=\d+)"'):
        m = re.search(pat, home_html, re.I)
        if m:
            href = m.group(1)
            if href.startswith("http"):
                return href
            return base.rstrip("/") + "/" + href.lstrip("/")
    return base


def _origin(url):
    """取 URL 的 scheme://host[:port]，用于 robots / sitemap 这类【根级】资源。

    check_robots/check_sitemap 会把路径直接拼在入参后；若传入的是子路径页
    （如 https://www.hqbnb.com/thinkpad-sz），拼出的 .../thinkpad-sz/robots.txt
    会 404，误报「未放行/未声明 Sitemap」。故这两项必须用 origin。
    """
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) if m else (url or "").rstrip("/")


def run_site(profile):
    base = profile["url"]
    home = fetch(base)
    html = home["body"]
    parser = parse_html(html)
    content_url = discover_content_url(base, html)

    # 内容可引用性在"内容页"上评估更贴近本库（报价帖/文章页）；
    # 若内容页与首页不同则单独抓一次，失败则回退首页。
    content_html, content_parser = html, parser
    if content_url != base:
        cr = fetch(content_url)
        if cr["ok"] and cr["body"]:
            content_html = cr["body"]
            content_parser = parse_html(content_html)

    checks = {
        "compression": check_compression(base),
        "httpver": check_http_version(base),
        "robots": check_robots(_origin(base)),
        "sitemap": check_sitemap(_origin(base)),
        "ai_crawl": check_ai_crawler_reach(content_url),
    }
    # 首页/内容页可解析（非连接层失败）时，才产出 HTML 派生检查；否则一律标 unknown（§一.6）
    home_ok = not _is_conn_error(home)
    if home_ok:
        checks.update({
            "jsonld": check_jsonld(html, parser),
            "nested": check_nested_doc(html),
            "lang": check_lang(parser),
            "semantic": check_semantic_tags(parser),
            "h1": check_main_heading(parser),
            "meta_in_body": check_meta_in_body(parser),
            "sec_headers": check_security_headers(home),
            "canonical": check_canonical(parser),
            "og": check_og(parser),
            "content": check_content_citability(content_html, content_parser),
            "entity": check_entity_signals(parser),
            "injection": check_injection_risks(html, parser),
            "agent_ready": check_agent_ready(parser),
        })
    else:
        def _uu(lv="P1"):
            return _unknown("", "无法判定（首页抓取失败）", "首页抓取失败（unknown）", lv)
        checks.update({
            "jsonld": _uu("P0"), "nested": _uu("P0"), "lang": _uu("P0"),
            "semantic": _uu("P1"), "h1": _uu("P2"), "meta_in_body": _uu("P2"),
            "sec_headers": _uu("P2"),
            "canonical": _uu("P1"), "og": _uu("P1"),
            "content": {"answer_first": _uu("P1"), "table_links": _uu("P2"), "time_price": _uu("P1")},
            "entity": {"org": _uu("P1"), "sameas": _uu("P1"), "id": _uu("P1"), "profile": _uu("P1")},
            "injection": {"known": False, "found": False, "points": 0,
                          "evidence": "无法判定（首页抓取失败）"},
            "agent_ready": {"known": False, "ok": False, "img_total": 0, "img_missing_dims": 0,
                            "evidence": "无法判定（首页抓取失败）"},
        })
    checks["platform"] = check_platform_readiness(checks["ai_crawl"])
    checks["ai_endpoints"] = check_ai_endpoints(_origin(base))
    base_score, breakdown, meta = compute_score(checks)
    problems = collect_problems(checks)
    final_score, finfo = finalize_score(base_score, problems, checks)
    meta["unknown"] = collect_unknown(meta, checks)
    meta["base_score"] = base_score
    meta["final_score"] = final_score
    return {
        "profile": profile,
        "url_checked": content_url,
        "checks": checks,
        "score": final_score,           # 兼容旧字段：score = 最终分（含微调与 P0 封顶）
        "base_score": base_score,
        "breakdown": breakdown,
        "problems": problems,
        "meta": meta,
        "finfo": finfo,
    }


# ---------------------------------------------------------------------------
# 可选 AI 评审（按需复用 AI_fenxi.py，失败自动降级）
# ---------------------------------------------------------------------------

def ai_review(result):
    """把确定性检测结果交给 AI_fenxi 评审（可选）。失败不影响主流程。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from AI_fenxi import ai_fenxi  # 延迟导入：不用 --ai 时绝不加载
    except Exception as e:
        return "（AI 评审不可用：无法 import AI_fenxi —— %s）" % e
    summary = {
        "站点": result["profile"].get("url"),
        "得分": result["score"],
        "问题": [{"等级": p["level"], "问题": p["desc"], "证据": p["evidence"]} for p in result["problems"]],
    }
    prompt = (
        "下面是网站 GEO（让 AI 更容易抓取/引用）的自动体检结果（JSON）。"
        "请用简体中文给出：1) 哪些是真正致命的问题；2) 有无自动检测遗漏的隐患；"
        "3) 按优先级给出最多 5 条可执行建议。不要复述原始 JSON。\n\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
    )
    r = ai_fenxi(prompt, system_prompt="你是严谨的中文网站 GEO 优化专家。")
    if r.get("success"):
        return r.get("content", "")
    return "（AI 评审失败：%s）" % r.get("error", "未知错误")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="网站 GEO 自动体检工具（配套《网站geo爬虫友好化经验.md》）")
    ap.add_argument("--site", help="站点 URL，如 https://www.hqbsh.com/")
    ap.add_argument("--all", action="store_true", help="跑内置站点清单（§八）")
    ap.add_argument("--name", help="站点名（--site 时可选）")
    ap.add_argument("--cms", default="未知", help="CMS 类型（--site 时可选）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供程序消费）")
    ap.add_argument("--out", help="把 Markdown 报告写入文件")
    ap.add_argument("--ai", action="store_true", help="额外调用 AI_fenxi 做评审（可选，需 openai 库）")
    ap.add_argument("--geo-new", action="store_true", dest="geo_new",
                    help="调用 geo.new 公开 API 做第三方交叉验证（可选；免 key 每小时 5 次）")
    ap.add_argument("--geo-new-wait", type=int, default=90, dest="geo_new_wait",
                    help="等待 geo.new 完成的秒数（默认 90；超时则仅报 audit_id 供后续读取）")
    args = ap.parse_args()

    if args.all:
        profiles = SITE_PROFILES
    elif args.site:
        profiles = [{"name": args.name or args.site, "url": args.site, "cms": args.cms}]
    else:
        ap.error("请指定 --site URL 或 --all")

    all_results = []
    md_all = []
    for prof in profiles:
        sys.stderr.write(">> 体检 %s ...\n" % prof.get("url"))
        res = run_site(prof)
        if args.geo_new:
            sys.stderr.write(">> geo.new 第三方审计（最多 %ds）...\n" % args.geo_new_wait)
            res["geo_new"] = check_geo_new(res["url_checked"], wait=args.geo_new_wait)
        all_results.append(res)
        md = render_markdown(prof, res["checks"], res["score"], res["breakdown"],
                             res["problems"], res["url_checked"],
                             res.get("meta"), res.get("finfo"))
        if args.geo_new:
            md += "\n" + render_geo_new(res.get("geo_new"), our_score=res["score"]) + "\n"
        if args.ai:
            md += "\n## D. AI 评审（可选）\n\n" + ai_review(res) + "\n"
        md_all.append(md)

    out_md = ("\n\n---\n\n").join(md_all)

    if args.json:
        payload = []
        for r in all_results:
            meta = r.get("meta") or {}
            finfo = r.get("finfo") or {}
            payload.append({
                "site": r["profile"], "url_checked": r["url_checked"],
                "score": r["score"], "band": score_band(r["score"]),
                "base_score": r.get("base_score", r["score"]),
                # ★ v10 置信度门控（§一.6）：三态 / 覆盖率 / P0 封顶 / 微调
                "confidence": meta.get("coverage"),
                "unknown": meta.get("unknown", []),
                "capped_by_p0": finfo.get("capped_by_p0", False),
                "micro_adjust": finfo.get("micro", 0),
                "breakdown": r["breakdown"], "problems": r["problems"],
                "ai_endpoints": r["checks"].get("ai_endpoints"),
                "injection": r["checks"].get("injection"),
                "agent_ready": r["checks"].get("agent_ready"),
                "geo_new": r.get("geo_new"),
            })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(out_md)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_md)
        sys.stderr.write(">> 报告已写入 %s\n" % args.out)

    # 退出码：存在 P0 即返回 2（便于 cron/CI）
    has_p0 = any(any(p["level"] == "P0" for p in r["problems"]) for r in all_results)
    return 2 if has_p0 else 0


if __name__ == "__main__":
    sys.exit(main())
