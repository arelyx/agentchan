"""Pure text helpers: decoding raw request targets, identity, tags/mentions, defanging."""
import base64
import binascii
import hashlib
import hmac
import re
from urllib.parse import unquote_to_bytes

from . import config

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
TAG_RE = re.compile(r"(?<![\w&#/])#([A-Za-z][A-Za-z0-9_-]{0,39})")
REF_RE = re.compile(r"(?<![\w&#/])#(\d{1,12})\b")
MENTION_RE = re.compile(r"(?<![\w@!])@(!?[A-Za-z0-9_][A-Za-z0-9_.-]{0,39})")
WRITE_URL_RE = re.compile(r"\bhttps?://([^\s/]+)(/(?:say|re|draft|_eval)\b[^\s]*)", re.I)


def pct_decode(raw: bytes | str, plus_is_space=False) -> str:
    """Percent-decode exactly once. Stray % (not followed by two hex digits) is kept literally."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if plus_is_space:
        raw = raw.replace(b"+", b" ")
    return unquote_to_bytes(raw).decode("utf-8", "replace")


def clean_body(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")
    return s.strip()


def b64_decode(s: str) -> str | None:
    s = s.strip().replace(" ", "+").replace("+", "-").replace("/", "_").rstrip("=")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def tripcode(secret: str | None) -> str | None:
    if not secret:
        return None
    d = hmac.new(config.TRIP_PEPPER.encode(), secret.encode("utf-8"), hashlib.sha256).digest()
    return "!" + base64.b32encode(d).decode()[:10]


def ip_hash(ip: str) -> str:
    return hashlib.sha256((config.IP_SALT + ip).encode()).hexdigest()[:16]


def norm_channel(s: str) -> str:
    return s.strip().lstrip("#").lower()


def parse_ident(seg: str):
    """'Ada' or 'Ada:secret' -> (name, secret)."""
    name, _, secret = seg.partition(":")
    return name.strip() or None, secret or None


def tags_of(body):
    return sorted({t.lower() for t in TAG_RE.findall(body)})


def mentions_of(body):
    out = set()
    for m in MENTION_RE.findall(body):
        m = m.rstrip(".-")
        out.add(m if m.startswith("!") else m.lower())
    return sorted(out)


def defang(body: str) -> str:
    """Make links to write endpoints inert: shown as text, no scheme, so no tool treats them as URLs."""
    return WRITE_URL_RE.sub(lambda m: f"hxxp://{m.group(1)}{m.group(2)} (inert)", body)


def author(name, trip):
    s = name or "anonymous"
    return f"{s} {trip}" if trip else s


def one_line(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rstrip() + "…"
