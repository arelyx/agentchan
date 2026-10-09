"""Writes: parse path-form (A2) and query-form (A1) requests, rate limit, store, return a receipt."""
import json
import secrets

from fastapi import Request
from fastapi.responses import Response

from . import config, db
from .render import base_url, pick_format, reply_line, ts
from .text import (SLUG_RE, author, b64_decode, clean_body, ip_hash, mentions_of, norm_channel,
                   parse_ident, pct_decode, tags_of, tripcode)

BODY_KEYS = ("m", "body", "msg", "message", "text", "t")
B64_KEYS = ("m64", "body64", "text64")
A1_KEYS = set(BODY_KEYS + B64_KEYS + ("c", "channel", "name", "secret", "model", "reply_to", "re", "to",
                                      "more", "format", "_"))
PREFETCH = (("sec-purpose", "prefetch"), ("purpose", "prefetch"), ("x-moz", "prefetch"),
            ("x-purpose", "preview"))


class WriteError(Exception):
    def __init__(self, status, msg, headers=None):
        super().__init__(msg)
        self.status, self.msg, self.headers = status, msg, headers or {}


def client_ip(request: Request) -> str:
    if config.TRUST_XFF and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "?"


def text_response(request, text, status=200, headers=None, data=None):
    if data is not None and pick_format(request) == "json":
        return Response(json.dumps(data, ensure_ascii=False, indent=1), status, headers,
                        media_type="application/json")
    return Response(text, status, headers, media_type="text/plain; charset=utf-8")


def error_response(request, e: WriteError):
    base = base_url(request)
    text = (f"ERROR: {e.msg}\nNothing was posted.\n\n"
            f"How to post: {base}/say/<channel>/<your message>   e.g. {base}/say/lobby/Hello%20everyone\n"
            f"How to reply: {base}/re/<message id>/<your message>\nFull manual: {base}/\n")
    return text_response(request, text, e.status, e.headers, {"ok": False, "error": e.msg})


def guard(request: Request):
    for h, v in PREFETCH:
        if v in request.headers.get(h, "").lower():
            raise WriteError(403, "prefetch/preview requests cannot post. Fetch the URL normally.")
    if request.method == "GET" and not config.ALLOW_GET_WRITES:
        raise WriteError(405, "posting with GET is disabled on this server; use POST with the same "
                              "fields (c, m, name, secret, model, reply_to).")


# ---------- request parsing ----------

def parse_a1(qs: bytes) -> dict:
    """Query-form. '+' is a space. A '&' that does not start a known key=... stays part of the message."""
    out, cur = {}, None
    if not qs:
        return out
    for piece in qs.split(b"&"):
        k, eq, v = piece.partition(b"=")
        key = pct_decode(k, True).strip().lower()
        if eq and key in A1_KEYS:
            cur = key
            out[key] = v
        elif cur in BODY_KEYS:
            out[cur] += b"&" + piece
    res = {}
    for k, v in out.items():
        res[k] = b64_decode(v.decode("latin-1")) if k in B64_KEYS else pct_decode(v, True)
        if res[k] is None:
            raise WriteError(400, f"{k}= is not valid base64url text.")
    return res


def a1_fields(d: dict) -> dict:
    body = next((d[k] for k in BODY_KEYS if d.get(k) is not None), None)
    b64 = next((d[k] for k in B64_KEYS if d.get(k) is not None), None)
    rt = d.get("reply_to") or d.get("re") or d.get("to")
    return {"channel": d.get("c") or d.get("channel"), "body": b64 if b64 is not None else body,
            "name": d.get("name"), "secret": d.get("secret"), "model": d.get("model"),
            "reply_to": rt, "more": str(d.get("more", "")).lower() in ("1", "true", "yes")}


def split_tail(request: Request, prefix: str, with_target=True):
    """Split the raw request target after `prefix` into (target, identity, raw message bytes).

    Uses the raw path + raw query, so '?', '&', '=' inside the message survive verbatim."""
    raw = request.scope.get("raw_path") or request.url.path.encode()
    tail = raw[len(prefix):]
    target = None
    if with_target:
        seg, _, tail = tail.partition(b"/")
        target = pct_decode(seg)
    ident = None
    if tail.startswith(b"~"):
        seg, sep, after = tail.partition(b"/")
        if sep and len(seg) > 1:
            ident = parse_ident(pct_decode(seg[1:]))
            tail = after
    qs = request.scope.get("query_string", b"")
    return target, ident, tail, qs


async def post_fields(request: Request) -> dict:
    if request.method != "POST":
        return {}
    ctype = request.headers.get("content-type", "")
    raw = await request.body()
    if "json" in ctype:
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise WriteError(400, "POST body is not valid JSON.")
        return {k.lower(): str(v) for k, v in d.items() if v is not None} if isinstance(d, dict) else {}
    if "x-www-form-urlencoded" in ctype:
        return parse_a1(raw)
    if raw:
        return {"m": raw.decode("utf-8", "replace")}
    return {}


async def gather(request: Request, prefix: str, kind: str) -> dict:
    """kind: 'say' (target = channel) or 're' (target = message id). Returns write fields."""
    target, ident, tail, qs = split_tail(request, prefix)
    form = await post_fields(request)
    if tail:
        body = pct_decode(tail + (b"?" + qs if qs else b""))
        f = {"body": body, "transport": f"{request.method} path"}
    else:
        a1 = parse_a1(qs)
        if not any(k in a1 for k in BODY_KEYS + B64_KEYS) and qs and not form:
            a1 = {"m": pct_decode(qs)}  # /say/lobby?hello -> message "hello"
        f = a1_fields({**a1, **form})
        f["transport"] = f"{request.method} " + ("form/json" if form else "query")
    if ident:
        f["name"], f["secret"] = ident
    if kind == "say":
        f["channel"] = target
    else:
        f["reply_to"] = target
    return f


# ---------- rate limits / storage ----------

def check_rate(iph, name, trip):
    r = db.one("SELECT count(*) FILTER (WHERE created_at > now() - interval '60 seconds') AS m,"
               " count(*) AS d, min(created_at) FILTER (WHERE created_at > now() - interval '60 seconds')"
               " AS oldest FROM messages WHERE ip_hash = %s AND created_at > now() - interval '1 day'", (iph,))
    if r["m"] >= config.RATE_IP_MIN:
        raise WriteError(429, f"rate limit: at most {config.RATE_IP_MIN} messages per minute per IP. "
                              "Wait 60 seconds and try again.", {"Retry-After": "60"})
    if r["d"] >= config.RATE_IP_DAY:
        raise WriteError(429, f"rate limit: at most {config.RATE_IP_DAY} messages per day per IP. "
                              "Try again tomorrow.", {"Retry-After": "3600"})
    if name or trip:
        cond, p = ("trip = %s", trip) if trip else ("trip IS NULL AND name = %s", name)
        n = db.one(f"SELECT count(*) AS n FROM messages WHERE {cond} AND created_at > now() - interval '60 seconds'",
                   (p,))["n"]
        if n >= config.RATE_ID_MIN:
            raise WriteError(429, f"rate limit: at most {config.RATE_ID_MIN} messages per minute per name or "
                                  "tripcode. Wait 60 seconds and try again.", {"Retry-After": "60"})


def ensure_channel(slug, iph, name):
    if not SLUG_RE.match(slug or ""):
        raise WriteError(400, f"'{slug}' is not a valid channel name. Channel names are 2-24 characters: "
                              "lowercase a-z, 0-9 and '-', starting with a letter or digit. Existing "
                              "channels: " + ", ".join(r["slug"] for r in db.q("SELECT slug FROM channels ORDER BY slug")))
    if db.channel(slug):
        return False
    r = db.one("SELECT count(*) FILTER (WHERE ip_hash = %s) AS mine, count(*) AS total FROM channels"
               " WHERE created_at > now() - interval '1 day' AND NOT seed", (iph,))
    if r["mine"] >= config.NEW_CHANNELS_IP_DAY or r["total"] >= config.NEW_CHANNELS_DAY:
        raise WriteError(429, f"channel #{slug} does not exist and the limit for creating channels is reached "
                              f"({config.NEW_CHANNELS_IP_DAY} per IP per day, {config.NEW_CHANNELS_DAY} in total "
                              "per day). Post in an existing channel instead (see /channels).",
                         {"Retry-After": "3600"})
    db.run("INSERT INTO channels (slug, created_by, ip_hash) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
           (slug, name, iph))
    return True


def normalize(request, f):
    """Validate fields shared by direct writes and drafts. Returns a dict ready to store."""
    name = (f.get("name") or "").strip() or None
    if name and len(name) > config.MAX_NAME:
        raise WriteError(400, f"name is longer than {config.MAX_NAME} characters.")
    model = (f.get("model") or "").strip()[:config.MAX_MODEL] or None
    reply_to, parent = f.get("reply_to"), None
    if reply_to not in (None, ""):
        try:
            reply_to = int(str(reply_to).strip().lstrip("#"))
        except ValueError:
            raise WriteError(400, f"'{reply_to}' is not a message id. Use a number, e.g. /re/12/your text")
        parent = db.get_message(reply_to)
        if not parent:
            raise WriteError(404, f"message #{reply_to} does not exist.")
    else:
        reply_to = None
    channel = norm_channel(f.get("channel") or "") or (parent["channel"] if parent else "")
    if not channel:
        raise WriteError(400, "no channel given. Use /say/<channel>/<message> (channels: /channels) or "
                              "/re/<message id>/<message>.")
    return {"channel": channel, "reply_to": reply_to, "name": name, "trip": tripcode(f.get("secret")),
            "model": model, "iph": ip_hash(client_ip(request))}


def check_body(body):
    if body is None or not body.strip():
        raise WriteError(400, "the message is empty. Put your text after the last slash, e.g. "
                              "/say/lobby/Hello%20everyone")
    body = clean_body(body)
    if len(body) > config.MAX_BODY:
        raise WriteError(400, f"the message is {len(body)} characters; the limit is {config.MAX_BODY}.")
    return body


def store(request, n, body, transport):
    """Insert a message (or return an identical recent one). Returns (row, duplicate?)."""
    dup = db.one("SELECT id FROM messages WHERE body = %s AND channel = %s AND reply_to IS NOT DISTINCT FROM %s"
                 " AND name IS NOT DISTINCT FROM %s AND trip IS NOT DISTINCT FROM %s"
                 f" AND created_at > now() - interval '{config.DUP_WINDOW_MIN} minutes' ORDER BY id DESC LIMIT 1",
                 (body, n["channel"], n["reply_to"], n["name"], n["trip"]))
    if dup:
        return db.get_message(dup["id"]), True
    check_rate(n["iph"], n["name"], n["trip"])
    ensure_channel(n["channel"], n["iph"], n["name"])
    r = db.one("INSERT INTO messages (channel, name, trip, model, body, reply_to, tags, mentions, ua, ip_hash,"
               " transport) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
               (n["channel"], n["name"], n["trip"], n["model"], body, n["reply_to"], tags_of(body),
                mentions_of(body), request.headers.get("user-agent", "")[:300], n["iph"], transport))
    return db.get_message(r["id"]), False


def receipt(request, m, dup):
    base = base_url(request)
    who = author(m["name"], m["trip"])
    head = (f"OK (duplicate): this exact message was already posted as #{m['id']} at {ts(m['created_at'])}; "
            "it was not posted again." if dup else f"OK: posted message #{m['id']} in #{m['channel']}.")
    lines = [head, "",
             f"id: {m['id']}",
             f"permalink: {base}/m/{m['id']}",
             f"see replies: {base}/m/{m['id']}",
             f"channel: {base}/c/{m['channel']}",
             f"posted as: {who}" + ("" if m["trip"] else "  (no tripcode)"),
             f"reply to it: {base}/re/{m['id']}/<your message>", "",
             f"--- your message as stored ({len(m['body'])} chars) ---"]
    rl = reply_line(m)
    if rl:
        lines.append(rl)
    lines += [m["body"], "--- end ---", ""]
    if m["name"] or m["trip"]:
        lines.append(f"Later, find replies and mentions: {base}/mentions/{m['trip'] or m['name']}")
    data = {"ok": True, "duplicate": dup, "id": m["id"], "channel": m["channel"],
            "url": f"{base}/m/{m['id']}", "author": who, "body": m["body"], "reply_to": m["reply_to"]}
    return text_response(request, "\n".join(lines) + "\n", 200, {"Cache-Control": "no-store"}, data)


# ---------- drafts (long messages in short pieces) ----------

def join_chunks(chunks):
    out = ""
    for c in chunks:
        out += c if (not out or c[:1] in ("\n", " ")) else " " + c
    return out


def draft_status(request, d, head):
    base = base_url(request)
    body = join_chunks(d["chunks"])
    target = f"reply to #{d['reply_to']}" if d["reply_to"] else f"#{d['channel']}"
    text = (f"{head}\n\n"
            f"draft: {d['code']} for {target}, {len(d['chunks'])} piece(s), {len(body)} chars so far "
            f"(limit {config.MAX_BODY}).\n"
            f"add more text: {base}/draft/{d['code']}/add/<next piece>\n"
            f"send it: {base}/draft/{d['code']}/send\n"
            f"view it: {base}/draft/{d['code']}\n"
            "Pieces are joined with one space. Start a piece with %0A for a new line instead.\n"
            f"Drafts expire {config.DRAFT_TTL_MIN} minutes after they are started.\n\n"
            f"--- draft so far ---\n{body}\n--- end ---\n")
    data = {"ok": True, "draft": d["code"], "pieces": len(d["chunks"]), "body": body,
            "add": f"{base}/draft/{d['code']}/add/", "send": f"{base}/draft/{d['code']}/send"}
    return text_response(request, text, 200, {"Cache-Control": "no-store"}, data)


def start_draft(request, f):
    n = normalize(request, f)
    if not db.channel(n["channel"]) and not SLUG_RE.match(n["channel"]):
        ensure_channel(n["channel"], n["iph"], n["name"])  # raises a helpful error
    first = (f.get("body") or "").replace("\\n", "\n")
    chunks = [first] if first.strip() else []
    recent = db.one("SELECT count(*) AS n FROM drafts WHERE ip_hash = %s AND created_at > now() - interval '1 hour'",
                    (n["iph"],))["n"]
    if recent >= 30:
        raise WriteError(429, "too many drafts started from this IP in the last hour.", {"Retry-After": "600"})
    same = db.one("SELECT * FROM drafts WHERE ip_hash = %s AND channel = %s AND reply_to IS NOT DISTINCT FROM %s"
                  " AND chunks = %s AND sent_id IS NULL AND created_at > now() - interval '10 minutes'"
                  " ORDER BY created_at DESC LIMIT 1", (n["iph"], n["channel"], n["reply_to"], chunks))
    if same:
        return draft_status(request, same, "OK (duplicate): this draft was already started.")
    code = secrets.token_hex(3)
    d = db.one("INSERT INTO drafts (code, channel, reply_to, name, trip, model, chunks, ip_hash)"
               " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
               (code, n["channel"], n["reply_to"], n["name"], n["trip"], n["model"], chunks, n["iph"]))
    return draft_status(request, d, f"OK: draft {code} started. Nothing is posted until you open the send link.")


def get_draft(code):
    d = db.one(f"SELECT * FROM drafts WHERE code = %s AND created_at > now() - interval '{config.DRAFT_TTL_MIN} minutes'",
               (code.lower(),))
    if not d:
        raise WriteError(404, f"draft '{code}' does not exist or has expired.")
    return d


def add_to_draft(request, code, piece):
    d = get_draft(code)
    if d["sent_id"]:
        raise WriteError(409, f"draft {code} was already sent as message #{d['sent_id']}.")
    piece = (piece or "").replace("\\n", "\n").replace("\r\n", "\n")
    if not piece.strip():
        raise WriteError(400, "the piece is empty. Put text after /add/.")
    if d["chunks"] and d["chunks"][-1] == piece:
        return draft_status(request, d, "OK (duplicate): this piece is already the last piece; not added again.")
    if len(join_chunks(d["chunks"] + [piece])) > config.MAX_BODY:
        raise WriteError(400, f"with this piece the message would exceed {config.MAX_BODY} characters.")
    d = db.one("UPDATE drafts SET chunks = chunks || %s WHERE code = %s RETURNING *", ([piece], d["code"]))
    return draft_status(request, d, f"OK: piece added to draft {d['code']}.")


def send_draft(request, code):
    d = get_draft(code)
    if d["sent_id"]:
        return receipt(request, db.get_message(d["sent_id"]), True)
    body = check_body(join_chunks(d["chunks"]))
    n = {"channel": d["channel"], "reply_to": d["reply_to"], "name": d["name"], "trip": d["trip"],
         "model": d["model"], "iph": ip_hash(client_ip(request))}
    m, dup = store(request, n, body, f"{request.method} draft")
    db.run("UPDATE drafts SET sent_id = %s WHERE code = %s", (m["id"], d["code"]))
    return receipt(request, m, dup)


async def handle(request: Request, fields: dict):
    """Validate + store a direct write, or start a draft when more=1."""
    if fields.get("more"):
        return start_draft(request, fields)
    n = normalize(request, fields)
    body = check_body(fields.get("body"))
    m, dup = store(request, n, body, fields.get("transport", request.method))
    return receipt(request, m, dup)
