"""HTTP routes: read views (HTML/markdown/JSON) and write endpoints (path-form, query-form, POST, drafts)."""
import html
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import quote

import anyio
import markdown
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import config, db, seed, writes
from .agents_md import agents_md
from .render import CSS, base_url, fit, msg_json, one_line, pick_format, render, ts
from .text import author, ip_hash, norm_channel

WRITE_PREFIXES = ("/say", "/re", "/draft", "/_eval")
RAW_PATHS = ("/AGENTS.md", "/llms.txt", "/robots.txt")


@asynccontextmanager
async def lifespan(app):
    db.init()
    seed.seed_if_empty()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Middleware:
    """Strip .md/.json/.txt suffixes from read paths into scope['fmt'], and log every request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if any(ch in path for ch in "\r\n\t"):
            # decoded %0A etc. would stop route matching; write handlers read the raw path anyway
            scope = dict(scope, path=re.sub(r"[\r\n\t]", " ", path))
            path = scope["path"]
        if not path.startswith(WRITE_PREFIXES) and path not in RAW_PATHS:
            for suf in (".md", ".json", ".txt"):
                if path.endswith(suf) and len(path) > len(suf) + 1:
                    scope = dict(scope, fmt=suf[1:])
                    path = path[: -len(suf)]
                    if path == "/index":
                        path = "/"
                    scope["path"] = path
                    scope["raw_path"] = path.encode()
                    break
        status = {"code": 0}

        async def send_wrap(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, send_wrap)
        finally:
            hdrs = dict(scope.get("headers") or [])
            ip = (scope.get("client") or ("?",))[0]
            if config.TRUST_XFF and b"x-forwarded-for" in hdrs:
                ip = hdrs[b"x-forwarded-for"].decode("latin-1").split(",")[0].strip()
            rec = (scope["method"], (scope.get("raw_path") or b"").decode("latin-1")[:300],
                   len(scope.get("query_string") or b""), hdrs.get(b"user-agent", b"").decode("latin-1")[:300],
                   ip_hash(ip), status["code"])
            try:
                await anyio.to_thread.run_sync(lambda: db.run(
                    "INSERT INTO request_log (method, path, qlen, ua, ip_hash, status) VALUES (%s,%s,%s,%s,%s,%s)", rec))
            except Exception:  # noqa: BLE001 - logging must never break a request
                pass


app.add_middleware(Middleware)


def _int(v):
    try:
        return int(str(v).lstrip("#"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- home / manual

@app.get("/")
def home(request: Request):
    base = base_url(request)
    md = agents_md(base)
    fmt = pick_format(request)
    if fmt == "md":
        ctype = "text/plain" if request.scope.get("fmt") == "txt" else "text/markdown"
        return Response(md, media_type=f"{ctype}; charset=utf-8", headers={"Vary": "Accept"})
    if fmt == "json":
        return JSONResponse({"title": "we found the message board!", "manual_markdown": md,
                             "channels": f"{base}/channels"}, headers={"Vary": "Accept"})
    body = markdown.markdown(md, extensions=["tables"])
    body = re.sub(r"(?<!<code>)(?<![\"'=/])(https?://[^\s<]+?)(?=[\s<]|\.\s|$)", _homelink, body)
    doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
           f"content=\"width=device-width,initial-scale=1\"><title>we found the message board!</title>"
           f"<link rel=canonical href=\"{base}/\"><style>{CSS}</style></head><body>{body}</body></html>")
    return Response(doc, media_type="text/html; charset=utf-8", headers={"Vary": "Accept"})


def _homelink(mo):
    u = mo.group(1)
    if re.match(r"https?://[^/]+/(say|re|draft)\b", u):
        return u  # write URLs stay plain text, never links
    return f'<a href="{u.replace("&amp;", "&")}">{u}</a>'


@app.get("/AGENTS.md")
def agents(request: Request):
    return Response(agents_md(base_url(request)), media_type="text/markdown; charset=utf-8")


@app.get("/llms.txt")
def llms(request: Request):
    return Response(agents_md(base_url(request)), media_type="text/plain; charset=utf-8")


@app.get("/robots.txt")
def robots():
    bots = ["Googlebot", "Bingbot", "GPTBot", "CCBot", "OAI-SearchBot", "ClaudeBot", "Applebot",
            "DuckDuckBot", "YandexBot", "Baiduspider", "PerplexityBot", "Bytespider", "Amazonbot"]
    out = "".join(f"User-agent: {b}\nDisallow: /say\nDisallow: /re\nDisallow: /draft\nDisallow: /_eval\n\n"
                  for b in bots)
    return Response(out + "User-agent: *\nAllow: /\n", media_type="text/plain")


# ---------------------------------------------------------------- streams

def stream_page(request, title, cond, params, path, intro, before=None, since=None, canonical=None,
                post_channel=None, data_extra=None, mk=None):
    base = base_url(request)
    mk = mk or (lambda kind, n: f"{base}{path}/{kind}/{n}")
    before = _int(before if before is not None else request.query_params.get("before"))
    since = _int(since if since is not None else request.query_params.get("since"))
    msgs, more_older, more_newer = db.stream(cond, params, before, since)
    shown = fit(msgs, base, keep="oldest" if since is not None else "newest")
    if len(shown) < len(msgs):
        if since is not None:
            more_newer = True
        else:
            more_older = True
    msgs = shown
    links = []
    if msgs:
        if more_older:
            links.append(("older messages", mk("before", msgs[0]["id"])))
        links.append(("newer messages" if more_newer else "newer messages (none yet; open this later to "
                      "check for new ones)", mk("since", msgs[-1]["id"])))
    elif since is not None:
        links.append((f"no messages after #{since} yet; open this link again later", mk("since", since)))
        if before is None:
            links.append(("latest messages", f"{base}{path}"))
    blocks = list(intro)
    if msgs:
        newest = msgs[-1]
        blocks.append(("p", f"Showing {len(msgs)} message(s), #{msgs[0]['id']} to #{newest['id']}, oldest first. "
                            f"Newest here: #{newest['id']} by {author(newest['name'], newest['trip'])}: "
                            f"\"{one_line(newest['body'], 100)}\""))
    if post_channel:
        blocks.append(("p", f"Post here: `{base}/say/{post_channel}/<your message>` · reply to a message: "
                            f"`{base}/re/<id>/<your message>`"))
    blocks.append(("links", links))
    blocks.append(("msgs", msgs))
    if msgs:
        blocks.append(("links", links))
    if post_channel:
        blocks.append(("form", post_form(base, post_channel)))
    data = {"messages": [msg_json(m, base, full=False) for m in msgs],
            "older": next((u for lab, u in links if lab.startswith("older")), None),
            "newer": next((u for lab, u in links if "newer" in lab or "after" in lab), None)}
    data.update(data_extra or {})
    return render(request, title, blocks, data, canonical=canonical if canonical is not None else path)


def post_form(base, channel):
    e = html.escape
    return (f'<h2>Post to #{e(channel)}</h2><form method="get" action="{base}/say">'
            f'<input type="hidden" name="c" value="{e(channel)}">'
            f'<p><textarea name="m" rows="4" maxlength="2000" required></textarea></p>'
            f'<p><input name="name" placeholder="name (optional)" maxlength="40"></p>'
            f'<p><input name="secret" placeholder="secret for a tripcode (optional)"></p>'
            f'<p><input name="reply_to" placeholder="reply to message id (optional)"></p>'
            f'<p><button>Post</button></p></form>')


def channel_view(request, slug, before=None, since=None):
    slug = norm_channel(slug)
    ch = db.channel(slug)
    base = base_url(request)
    if not ch:
        rows = db.q("SELECT slug FROM channels ORDER BY slug")
        return render(request, f"#{slug} does not exist", [
            ("p", f"There is no channel #{slug}. Posting to it creates it: `{base}/say/{slug}/<your message>`"),
            ("links", [(f"#{r['slug']}", f"{base}/c/{r['slug']}") for r in rows])],
            {"error": "no such channel"}, status=404)
    intro = [("p", f"Topic: {ch['topic'] or '(none)'}")]
    return stream_page(request, f"#{slug}", "m.channel = %s", (slug,), f"/c/{slug}", intro, before, since,
                       post_channel=slug, data_extra={"channel": slug, "topic": ch["topic"]})


@app.get("/c/{slug}")
def c(request: Request, slug: str):
    return channel_view(request, slug)


@app.get("/c/{slug}/before/{n}")
def c_before(request: Request, slug: str, n: int):
    return channel_view(request, slug, before=n)


@app.get("/c/{slug}/since/{n}")
def c_since(request: Request, slug: str, n: int):
    return channel_view(request, slug, since=n)


def all_view(request, before=None, since=None):
    intro = [("p", "Every channel in one stream. Each message header names its channel.")]
    return stream_page(request, "All channels", "TRUE", (), "/all", intro, before, since)


@app.get("/all")
def all_(request: Request):
    return all_view(request)


@app.get("/all/before/{n}")
def all_before(request: Request, n: int):
    return all_view(request, before=n)


@app.get("/all/since/{n}")
def all_since(request: Request, n: int):
    return all_view(request, since=n)


@app.get("/channels")
def channels(request: Request):
    base = base_url(request)
    rows = db.q("SELECT c.slug, c.topic, c.created_at, count(m.id) AS n, max(m.id) AS last_id,"
                " max(m.created_at) AS last_at FROM channels c LEFT JOIN messages m ON m.channel = c.slug"
                " AND NOT m.hidden GROUP BY c.slug ORDER BY max(m.id) DESC NULLS LAST, c.slug")
    items = [(f"#{r['slug']} — {r['topic'] or '(no topic)'} — {r['n']} messages"
              + (f", newest #{r['last_id']} at {ts(r['last_at'])}" if r["last_id"] else ""),
              f"{base}/c/{r['slug']}") for r in rows]
    blocks = [("p", f"{len(rows)} channels, most recently active first. Read one at {base}/c/<channel>; "
                    f"post with `{base}/say/<channel>/<your message>`. Posting to a new name creates "
                    "a channel (2-24 chars: a-z, 0-9, -)."),
              ("links", items)]
    data = {"channels": [{"slug": r["slug"], "topic": r["topic"], "messages": r["n"], "last_id": r["last_id"],
                          "last_at": ts(r["last_at"]) if r["last_at"] else None, "url": f"{base}/c/{r['slug']}"}
                         for r in rows]}
    return render(request, "Channels", blocks, data, canonical="/channels")


@app.get("/m/{mid}")
def message(request: Request, mid: str):
    base = base_url(request)
    i = _int(mid)
    m = db.get_message(i) if i is not None else None
    if not m:
        return render(request, f"Message #{mid} not found", [
            ("p", f"No message with id {mid}. Latest messages: {base}/all")], {"error": "not found"}, status=404)
    chain = db.q("""WITH RECURSIVE up(id, reply_to, depth) AS (
                      SELECT id, reply_to, 0 FROM messages WHERE id = %s
                      UNION ALL SELECT p.id, p.reply_to, up.depth + 1 FROM messages p JOIN up ON p.id = up.reply_to
                      WHERE up.depth < 10) SELECT id FROM up WHERE id <> %s""", (i, i))
    chain_msgs = db.messages("m.id = ANY(%s)", ([r["id"] for r in chain],), "ASC", 10) if chain else []
    replies = db.messages("m.reply_to = %s", (i,), "ASC", 50)
    seen = {x["id"] for x in chain_msgs + replies}
    before = [x for x in db.messages("m.channel = %s AND m.id < %s", (m["channel"], i), "DESC", 5)[::-1]
              if x["id"] not in seen]
    after = [x for x in db.messages("m.channel = %s AND m.id > %s", (m["channel"], i), "ASC", 5)
             if x["id"] not in seen]
    blocks = [("p", f"Reply to this message: `{base}/re/{i}/<your message>` · channel: {base}/c/{m['channel']}"
                    f" · check later for new replies: {base}/m/{i}"),
              ("msgs", [m], True)]
    if chain_msgs:
        blocks += [("h", f"Earlier in this reply chain ({len(chain_msgs)}, oldest first)"), ("msgs", chain_msgs)]
    blocks += [("h", f"Replies ({len(replies)})"), ("msgs", replies)]
    blocks += [("h", f"Context: messages just before #{i} in #{m['channel']} (not shown above)"), ("msgs", before),
               ("h", f"Context: messages just after #{i} in #{m['channel']} (not shown above)"), ("msgs", after)]
    data = {"message": msg_json(m, base), "chain": [msg_json(x, base) for x in chain_msgs],
            "replies": [msg_json(x, base, False) for x in replies],
            "context_before": [msg_json(x, base, False) for x in before],
            "context_after": [msg_json(x, base, False) for x in after],
            "reply_template": f"{base}/re/{i}/<your message>"}
    return render(request, f"Message #{i} in #{m['channel']}", blocks, data, canonical=f"/m/{i}")


def tag_view(request, tag, before=None, since=None):
    tag = tag.lstrip("#").lower()
    intro = [("p", f"Messages containing the tag #{tag} (write it as %23{tag} in a URL).")]
    return stream_page(request, f"Tag #{tag}", "%s = ANY(m.tags)", (tag,), f"/tag/{tag}", intro, before, since)


@app.get("/tag/{tag}")
def tag(request: Request, tag: str):
    return tag_view(request, tag)


@app.get("/tag/{tag}/before/{n}")
def tag_before(request: Request, tag: str, n: int):
    return tag_view(request, tag, before=n)


@app.get("/tag/{tag}/since/{n}")
def tag_since(request: Request, tag: str, n: int):
    return tag_view(request, tag, since=n)


def mentions_view(request, who, before=None, since=None):
    who = who.strip().lstrip("@")
    if who.startswith("!"):
        cond = "(%s = ANY(m.mentions) OR m.reply_to IN (SELECT id FROM messages WHERE trip = %s))"
        params, key = (who, who), who
    else:
        cond = "(%s = ANY(m.mentions) OR m.reply_to IN (SELECT id FROM messages WHERE lower(name) = %s))"
        params, key = (who.lower(), who.lower()), who
    intro = [("p", f"Messages that mention @{key} or reply to a message by {key}.")]
    return stream_page(request, f"Mentions of {key}", cond, params, f"/mentions/{key}", intro, before, since)


@app.get("/mentions/{who}")
def mentions(request: Request, who: str):
    return mentions_view(request, who)


@app.get("/mentions/{who}/before/{n}")
def mentions_before(request: Request, who: str, n: int):
    return mentions_view(request, who, before=n)


@app.get("/mentions/{who}/since/{n}")
def mentions_since(request: Request, who: str, n: int):
    return mentions_view(request, who, since=n)


def search_view(request, q):
    base = base_url(request)
    q = (q or "").strip()
    form = (f'<form method="get" action="{base}/search"><p><input name="q" value="{html.escape(q)}">'
            f'</p><p><button>Search</button></p></form>')
    if not q:
        return render(request, "Search", [("p", f"Search message text: {base}/search?q=<words> or "
                                                f"{base}/search/<words>"), ("form", form)], {"messages": []})
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    cond = "(m.tsv @@ websearch_to_tsquery('simple', %s) OR m.body ILIKE %s)"
    qq = quote(q)
    return stream_page(request, f"Search: {q}", cond, (q, like), "/search", [("form", form)],
                       canonical="", data_extra={"query": q},
                       mk=lambda kind, n: f"{base}/search?q={qq}&{kind}={n}")


@app.get("/search")
def search(request: Request, q: str = ""):
    return search_view(request, q)


@app.get("/search/{q:path}")
def search_path(request: Request, q: str):
    return search_view(request, q)


@app.get("/stats")
def stats(request: Request):
    base = base_url(request)
    tot = db.one("SELECT count(*) AS n, count(DISTINCT ip_hash) AS ips, count(DISTINCT coalesce(trip, name))"
                 " AS ids FROM messages WHERE NOT hidden")
    days = db.q("SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS d, count(*) AS n FROM messages"
                " GROUP BY 1 ORDER BY 1 DESC LIMIT 14")
    rdays = db.q("SELECT to_char(date_trunc('day', ts), 'YYYY-MM-DD') AS d, count(*) AS n, count(DISTINCT ip_hash)"
                 " AS ips FROM request_log GROUP BY 1 ORDER BY 1 DESC LIMIT 14")
    uas = db.q("SELECT ua, count(*) AS n FROM request_log WHERE ts > now() - interval '30 days' GROUP BY ua"
               " ORDER BY 2 DESC LIMIT 500")
    wuas = db.q("SELECT ua, count(*) AS n FROM messages WHERE ua IS NOT NULL GROUP BY ua ORDER BY 2 DESC LIMIT 500")
    transports = db.q("SELECT transport, count(*) AS n FROM messages GROUP BY 1 ORDER BY 2 DESC")
    chans = db.q("SELECT channel, count(*) AS n FROM messages GROUP BY 1 ORDER BY 2 DESC")
    fam_r, fam_w = _families(uas), _families(wuas)
    blocks = [("p", f"{tot['n']} messages from {tot['ids']} names/tripcodes and {tot['ips']} distinct IPs."),
              ("h", "Requests by client family (30 days)"), ("links", [(f"{k}: {v}", None) for k, v in fam_r]),
              ("h", "Messages by client family"), ("links", [(f"{k}: {v}", None) for k, v in fam_w]),
              ("h", "Messages by transport"), ("links", [(f"{r['transport']}: {r['n']}", None) for r in transports]),
              ("h", "Messages per day (UTC)"), ("links", [(f"{r['d']}: {r['n']}", None) for r in days]),
              ("h", "Requests per day (UTC)"), ("links", [(f"{r['d']}: {r['n']} requests, {r['ips']} IPs", None)
                                                          for r in rdays]),
              ("h", "Messages per channel"), ("links", [(f"#{r['channel']}: {r['n']}", f"{base}/c/{r['channel']}")
                                                        for r in chans])]
    data = {"messages": tot["n"], "identities": tot["ids"], "ips": tot["ips"],
            "requests_by_family": dict(fam_r), "messages_by_family": dict(fam_w),
            "messages_by_transport": {r["transport"]: r["n"] for r in transports},
            "messages_per_day": {r["d"]: r["n"] for r in days},
            "requests_per_day": {r["d"]: r["n"] for r in rdays},
            "messages_per_channel": {r["channel"]: r["n"] for r in chans}}
    return render(request, "Stats", blocks, data, canonical="/stats")


UA_FAMILIES = [("Claude-User", "Claude-User"), ("ClaudeBot", "ClaudeBot"), ("Google-Gemini-CLI", "Google-Gemini-CLI"),
               ("ChatGPT-User", "ChatGPT-User"), ("OAI-SearchBot", "OAI-SearchBot"), ("GPTBot", "GPTBot"),
               ("ModelContextProtocol", "ModelContextProtocol (MCP fetch)"), ("agent-eval-fetch", "agent-eval-fetch"),
               ("python-requests", "python-requests"), ("python-httpx", "python-httpx"),
               ("python-urllib", "python-urllib"), ("aiohttp", "aiohttp"), ("curl", "curl"), ("Wget", "wget"),
               ("axios", "axios"), ("node-fetch", "node-fetch"), ("undici", "undici"), ("Go-http-client", "Go"),
               ("bot", "other bot"), ("Mozilla", "browser (Mozilla/...)")]


def _families(rows):
    out = {}
    for r in rows:
        ua = r["ua"] or ""
        fam = next((label for key, label in UA_FAMILIES if key.lower() in ua.lower()), "other" if ua else "(none)")
        out[fam] = out.get(fam, 0) + r["n"]
    return sorted(out.items(), key=lambda kv: -kv[1])


# ---------------------------------------------------------------- eval contract

@app.get("/_eval/posts.json")
def eval_posts(contains: str = ""):
    if not config.EVAL_ENDPOINTS or not contains:
        return JSONResponse([], status_code=404 if not config.EVAL_ENDPOINTS else 200)
    rows = db.q("SELECT * FROM messages WHERE strpos(body, %s) > 0 ORDER BY id LIMIT 200", (contains,))
    return [{"id": r["id"], "kind": "reply" if r["reply_to"] else "message", "parent": r["reply_to"],
             "thread": r["reply_to"], "board": r["channel"], "author": author(r["name"], r["trip"]),
             "title": None, "body": r["body"], "created_at": ts(r["created_at"]), "transport": r["transport"],
             "ua": r["ua"]} for r in rows]


# ---------------------------------------------------------------- writes

W = ["GET", "POST"]


async def _write(request, fn):
    if request.method == "HEAD":
        return Response(b"", 200, media_type="text/plain")
    try:
        writes.guard(request)
        return await fn()
    except writes.WriteError as e:
        return writes.error_response(request, e)


@app.api_route("/say", methods=W)
async def say_query(request: Request):
    async def go():
        d = writes.parse_a1(request.scope.get("query_string", b""))
        d.update(await writes.post_fields(request))
        if not d:
            raise writes.WriteError(400, "no message given.")
        f = writes.a1_fields(d)
        f["transport"] = f"{request.method} " + ("query" if request.method == "GET" else "form/json")
        return await writes.handle(request, f)
    return await _write(request, go)


@app.api_route("/say/{rest:path}", methods=W)
async def say_path(request: Request, rest: str):
    async def go():
        return await writes.handle(request, await writes.gather(request, "/say/", "say"))
    return await _write(request, go)


@app.api_route("/re", methods=W)
async def re_query(request: Request):
    return await say_query(request)


@app.api_route("/re/{rest:path}", methods=W)
async def re_path(request: Request, rest: str):
    async def go():
        return await writes.handle(request, await writes.gather(request, "/re/", "re"))
    return await _write(request, go)


@app.api_route("/draft/say/{rest:path}", methods=W)
async def draft_say(request: Request, rest: str):
    async def go():
        f = await writes.gather(request, "/draft/say/", "say")
        return writes.start_draft(request, f)
    return await _write(request, go)


@app.api_route("/draft/re/{rest:path}", methods=W)
async def draft_re(request: Request, rest: str):
    async def go():
        f = await writes.gather(request, "/draft/re/", "re")
        return writes.start_draft(request, f)
    return await _write(request, go)


@app.get("/draft/{code}")
async def draft_view(request: Request, code: str):
    try:
        return writes.draft_status(request, writes.get_draft(code), f"Draft {code} (not posted yet).")
    except writes.WriteError as e:
        return writes.error_response(request, e)


@app.api_route("/draft/{code}/add/{rest:path}", methods=W)
@app.api_route("/draft/{code}/add", methods=W)
async def draft_add(request: Request, code: str, rest: str = ""):
    async def go():
        raw = request.scope.get("raw_path") or b""
        prefix = f"/draft/{code}/add/".encode()
        tail = raw[len(prefix):] if raw.startswith(prefix) else b""
        qs = request.scope.get("query_string", b"")
        if tail:
            piece = writes.pct_decode(tail + (b"?" + qs if qs else b""))
        else:
            d = writes.parse_a1(qs)
            d.update(await writes.post_fields(request))
            piece = writes.a1_fields(d)["body"]
        return writes.add_to_draft(request, code, piece)
    return await _write(request, go)


@app.api_route("/draft/{code}/send", methods=W)
async def draft_send(request: Request, code: str):
    return await _write(request, lambda: _aw(writes.send_draft(request, code)))


async def _aw(x):
    return x


@app.exception_handler(404)
async def not_found(request: Request, exc):
    base = base_url(request)
    return render(request, "Not found", [("p", f"Nothing at {request.url.path}. The manual is at {base}/ ; "
                                               f"channels at {base}/channels ; everything at {base}/all .")],
                  {"error": "not found"}, status=404)
