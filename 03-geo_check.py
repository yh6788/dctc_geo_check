# coding=utf-8
# Compatible with Python 3.8+ (avoid X | Y type syntax).
"""
geo_check.py — GEO (generative-engine citation readiness) audit tool.

Scoring aligns with five-axis v11 used in production at https://www.geohttp.com
(confidence gating, P0 cap, AI discovery endpoints, agent-ready hints, injection self-check;
unknown ≠ 0).

Public docs:
  https://www.geohttp.com/method
  https://www.geohttp.com/en/method
  https://github.com/yh6788/dctc_geo_check

Design:
  1) Deterministic: Python standard library only (no requests/bs4/lxml).
  2) Read-only: HTTP GET only; never writes the target site.
  3) No LLM required; optional --ai loads a local helper if present.
  4) Default Markdown report suitable for humans and automation.
  5) Evidence-first: pass / fail / unknown; unknown lowers confidence only;
     any open P0 caps the final score at 74.

Usage:
  python3 03-geo_check.py --site https://www.example.com/
  python3 03-geo_check.py --all
  python3 03-geo_check.py --site URL --json
  python3 03-geo_check.py --site URL --out report.md
  python3 03-geo_check.py --site URL --ai

Exit codes: 0 ok; 2 one or more P0 issues (for cron/CI).
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
# Constants: AI crawler lists (tiers aligned with common BOT_TIERS practice)
# ---------------------------------------------------------------------------

# Citation core set — must be explicitly Allowed in robots for citation readiness
CITATION_BOTS = ["OAI-SearchBot", "Claude-SearchBot", "PerplexityBot", "Googlebot", "Applebot"]
# Other retrieval / on-demand bots
SEARCH_BOTS = ["ChatGPT-User", "Claude-User", "Perplexity-User", "xAI-Bot",
               "Amazonbot", "DuckAssistBot", "YouBot", "PetalBot"]
# Training bots (allow/block is a site policy choice)
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "Applebot-Extended", "CCBot", "Bytespider",
                 "meta-externalagent", "Meta-ExternalFetcher", "AI2Bot", "cohere-ai"]
# Classic search engines
CLASSIC_BOTS = ["Baiduspider", "bingbot", "Sogou", "360Spider", "YisouSpider"]

# Representative UAs for live reachability probes
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
    # Display-only platform rows; UA strings follow public robots conventions.
    # Non-200 only marks that platform unreachable; does not fail the scored crawl item as P0.
    "DoubaoBot": "Mozilla/5.0 (compatible; DoubaoBot/1.0; +https://www.doubao.com/)",
    "DeepSeekBot": "Mozilla/5.0 (compatible; DeepSeekBot/1.0; +https://www.deepseek.com/)",
    "MiniMaxBot": "Mozilla/5.0 (compatible; MiniMaxBot/1.0; +https://www.minimax.io/)",
}

DISPLAY_ONLY_BOTS = {"DoubaoBot", "DeepSeekBot", "MiniMaxBot"}

# Platform → representative UA map (readiness matrix for report display)
# A platform is "readable" if any listed UA returns live success.
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

# Authority-link host suffixes for the citable-content heuristic
AUTHORITATIVE_DOMAINS = (
    ".gov", ".edu", "wikipedia.org", "wikidata.org", "lenovo.com", "dell.com",
    "apple.com", "hp.com", "asus.com", "intel.com", "amd.com", "nvidia.com",
    "github.com", "arxiv.org", "stackoverflow.com",
)

# Default --all demos (public). Put private lists in site_profiles.local.json (do not publish).
SITE_PROFILES = [
    {"name": "example.com", "url": "https://www.example.com/", "cms": "example"},
    {"name": "geohttp.com", "url": "https://www.geohttp.com/", "cms": "GEO Cool"},
]


def load_site_profiles():
    """Prefer GEO_CHECK_SITES or site_profiles.local.json; else built-in demos."""
    candidates = []
    env_path = (os.environ.get("GEO_CHECK_SITES") or "").strip()
    if env_path:
        candidates.append(env_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "site_profiles.local.json"))
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception as exc:
            sys.stderr.write("Failed to load site list %s: %s\n" % (path, exc))
    return SITE_PROFILES


TIMEOUT = 20


# ---------------------------------------------------------------------------
# HTTP fetch (stdlib; returns status/headers/body/HTTP version)
# ---------------------------------------------------------------------------

def fetch(url, ua=DEFAULT_UA, accept_encoding=None, timeout=TIMEOUT):
    """GET and return {ok,status,headers,body,version,error,url,requested_url,redirects}.

    Follows 3xx. url is the final URL; redirects is [{status,from,to}, …].
    Never raises.
    """
    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua)
    if accept_encoding:
        req.add_header("Accept-Encoding", accept_encoding)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # audits tolerate self-signed / chain issues
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
        # Still try to read body (404 on robots/sitemap can be meaningful)
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
    except Exception as e:  # timeout / connect / SSL / parse
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
    """Decompress by Content-Encoding and decode charset (text analysis only)."""
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
        # brotli not in stdlib: leave raw (header check still works)
    except Exception:
        data = raw
    for cs in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return data.decode(cs)
        except Exception:
            continue
    return ""


def head(url, timeout=TIMEOUT):
    """Fetch with Accept-Encoding for compression/version checks."""
    return fetch(url, accept_encoding="gzip, br", timeout=timeout)


def _json_request(url, method="GET", payload=None, headers=None, timeout=TIMEOUT):
    """JSON request via stdlib; return (status, data_dict, resp_headers). data is None on failure.

    Do not send Origin / Sec-Fetch-* (geo.new rejects browser-like first requests).
    User-Agent is required (Cloudflare Error 1010 otherwise).
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
# HTML parse: semantic tags / meta / JSON-LD scripts / link / html attrs
# ---------------------------------------------------------------------------

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag_counts = {}
        self.html_attrs = {}       # attrs of first <html> (main document)
        self._first_html_seen = False
        self.metas = []          # each <meta> attrs
        self.links = []          # each <link> attrs
        self.jsonld = []         # raw JSON-LD strings
        self.times = []          # <time datetime=...>
        self.imgs = []           # each <img> attrs (CLS / agent-ready hints)
        # <h1> texts; meta tags found inside <body>
        self.h1_texts = []
        self.meta_in_body = 0    # <meta> count after body start
        self._in_body = False
        self._in_h1 = False
        self._h1_buf = []
        self._in_jsonld = False
        self._jsonld_buf = []
        # Observation extras: title / h1–h6 / script
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
            # Record first <html> only (nested documents ignored here)
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
    """Raw regex count (e.g. nested-document checks)."""
    return len(re.findall(pattern, html, re.I))


# ---------------------------------------------------------------------------
# Single checks: each returns {status, known, value, expected, evidence, level}
# ---------------------------------------------------------------------------

def _ok(value, expected, evidence="", level="P0"):
    return {"status": True, "known": True, "value": value, "expected": expected, "evidence": evidence, "level": level}


def _fail(value, expected, evidence="", level="P0"):
    return {"status": False, "known": True, "value": value, "expected": expected, "evidence": evidence, "level": level}


def _unknown(value, expected, evidence="", level="P2"):
    """Unknown (network / N/A): neither 0 nor full points; lowers confidence only."""
    return {"status": False, "known": False, "value": value, "expected": expected,
            "evidence": evidence, "level": level}


def _is_conn_error(resp):
    """True when the response never got an HTTP status (connection-layer failure)."""
    return isinstance(resp, dict) and resp.get("status") is None and not resp.get("ok")


def check_jsonld(html, parser):
    """JSON-LD presence and validity."""
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
    """Recursively collect @type values (including @graph)."""
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
    """Semantic HTML tags (main/article/footer/time, etc.)."""
    need = ["main", "article", "footer", "header", "nav", "time"]
    counts = {t: parser.tag_counts.get(t, 0) for t in need}
    has = all(counts.get(t, 0) >= 1 for t in ("main", "article", "footer", "time"))
    ev = " ".join("%s=%d" % (k, v) for k, v in counts.items())
    return _ok(counts, "main/article/footer/time ≥1", ev, "P1") if has else \
        _fail(counts, "main/article/footer/time ≥1", ev, "P1")


def check_main_heading(parser):
    """Primary <h1> (main heading).

    Prefer one topical <h1> per page. Zero fails; multiple still passes with a hint.
    Count is by <h1> tags (image-only logo h1 still counts).
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
    """<meta> must not appear inside <body>.

    Body metas (often from broken themes/editors) may be ignored and confuse parsers.
    """
    n = int(getattr(parser, "meta_in_body", 0) or 0)
    ev = "body 内 <meta>=%d（应全部在 <head>）" % n
    return _ok(n, "0", ev, "P2") if n == 0 else _fail(n, "0", ev, "P2")


# Security headers (CSP or frame-ancestors can satisfy the frame item)
SECURITY_HEADERS = (
    ("content-security-policy", "CSP 或 frame-ancestors"),
    ("x-content-type-options", "X-Content-Type-Options: nosniff"),
    ("referrer-policy", "Referrer-Policy"),
    ("permissions-policy", "Permissions-Policy"),
)


def check_security_headers(resp):
    """Security response headers.

    Expect CSP (or frame-ancestors), X-Content-Type-Options, Referrer-Policy,
    Permissions-Policy. Pass with at least 3 of 4 (CSP need not be strict).
    """
    h = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
    present = [label for key, label in SECURITY_HEADERS if h.get(key)]
    has_frame = bool(h.get("x-frame-options")) or ("frame-ancestors" in (h.get("content-security-policy", "") or ""))
    need = 4
    got = len(present) + (1 if (has_frame and not h.get("content-security-policy")) else 0)
    ev = "已下发=%s（%d/4）" % ("、".join(present) or "无", min(got, need))
    return _ok(got, "≥3 项安全头", ev, "P2") if got >= 3 else _fail(got, "≥3 项安全头", ev, "P2")


def check_canonical(parser):
    """rel=canonical present."""
    found = [l.get("href", "") for l in parser.links if "canonical" in l.get("rel", "").lower()]
    ev = "canonical=%s" % (found[0] if found else "无")
    return _ok(found, "有 canonical", ev, "P1") if found else \
        _fail(False, "有 canonical", ev, "P1")


def check_og(parser):
    """Open Graph / Twitter Card tags."""
    og = [m for m in parser.metas if m.get("property", "").startswith("og:")]
    tw = [m for m in parser.metas if m.get("name", "").startswith("twitter:")]
    n = len(og) + len(tw)
    ev = "og=%d twitter=%d" % (len(og), len(tw))
    return _ok(n, "≥4", ev, "P1") if n >= 4 else _fail(n, "≥4", ev, "P1")


def check_lang(parser):
    """html lang attribute."""
    lang = parser.html_attrs.get("lang", "")
    return _ok(lang, "含 lang", "lang=%s" % (lang or "无"), "P0") if lang else \
        _fail("", "含 lang", "lang=无", "P0")


def check_nested_doc(html):
    """Nested document check: exactly one html/head/DOCTYPE each."""
    n_html = count_raw(html, r"<html[\s>]")
    n_head = count_raw(html, r"<head[\s>]")
    n_dt = count_raw(html, r"<!DOCTYPE")
    total = n_html + n_head + n_dt
    ev = "<html>=%d <head>=%d <!DOCTYPE>=%d 合计=%d（期望 3）" % (n_html, n_head, n_dt, total)
    good = (n_html == 1 and n_head == 1 and n_dt == 1)
    return _ok(total, "3", ev, "P0") if good else _fail(total, "3", ev, "P0")


def check_compression(url):
    """Content-Encoding gzip/br. Connection failure → unknown."""
    r = head(url)
    if _is_conn_error(r):
        return _unknown("", "含 gzip 或 br", "抓取失败，无法判定压缩（unknown）", "P2")
    ce = r["headers"].get("Content-Encoding", "")
    ev = "Content-Encoding=%s" % (ce or "无")
    return _ok(ce, "含 gzip 或 br", ev, "P2") if ce else _fail(ce, "含 gzip 或 br", ev, "P2")


def check_http_version(url):
    """HTTP protocol version.

    urllib stops at HTTP/1.1. Prefer ALPN/curl for h2; fall back with a note.
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
            ver = {10: "1.1", 11: "1.1"}.get(v, "unknown")  # urllib cannot see h2
            note = "（urllib 无法探测 h2，建议复核）"
    ev = "http_version=%s%s" % (ver, note)
    good = ver in ("2", "2.0", "3", "3.0")
    return _ok(ver, "2 或 3（非 1.1）", ev, "P2") if good else \
        _fail(ver, "2 或 3（非 1.1）", ev, "P2")


def _http_version_via_alpn(url):
    """Detect h2 via TLS ALPN. Return (ver, note) or (None, None)."""
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
    """Probe HTTP version with curl when available; else None."""
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
    """Parse robots.txt into groups with agents / allow_root / disallow_root.

    Simplified: consecutive User-agent lines form a group; rules attach to it.
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
            cur["closed"] = True  # after a rule line, next User-agent starts a new group
    return groups


def _robots_blocked(groups, bot):
    """True if this bot is explicitly blocked with Disallow: /."""
    # 1) groups targeting this bot
    for g in groups:
        if bot in g["agents"] and g["disallow_root"] and not g["allow_root"]:
            return True
    # 2) wildcard group denies all, and no bot-specific Allow
    has_own_allow = any(bot in g["agents"] and g["allow_root"] for g in groups)
    if not has_own_allow:
        for g in groups:
            if "*" in g["agents"] and g["disallow_root"] and not g["allow_root"]:
                return True
    return False


def check_robots(base):
    """robots.txt presence, Sitemap declaration, citation-bot allow policy.

    Explicit Disallow: / → P0. Reachable but not per-bot Allow → P1.
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
    # Training-bot policy (observe only): dedicated User-agent groups
    train_named, train_blocked = [], []
    for bot in TRAINING_BOTS[:6]:
        seg = _robots_segment(body, bot)
        if seg is not None:
            train_named.append(bot)
            if _robots_blocked(groups, bot):
                train_blocked.append(bot)
    train_ev = "独立段落=%s；其中 Disallow:/=%s" % (
        ",".join(train_named) or "无", ",".join(train_blocked) or "无")
    # Any dedicated policy (allow or deny) counts as "training policy declared"
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
    """Return the User-agent segment for bot, or None if absent."""
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
    """Sitemap presence and freshness (sitemap index supported)."""
    r = fetch(base.rstrip("/") + "/sitemap.xml")
    if _is_conn_error(r):
        return {"exists": _unknown(False, "存在", "抓取失败，无法判定 sitemap（unknown）", "P0"),
                "fresh": _unknown(False, "lastmod 新鲜", "抓取失败，无法判定（unknown）", "P2"), "latest": ""}
    if not (r["ok"] or r["status"] == 200):
        return {"exists": _fail(False, "存在", "HTTP %s" % r["status"], "P0"),
                "fresh": _fail(False, "lastmod 新鲜", "不可用", "P2"), "latest": ""}
    body = r["body"]
    lastmods = re.findall(r"<lastmod>\s*([^<\s]+)", body)
    # If sitemap index, fetch first child sitemap for lastmod
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
        # No lastmod: treat as not-fresh without claiming "stale"
        fresh_res = _fail(False, "含 lastmod 且最近 90 天内有更新", ev + "（未提供 lastmod）", "P2")
    return {"exists": exists, "fresh": fresh_res, "latest": latest}


def check_ai_crawler_reach(url):
    """Live AI-crawler reachability (graded by bot tier).

    Citation/search bots blocked (non-200) → P0.
    Training bots blocked → not a scored failure (policy choice).
    Connection failure → unknown.
    """
    results = {}
    blocked_critical, blocked_training, conn_err = [], [], []
    for name, ua in PROBE_UAS.items():
        status = None
        for _ in range(3):  # up to 3 tries against transient failures
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
        ev += "（训练型 %s 被拦，属可选，不计问题）" % ",".join(blocked_training)
    if blocked_critical:
        return _fail(blocked_critical, "引用型/检索型全部 200",
                     ev + "（引用/检索型被拦截）", "P0")
    if conn_err:
        return _unknown(conn_err, "引用型/检索型全部 200",
                        ev + "（连接错误，无法判定，疑本机网络）", "P2")
    return _ok(results, "引用型/检索型全部 200", ev, "P0")


# ---- Citable content heuristics ------------------------------------------------

def check_content_citability(html, parser):
    """Heuristics: answer-first / table-or-authority links / time+price date."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # 1) Answer-first: a 20–300 char statement near the top, or FAQ/QA
    front = ""
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
    paras = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
    paras = [p for p in paras if 20 <= len(p) <= 300]
    if paras:
        front = paras[0]
    types = _extract_types_of(parser)
    has_faq = any(t in ("FAQPage", "QAPage") for t in types)
    has_answer = bool(front) or has_faq

    # 2) Data table / authority outbound links
    # Count tables with <th>/<thead> only; skip layout tables
    data_table = (parser.tag_counts.get("th", 0) >= 1 or
                  parser.tag_counts.get("thead", 0) >= 1)
    ext_auth = 0
    for m2 in re.finditer(r'href=["\'](https?://[^"\']+)', html, re.I):
        u = m2.group(1).lower()
        if any(d in u for d in AUTHORITATIVE_DOMAINS):
            ext_auth += 1
    has_table_links = data_table or ext_auth > 0

    # 3) <time datetime> plus visible price date
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


# ---- Entity signals -----------------

def _jsonld_nodes(parser):
    """Flatten JSON-LD into node dicts (expand @graph / nesting)."""
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
    """Entity signals: Organization sameAs / @id / name / url.

    返回 dict，每个子项为独立 check（供评分与问题清单引用）。
    分级：Organization 缺失或 sameAs 缺失 → P1（显著影响"这是什么实体"的判定）。
    """
    nodes = _jsonld_nodes(parser)
    # Fallback when @type sits outside @graph: scan org-ish fields
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
    """Map live UA results into a per-platform readable matrix (display only).

    Input is check_ai_crawler_reach output (value is {UA: status}).
    Not part of the base 100 (avoids double-counting the crawl item).
    """
    raw = ai_crawl.get("value")
    results = raw if isinstance(raw, dict) else {}   # value may be a list on total failure
    matrix = {}
    for plat, uas in PLATFORMS.items():
        st = [results.get(ua) for ua in uas]
        ok = any(s == 200 for s in st)
        matrix[plat] = {"ok": ok, "uas": uas, "status": st}
    return matrix


# ---- AI discovery endpoints / agent-ready / injection self-check ---------------

# endpoint → (path, label, micro-adjust weight)
AI_ENDPOINTS = [
    ("/llms.txt",               "llms.txt",        2),   # ignored by Google; useful for agents
    ("/.well-known/ai.txt",     "ai.txt",          1),
    ("/ai/summary.json",        "ai/summary.json", 1),
    ("/ai/faq.json",            "ai/faq.json",     1),
    ("/ai/service.json",        "ai/service.json", 1),
]


def check_ai_endpoints(origin):
    """AI discovery endpoints (llms.txt, ai.txt, ai/*.json).

    Low priority for classic Google ranking; useful for agents.
    Display + micro-adjust (up to +5); not part of base 100.
    Connection failure counts as missing (not unknown).
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
    """GEO injection / hidden-text self-check (high-signal patterns).

    High-confidence only (prompt phrases / data-ai-* / invisible Unicode).
    CSS hide is a hint only (display:none is common in UI).
    Hit → P2 issue + micro −5.
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
    """Agent-ready light check (display only, unscored).

    Agents locate elements; CLS from images without width/height is a risk signal.
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


# ---- Observation extras (not part of base 100 by default) ---------------------

DOM_SIZE_WARN = 1500  # warn when estimated tag count exceeds this
IMG_SAMPLE = 12       # max <img> sample for modern format check


def check_obs_faq_visible(html, parser):
    """Visible on-page FAQ/Q&A (not JSON-LD FAQPage alone)."""
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
    """Visible author/org byline (not schema alone)."""
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
    """Visible full update date (Y-M-D)."""
    times = [t for t in (parser.times or []) if t]
    full_dt = [t for t in times if re.search(r"20\d{2}-\d{2}-\d{2}", t)]
    text_hit = bool(re.search(r"20\d{2}\s*[年./\-]\s*\d{1,2}\s*[月./\-]?\s*\d{1,2}", html))
    ok = bool(full_dt) or text_hit
    ev = "time@datetime 全日=%d；正文年月日=%s" % (len(full_dt), text_hit)
    return _ok(True, "完整日期", ev, "P2") if ok else _fail(False, "完整日期", ev, "P2")


def check_obs_img_modern(parser):
    """Sample whether images use WebP/AVIF (hint, unscored)."""
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
    """DOM size threshold hint (unscored)."""
    total = sum(int(v or 0) for v in (parser.tag_counts or {}).values())
    ok = total <= DOM_SIZE_WARN
    ev = "估算标签出现合计=%d（告警线 %d）" % (total, DOM_SIZE_WARN)
    return _ok(True, "DOM 体积", ev, "P3") if ok else _fail(False, "DOM 体积", ev, "P3")


def check_obs_title_length(parser):
    """Title length hint: about 15–65 characters."""
    title = (parser.title_text or "").strip()
    n = len(title)
    ok = 15 <= n <= 65
    ev = "title 长度=%d%s" % (n, ("「%s」" % title[:40]) if title else "（空）")
    return _ok(n, "15–65 字符", ev, "P2") if ok else _fail(n, "15–65 字符", ev, "P2")


def check_obs_meta_desc_length(parser):
    """Meta description length hint: about 50–160 characters."""
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
    """Favicon present."""
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
    """Heading level jumps: do not skip from h1 straight to h3+."""
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
    """Question-style headings: at least one h2–h6 with ? or question words."""
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
    """Lazy-load placement: early images should not use loading=lazy."""
    imgs = list(parser.imgs or [])
    if not imgs:
        return _ok(True, "懒加载位置合理", "无 <img>", "P3")
    early = imgs[:3]
    lazy_early = [im for im in early if (im.get("loading") or "").lower() == "lazy"]
    ok = not lazy_early
    ev = "前 %d 张中 loading=lazy 共 %d" % (len(early), len(lazy_early))
    return _ok(True, "首屏勿过早 lazy", ev, "P3") if ok else _fail(False, "首屏勿过早 lazy", ev, "P3")


def check_obs_script_budget(parser):
    """External script count / inline size (observation)."""
    scripts = [s for s in (parser.scripts or []) if not s.get("jsonld")]
    external = [s for s in scripts if s.get("src")]
    inline_lens = [int(s.get("inline_len") or 0) for s in scripts if not s.get("src")]
    inline_total = sum(inline_lens)
    n_ext = len(external)
    ok = n_ext <= 25 and inline_total <= 80000
    ev = "外链 script=%d；内联合计=%d 字节" % (n_ext, inline_total)
    return _ok(True, "脚本预算内", ev, "P3") if ok else _fail(False, "脚本预算内", ev, "P3")


def check_obs_llms_quality(ai_endpoints):
    """llms.txt quality: present and structured (title/links)."""
    found = ai_endpoints.get("found") or []
    if "llms.txt" not in found:
        return _fail(False, "llms.txt 可用且有结构", "未检出 llms.txt", "P3")
    # Endpoint check is presence-only; evidence string carries quality hints
    ev = ai_endpoints.get("evidence") or "llms.txt 已存在"
    # Presence alone passes; hint to add headings/links
    return _ok(True, "llms.txt 可用", ev + "（建议含 # 标题与页面链接）", "P3")


def check_observe_extras(html, parser, ai_endpoints=None):
    """Observation extras (unscored)."""
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


# ---- Optional geo.new third-party audit --------------------------------------

GEO_NEW_API = "https://geo.new/api/v1/audits"
# Reuse one session so repeat submits attach/reuse recent runs,
# instead of cold-session TARGET_COOLDOWN limits.
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
    """Optional geo.new public API single-page audit (cross-check).

    Flow (public /docs/api):
      POST /api/v1/audits {"url":...} → 202 {audit_id, session, snapshot_url, ...}
      GET snapshot_url (Authorization: Bearer <session>) until complete/partial/failed
    Notes:
      · First tokenless request must omit Origin / Sec-Fetch-* (else ORIGIN_REJECTED).
      · User-Agent required (Cloudflare Error 1010 otherwise).
      · Persist/reuse `.geo_new_session` to avoid TARGET_COOLDOWN on repeats.
    Anonymous rate limits apply; failures do not break the deterministic report.
    Honours HTTPS_PROXY / https_proxy / ALL_PROXY when set.

    Returns a dict (never raises): audit_id, score, platforms, blockers, etc.
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
# Scoring (0–100) and issue bands
# ---------------------------------------------------------------------------

# Content bonus pool: 1 point each, capped at 5. Off-site signals stay out.
BONUS_POOL_KEYS = (
    "title_length", "meta_desc_length", "heading_order",
    "question_headings", "favicon", "lazy_loading", "script_budget",
)
BONUS_POOL_CAP = 5


def compute_score(checks):
    """Five-axis base score (v11).

    Citation 28, structured 18, citable 24 (bonus ≤5),
    on-page entity 18, technical rendering 12. Total 100.
    Unknown excluded from numerator and denominator.
    Micro-adjust and P0 cap are applied in finalize_score().
    """
    items = []   # (label, max, earned, known)

    def add(label, mx, earned, known=True):
        earned = max(0, min(int(earned), mx))
        items.append((label, mx, earned, bool(known)))

    def kof(r):
        return bool((r or {}).get("known", True))

    # Citation reach 28
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

    # Structured data 18 (entity on its own axis)
    jl = checks["jsonld"]
    add("有 JSON-LD 且合法", 8, 8 if jl["status"] else 0, kof(jl))
    key_types = {"Organization", "WebSite", "Article", "Product", "BreadcrumbList", "BlogPosting"}
    hit = len(key_types & set(jl.get("types", [])))
    add("关键类型齐全", 5, int(round(5 * min(hit, 4) / 4)), kof(jl))
    all_types = " ".join(jl.get("types", []))
    add("问答 FAQPage/QAPage", 5, 5 if ("FAQPage" in all_types or "QAPage" in all_types) else 0, kof(jl))

    # Citable content 19 + bonus pool ≤5 = 24
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

    # On-page entity 18
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

    # Technical rendering 12
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
    # Bonus pool weight may shrink; coverage still uses known weight vs 100.
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
    """Apply micro-adjust (±5) and P0 cap on the base score.

    Micro-adjust: discovery endpoints up to +5; injection hit −5; combined ±5.
    Any open P0 caps the final score at 74.
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
    """Score band label (excellent / good / pass / needs work).

    "Good" starts at 75 so public auditor band edges roughly align.
    """
    if score >= 90:
        return "优秀"
    if score >= 75:
        return "良好"
    if score >= 50:
        return "及格"
    return "待整改"


def collect_problems(checks):
    """Collect judged failures into a problem list, sorted P0→P3.

    Unknown items are not problems; they go to meta.unknown instead.
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
    # Sub-checks
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
    # Entity signals
    ent = checks.get("entity", {})
    for k, desc in (("org", "无 Organization 实体声明"),
                    ("sameas", "Organization 缺 sameAs 权威锚点"),
                    ("id", "Organization 缺 @id 稳定标识"),
                    ("profile", "Organization 缺 name/url")):
        r = ent.get(k)
        if failed(r):
            problems.append({"level": r["level"], "key": "entity." + k, "desc": desc, "evidence": r["evidence"]})
    # GEO injection / hidden-text self-check
    inj = checks.get("injection") or {}
    if inj.get("found"):
        problems.append({"level": "P2", "key": "injection",
                         "desc": "疑似 GEO 注入 / 隐藏文字信号（自查）",
                         "evidence": inj.get("evidence", "")})
    problems.sort(key=lambda x: order.get(x["level"], 9))
    return problems


def collect_unknown(meta, checks):
    """Collect unknown items for report / JSON."""
    out = list(meta.get("unknown", []))
    for grp, key in (("robots", "citation_allowed"), ("robots", "sitemap"),
                     ("sitemap", "exists"), ("sitemap", "fresh")):
        r = (checks.get(grp) or {}).get(key)
        if r and not r.get("known", True):
            out.append("%s.%s" % (grp, key))
    # Also list unknowns from observation extras
    for key in ("h1", "meta_in_body", "sec_headers", "lang", "semantic",
                "canonical", "og", "jsonld", "nested"):
        r = checks.get(key)
        if isinstance(r, dict) and not r.get("known", True):
            out.append(key)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Markdown report output
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
    # Confidence gating / P0 cap / micro-adjust
    cov = meta.get("coverage")
    if cov is not None:
        conf = "%.0f%%" % (cov * 100)
        note = "（正常）" if cov >= 0.7 else "（⚠️ 偏低：多项无法判定 → 结论置信度低）"
        L.append("- **结论置信度：%s**%s" % (conf, note))
    if finfo.get("capped_by_p0"):
        L.append("- ⚠️ **总分已按 P0 封顶**：存在未修复 P0 → 上限 74（及格档）")
        for d in finfo.get("p0", []):
            L.append("  - P0 未修复：%s" % d)
    if finfo.get("micro"):
        L.append("- 微调项：%+d 分（%s）" % (finfo["micro"], "；".join(finfo.get("micro_details", []))))
    L.append("")
    L.append("> 评分与分级说明见 https://www.geohttp.com/method")
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
    # Unknown items — neither 0 nor full points; listed separately
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
        L.append("| 微调项（端点/注入） | %+d |" % finfo["micro"])
    final_label = "**最终（P0 封顶）**" if finfo.get("capped_by_p0") else "**最终**"
    L.append("| %s | **%d** |" % (final_label, score))
    L.append("")
    # D. Platform readiness matrix (display)
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

    # E. Entity signals (display)
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

    # G. AI discovery endpoints / agent-ready / injection self-check
    ep = checks.get("ai_endpoints") or {}
    ag = checks.get("agent_ready") or {}
    inj = checks.get("injection") or {}
    if ep or ag or inj:
        L.append("## G. AI 发现端点 · Agent-ready · 注入自查（★ v10 新增）")
        L.append("")
        if ep:
            L.append("**① AI 发现端点**：已有 %s ｜ 缺失 %s"
                     % ("、".join(ep.get("found") or []) or "无",
                        "、".join(ep.get("missing") or []) or "无"))
            L.append("")
            L.append("> `llms.txt` 等对 **Google 无益无害**，但对 **agent / 其它系统** 有用；"
                     "成本极低，属「低优先可做」。合计最多 **+5 分微调**。")
            L.append("")
        if ag:
            mark = "✅" if ag.get("ok") else "⚠️"
            L.append("**② Agent-ready 轻检**：%s %s" % (mark, ag.get("evidence", "")))
            L.append("")
        if inj:
            mark = "❌ 命中" if inj.get("found") else "✅ 未发现"
            L.append("**③ GEO 注入 / 隐藏文字自查**：%s —— %s" % (mark, inj.get("evidence", "")))
            L.append("")
            if inj.get("found"):
                L.append("> ⚠️ 命中项会造成 AI 对整站的不信任，且可能来自广告位 / 第三方插件注入，"
                         "建议排查（−5 微调）。")
                L.append("")
    return "\n".join(L)


def render_geo_new(gn, our_score=None):
    """Render optional geo.new third-party audit block."""
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
# Main flow: audit one site profile
# ---------------------------------------------------------------------------

def discover_content_url(base, home_html):
    """Pick a content-page link (article/post) from the homepage when useful."""
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
    """Return scheme://host[:port] for root resources (robots/sitemap).

    Subpaths must not be used or .../path/robots.txt 404s falsely.
    """
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) if m else (url or "").rstrip("/")


def run_site(profile):
    base = profile["url"]
    home = fetch(base)
    html = home["body"]
    parser = parse_html(html)
    content_url = discover_content_url(base, html)

    # Prefer a content page for citable-content checks when homepage is thin
    # Fetch that page if different from home; else fall back to home
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
    # HTML-derived checks only when the page body is available; else unknown
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
# Optional AI review (loads a local helper if present; failures are soft)
# ---------------------------------------------------------------------------

def ai_review(result):
    """Optional local AI helper review. Failures do not affect the main flow."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from AI_fenxi import ai_fenxi  # lazy import: only when --ai is set
    except Exception as e:
        return "(optional AI helper unavailable: %s)" % e
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
    return "(optional AI helper failed: %s)" % r.get("error", "unknown")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="GEO citation-readiness audit (live: https://www.geohttp.com)")
    ap.add_argument("--site", help="Site URL, e.g. https://www.example.com/")
    ap.add_argument("--all", action="store_true",
                    help="Run site list (demos, or site_profiles.local.json / GEO_CHECK_SITES)")
    ap.add_argument("--name", help="Site name (optional with --site)")
    ap.add_argument("--cms", default="unknown", help="CMS label (optional with --site)")
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--out", help="Write Markdown report to a file")
    ap.add_argument("--ai", action="store_true", help="Optional local AI helper review (if installed)")
    ap.add_argument("--geo-new", action="store_true", dest="geo_new",
                    help="Optional geo.new public API cross-check")
    ap.add_argument("--geo-new-wait", type=int, default=90, dest="geo_new_wait",
                    help="Seconds to wait for geo.new (default 90)")
    args = ap.parse_args()

    if args.all:
        profiles = load_site_profiles()
    elif args.site:
        profiles = [{"name": args.name or args.site, "url": args.site, "cms": args.cms}]
    else:
        ap.error("Specify --site URL or --all")

    all_results = []
    md_all = []
    for prof in profiles:
        sys.stderr.write(">> auditing %s ...\n" % prof.get("url"))
        res = run_site(prof)
        if args.geo_new:
            sys.stderr.write(">> geo.new cross-check (max %ds)...\n" % args.geo_new_wait)
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
                # Confidence gating: three-state / coverage / P0 cap / micro-adjust
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

    # Exit 2 when any P0 remains (cron/CI)
    has_p0 = any(any(p["level"] == "P0" for p in r["problems"]) for r in all_results)
    return 2 if has_p0 else 0


if __name__ == "__main__":
    sys.exit(main())
