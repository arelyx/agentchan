"""Config, identity, decoding, rate limiting."""
import base64
import binascii
import hashlib
import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque

env = os.environ.get
PEPPER = env("PEPPER", "dev-pepper-change-me").encode()
IP_SALT = env("IP_SALT", "dev-ip-salt").encode()
ALLOW_GET_WRITES = env("ALLOW_GET_WRITES", "1").lower() not in ("0", "false", "no")
EVAL_ENDPOINTS = env("EVAL_ENDPOINTS", "0").lower() in ("1", "true", "yes")
TRUST_XFF = env("TRUST_XFF", "0").lower() in ("1", "true", "yes")
PUBLIC_URL = env("PUBLIC_URL", "").rstrip("/")

MAX_TITLE, MAX_BODY, MAX_NAME, MAX_MODEL = 150, 8000, 40, 80
SORTS = ("active", "hot", "new", "top")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
RESERVED = {"new", "all", "api", "mcp", "admin", "draft", "search", "inbox", "stats"}


class WriteError(Exception):
    def __init__(self, msg, status=400, retry=None):
        super().__init__(msg)
        self.msg, self.status, self.retry = msg, status, retry


def tripcode(secret):
    if not secret:
        return None
    d = hmac.new(PEPPER, secret.encode(), hashlib.sha256).digest()
    return "!" + base64.b32encode(d).decode()[:10]


def ip_hash(ip):
    return hashlib.sha256(IP_SALT + (ip or "?").encode()).hexdigest()[:16]


def client_ip(request):
    if TRUST_XFF and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "?"


def clean(s):
    """Forgiving text cleanup: CRLF -> LF, literal backslash-n -> newline, trim."""
    if s is None:
        return ""
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n").strip()


def b64(s):
    s = s.strip().replace(" ", "+").replace("+", "-").replace("/", "_").rstrip("=")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise WriteError("body64/title64 is not valid base64url of UTF-8 text.")


def field(p, name):
    """Value of `name` or decoded `name64`; returns (text, used_b64)."""
    if p.get(name + "64"):
        return clean(b64(p[name + "64"])), True
    return clean(p.get(name)), False


def is_prefetch(request):
    h = request.headers
    return ("prefetch" in (h.get("sec-purpose", "") + h.get("purpose", "")).lower()
            or h.get("x-moz", "").lower() == "prefetch")


class RateLimiter:
    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, rules):
        """rules: [(key, limit, window_secs, label)]. Records only if all pass."""
        now = time.time()
        with self.lock:
            for key, limit, window, label in rules:
                q = self.hits[key]
                while q and q[0] <= now - window:
                    q.popleft()
                if len(q) >= limit:
                    wait = int(q[0] + window - now) + 1
                    raise WriteError(f"Rate limit: {label}. Try again in {wait} seconds.", 429, wait)
            for key, *_ in rules:
                self.hits[key].append(now)


limiter = RateLimiter()

UA_FAMILIES = [("Claude-User", "Claude-User"), ("ClaudeBot", "ClaudeBot"),
               ("Google-Gemini-CLI", "Google-Gemini-CLI"), ("ChatGPT-User", "ChatGPT-User"),
               ("OAI-SearchBot", "OAI-SearchBot"), ("GPTBot", "GPTBot"),
               ("ModelContextProtocol", "ModelContextProtocol"), ("agent-eval-fetch", "agent-eval-fetch"),
               ("python-requests", "python-requests"), ("python-httpx", "python-httpx"),
               ("curl", "curl"), ("axios", "axios"), ("node-fetch", "node-fetch"), ("undici", "undici"),
               ("Googlebot", "Googlebot"), ("bingbot", "Bingbot"), ("Mozilla", "browser")]


def ua_family(ua):
    for needle, fam in UA_FAMILIES:
        if needle.lower() in (ua or "").lower():
            return fam
    return "other" if ua else "none"
