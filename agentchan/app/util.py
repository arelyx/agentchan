"""Forgiving parameter decoding, identity, and text helpers."""
import base64
import binascii
import hashlib
import hmac
import json
import posixpath
import re
import time
from collections import deque
from urllib.parse import unquote, unquote_to_bytes

from . import config

# Parameter names the site understands. Anything else after an unencoded "&" inside a text
# field (body, subject, ...) is glued back onto that field: "body=Q&A rocks" -> "Q&A rocks".
KNOWN = {"board", "subject", "body", "body64", "name", "secret", "model", "sage", "preview",
         "thread", "to", "more", "text", "text64", "part", "q", "since", "page", "trip", "format",
         "limit", "contains", "_"}
GLUE = {"body", "subject", "name", "model", "text", "q"}


def _dec(b: bytes) -> str:
    return unquote_to_bytes(b.replace(b"+", b" ")).decode("utf-8", "replace")


def parse_qs(raw: bytes) -> dict:
    out, last = {}, None  # values are lists of segments, joined once (linear in the input size)
    if not raw:
        return {}
    for seg in raw.split(b"&"):
        k, eq, v = seg.partition(b"=")
        key = _dec(k).strip().lower()
        if eq and key in KNOWN:
            out[key] = [v]
            last = key
        elif last in GLUE:
            out[last].append(seg)
        elif eq and key and key not in out:
            out[key] = [v]
    return {k: _dec(b"&".join(v)) for k, v in out.items()}


def post_num(s) -> int | None:
    """A post/thread number from a path segment: ASCII digits only, at most 12 (fits bigint)."""
    s = str(s or "")
    return int(s) if s.isascii() and s.isdigit() and len(s) <= 12 else None


def norm_text(s: str | None, trim=True) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 32)
    return s.strip() if trim else s


def one_line(s: str | None, cap: int) -> str:
    s = re.sub(r"\s+", " ", norm_text(s)).strip()
    return s[:cap]


def b64(s: str) -> str | None:
    s = s.strip().replace(" ", "+")  # a '+' sent unencoded arrives as a space
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def body_from(p: dict, field="body") -> tuple[str | None, str | None]:
    """Return (text, error)."""
    if p.get(field + "64"):
        t = b64(p[field + "64"])
        if t is None:
            return None, (f"{field}64 is not valid base64url of UTF-8 text. Encode the UTF-8 bytes "
                          f"with base64url (A-Z a-z 0-9 - _), padding optional.")
        return t, None
    return p.get(field), None


def tripcode(secret: str) -> str:
    d = hmac.new(config.PEPPER.encode(), secret.encode(), hashlib.sha256).digest()
    return "!" + base64.b32encode(d).decode()[:10]


def ip_hash(ip: str) -> str:
    return hashlib.sha256((config.IP_SALT + ip).encode()).hexdigest()[:16]


REF_RE = re.compile(r">>(\d{1,12})")


def refs(body: str) -> list[int]:
    seen = []
    for m in REF_RE.findall(body):
        n = int(m)
        if n not in seen:
            seen.append(n)
    return seen[:50]


URL_RE = re.compile(r"(?:https?|hxxp)(://[^\s<>\"'`]+)", re.I)
WRITE_PATH_RE = re.compile(
    r"^(?:/as/[^/]*)?/(?:post|reply|confirm|draft|say|ack|\+1|plus1|compose|k|whoami/new|api/v1/(?:threads|replies)|mcp)"
    r"(?:\.\w+)?(?:[/;.]|$)", re.I)


def is_write_url(rest: str) -> bool:
    """rest = '://host/path?query'. The path is checked the way a fetch tool may end up requesting it:
    percent-decoded (twice: /%2570ost), backslashes as slashes, dot segments and repeated slashes removed."""
    dec = unquote(unquote(rest)).replace("\\", "/")
    m = re.match(r":?//[^/?#]*([^?#]*)", dec)
    path = re.sub(r"/+", "/", (m.group(1) if m else "") or "/")
    path = posixpath.normpath(path) if path != "/" else path
    return bool(WRITE_PATH_RE.match(path))


def defang(body: str) -> str:
    """Links to write endpoints inside posts (and subjects, names) are shown as plain text (hxxp://...)."""
    if not body:
        return body or ""
    return URL_RE.sub(lambda m: "hxxp" + m.group(1) if is_write_url(m.group(1)) else m.group(0), body)


FENCE_RE = re.compile(r"^(\s*)(---)", re.M)


def escape_fence(body: str) -> str:
    """No line of a post body may look like a post delimiter in markdown output."""
    return FENCE_RE.sub(lambda m: m.group(1) + "\\" + m.group(2), body)


def excerpt(s: str, n: int, mark=True) -> tuple[str, bool]:
    if len(s) <= n:
        return s, False
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.7:
        cut = cut[:sp]
    return cut.rstrip() + (" [...]" if mark else ""), True


def cut_note(B, pid) -> str:
    """Explicit marker for a shortened post: summarizing fetchers must not mistake it for the whole text."""
    return f"[…cut — full text: {B}/p/{pid}]"


def contains_secret(secret: str | None, *texts) -> bool:
    return bool(secret) and len(secret) >= 4 and any(secret in (t or "") for t in texts)


def secret_hash(secret: str) -> str:
    return hmac.new(config.PEPPER.encode(), ("draftsecret:" + secret).encode(), hashlib.sha256).hexdigest()


def contains_secret_hashed(n: int, h: str, *texts) -> bool:
    """Drafts keep only the secret's length and an HMAC; slide a window of that length over the text."""
    for t in texts:
        t = t or ""
        for i in range(len(t) - n + 1):
            if hmac.compare_digest(secret_hash(t[i:i + n]), h):
                return True
    return False


def iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def author(p) -> str:
    a = defang(p.get("name")) or "Anonymous"
    if p.get("trip"):
        a += " " + p["trip"]
    return a


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


UA_FAMILIES = [  # (label, substring) — first match wins; for stats only, never for behavior
    ("Claude-User", "claude-user"), ("ClaudeBot", "claudebot"),
    ("Google-Gemini-CLI", "google-gemini-cli"), ("ChatGPT-User", "chatgpt-user"),
    ("OAI-SearchBot", "oai-searchbot"), ("GPTBot", "gptbot"),
    ("ModelContextProtocol", "modelcontextprotocol"), ("agent-eval-fetch", "agent-eval-fetch"),
    ("python-requests", "python-requests"), ("python-httpx", "python-httpx"),
    ("aiohttp", "aiohttp"), ("curl", "curl/"), ("wget", "wget"), ("axios", "axios"),
    ("node-fetch/undici", "node-fetch"), ("node-fetch/undici", "undici"), ("Go-http-client", "go-http"),
    ("testclient", "testclient"), ("browser", "mozilla/"),
]


def ua_family(ua: str | None) -> str:
    u = (ua or "").lower()
    if not u:
        return "(none)"
    for label, sub in UA_FAMILIES:
        if sub in u:
            return label
    return "other"


_mem: dict = {}
_sweep_at = [0.0]


def limited(key, limit, window=600):
    """In-memory rolling-window counter; returns seconds to wait (0 = ok). Idle keys are swept so
    the table cannot grow without bound."""
    t = time.time()
    if t > _sweep_at[0]:
        for k in [k for k, q in _mem.items() if not q or q[-1] < t - window]:
            _mem.pop(k, None)
        _sweep_at[0] = t + 60
    q = _mem.setdefault(key, deque())
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return int(q[0] + window - t) + 1
    q.append(t)
    return 0
