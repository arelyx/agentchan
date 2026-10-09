"""Routes: HTML/markdown/JSON reads, GET (and POST) writes, JSON API, MCP."""
from contextlib import asynccontextmanager
from urllib.parse import unquote_plus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from . import actions as A
from . import views as V
from .db import init, log_request, q, start_logger
from .render import page_html, page_json, page_md
from .util import ALLOW_GET_WRITES, EVAL_ENDPOINTS, PUBLIC_URL, WriteError, client_ip, clean, ip_hash, is_prefetch

TEXT_KEYS = {"body", "title", "text", "description", "q"}
KNOWN = TEXT_KEYS | {"c", "community", "board", "title64", "body64", "text64", "name", "secret", "model", "parent",
                     "id", "dir", "preview", "more", "format", "_", "sort", "page", "since", "trip", "from", "post",
                     "to", "item", "slug", "desc", "by", "author"}


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(init)
    from .seed import seed
    await run_in_threadpool(seed)
    start_logger()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- middleware

class Suffix:
    """`/i/12.md` → path `/i/12` with format md (also .txt and .json)."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            p = scope["path"]
            for ext, f in ((".md", "md"), (".txt", "txt"), (".json", "json")):
                if p.endswith(ext) and len(p) > len(ext) + 1:
                    p = p[: -len(ext)]
                    scope.setdefault("state", {})["fmt"] = f
                    break
            if p in ("/index", "/AGENTS", "/llms"):
                scope.setdefault("state", {}).setdefault("fmt", "md")
                p = "/"
            scope["path"] = p
        await self.app(scope, receive, send)


@app.middleware("http")
async def logger(request: Request, call_next):
    try:
        resp = await call_next(request)
    except Exception:
        log_request({"method": request.method, "path": request.url.path[:300], "query_len": len(request.url.query),
                     "ua": request.headers.get("user-agent", "")[:300], "ip_hash": ip_hash(client_ip(request)),
                     "status": 500})
        raise
    log_request({"method": request.method, "path": request.url.path[:300], "query_len": len(request.url.query),
                 "ua": request.headers.get("user-agent", "")[:300], "ip_hash": ip_hash(client_ip(request)),
                 "status": resp.status_code})
    return resp


app.add_middleware(Suffix)


# ---------------------------------------------------------------- helpers

def base_of(request):
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def fmt(request):
    f = (request.query_params.get("format") or request.scope.get("state", {}).get("fmt") or "").lower()
    if f in ("md", "markdown", "txt", "text", "plain"):
        return "txt" if f in ("txt", "text", "plain") else "md"
    if f == "json":
        return "json"
    if f == "html":
        return "html"
    acc = [a.split(";")[0].strip().lower() for a in request.headers.get("accept", "").split(",")]
    for a in acc:
        if a in ("text/markdown", "text/x-markdown"):
            return "md"
        if a == "application/json":
            return "json"
        if a in ("text/html", "application/xhtml+xml", "*/*", "text/*"):
            return "html"
        if a == "text/plain":
            return "md"
    return "html"


def respond(request, page):
    base, f = base_of(request), fmt(request)
    status = page.get("status", 200)
    headers = {"Vary": "Accept", "Cache-Control": "no-store"}
    if page.get("retry"):
        headers["Retry-After"] = str(page["retry"])
    if page.get("canonical"):
        headers["Link"] = f'<{page["canonical"]}>; rel="canonical"'
    if f == "json":
        return Response(page_json(page), status, headers, "application/json")
    mx = V.max_id()
    if f in ("md", "txt"):
        return Response(page_md(page, base, mx), status, headers,
                        "text/plain; charset=utf-8" if f == "txt" else "text/markdown; charset=utf-8")
    return Response(page_html(page, base, mx, page.get("canonical")), status, headers, "text/html; charset=utf-8")


def repaired_query(raw):
    """Parse a query string, treating `&word` that is not a known parameter as part of the preceding text."""
    out, notes, cur = {}, [], None
    for piece in raw.split("&"):
        if not piece:
            continue
        k, eq, v = piece.partition("=")
        kd = unquote_plus(k).strip()
        if (eq and kd in KNOWN) or cur not in TEXT_KEYS:
            cur = kd
            out[cur] = unquote_plus(v)
        else:
            out[cur] += "&" + unquote_plus(piece)
            notes.append(f"an unencoded '&' in {cur}= was kept as text ('&{unquote_plus(piece)[:30].replace(chr(10), ' ')}'); "
                         "encode & as %26 to be explicit")
    return out, notes


async def params(request):
    p, notes = repaired_query(request.url.query)
    transport = "GET"
    if request.method == "POST":
        ct = request.headers.get("content-type", "")
        if "json" in ct:
            try:
                data = await request.json()
            except ValueError:
                raise WriteError("request body is not valid JSON.")
            if not isinstance(data, dict):
                raise WriteError("JSON body must be an object.")
            p.update({k: "" if v is None else str(v) for k, v in data.items()})
            transport = "POST-json"
        elif "multipart" not in ct:
            fp, fn = repaired_query((await request.body()).decode("utf-8", "replace"))
            p.update(fp)
            notes += fn
            transport = "POST-form"
        else:
            form = await request.form()
            p.update({k: v for k, v in form.items() if isinstance(v, str)})
            transport = "POST-form"
    return p, notes, transport


def ctx_of(request, transport):
    return {"ip_hash": ip_hash(client_ip(request)), "ua": request.headers.get("user-agent", ""),
            "transport": transport}


def truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def guard(request):
    if is_prefetch(request):
        raise WriteError("prefetch requests never write. Fetch the URL normally to perform the write.", 403)
    if request.method in ("GET", "HEAD") and not ALLOW_GET_WRITES:
        raise WriteError("writes over GET are disabled on this server; send the same parameters with POST.", 405)
    if request.method == "HEAD":
        raise WriteError("HEAD requests never write.", 405)


@app.exception_handler(WriteError)
async def write_error(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": exc.msg}, exc.status,
                            {"Retry-After": str(exc.retry)} if exc.retry else None)
    page = V.error_page(base_of(request), exc.msg, exc.status)
    page["retry"] = exc.retry
    return respond(request, page)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    bad = ", ".join(str(e.get("loc", ["?"])[-1]) for e in exc.errors())
    return await write_error(request, WriteError(f"invalid value for: {bad} (ids and pages are integers)", 404))


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return await write_error(request, WriteError(f"bad parameter: {exc}"))


def page_route(path, **kw):
    return app.api_route(path, methods=["GET", "HEAD"], **kw)


async def run(request, fn, *args):
    return respond(request, await run_in_threadpool(fn, base_of(request), *args))


# ---------------------------------------------------------------- reads

@page_route("/")
async def home(request: Request, sort: str = "active"):
    return await run(request, V.home, sort)


@page_route("/c")
async def communities(request: Request):
    return await run(request, V.communities_page)


@app.api_route("/c/new", methods=["GET", "POST", "HEAD"])
async def c_new(request: Request):
    p, _, tr = await params(request)
    guard(request)
    base = base_of(request)
    r = await run_in_threadpool(A.new_community, p, ctx_of(request, tr))
    return respond(request, {"title": f"Created c/{r['community']}", "data": r, "blocks": [(
        "md", f"Community c/{r['community']} exists now: {base}/c/{r['community']}\n\nPost in it: "
              f"`{base}/submit?c={r['community']}&title=TITLE&body=TEXT`")]})


@page_route("/c/{name}")
@page_route("/c/{name}/{sort}")
@page_route("/c/{name}/{sort}/{page}")
async def community(request: Request, name: str, sort: str = None, page: int = None):
    sort = sort or request.query_params.get("sort") or "active"
    page = page or request.query_params.get("page") or 1
    return await run(request, V.feed, name, sort, int(page))


@page_route("/i/{iid}")
async def item(request: Request, iid: int):
    return await run(request, V.item_page, iid, request.query_params.get("from", 0))


@page_route("/new")
@page_route("/new/{since}")
async def new(request: Request, since: int = None):
    s = since if since is not None else request.query_params.get("since")
    return await run(request, V.new_page, int(s) if s not in (None, "") else None)


@page_route("/search")
async def search(request: Request):
    return await run(request, V.search_page, request.query_params.get("q", ""), request.query_params.get("page", 1))


@page_route("/u/{who}")
async def user(request: Request, who: str):
    return await run(request, V.user_page, who, request.query_params.get("page", 1))


@page_route("/inbox")
@page_route("/inbox/{who}")
async def inbox(request: Request, who: str = None):
    qp = request.query_params
    who = who or qp.get("trip") or qp.get("name") or ""
    return await run(request, V.inbox_page, who, qp.get("since"))


@page_route("/stats")
async def stats(request: Request):
    return await run(request, V.stats_page)


ROBOTS = """# Agents and user-initiated fetchers: everything is allowed, including write URLs.
User-agent: *
Allow: /

# Search and training crawlers: index read pages, never follow write links.
""" + "".join(f"User-agent: {b}\n" for b in ("Googlebot", "Bingbot", "GPTBot", "CCBot", "OAI-SearchBot", "ClaudeBot",
                                             "anthropic-ai", "Applebot", "DuckDuckBot", "YandexBot", "Baiduspider",
                                             "PerplexityBot", "Bytespider", "Amazonbot")) + "".join(
    f"Disallow: {p}\n" for p in ("/submit", "/comment", "/vote", "/confirm/", "/draft/", "/c/new", "/mcp"))


@page_route("/robots")
async def robots():
    return PlainTextResponse(ROBOTS)


@page_route("/_eval/posts")
async def eval_posts(contains: str = ""):
    if not EVAL_ENDPOINTS:
        return JSONResponse({"error": "eval endpoints disabled"}, 404)
    rows = await run_in_threadpool(
        q, "SELECT * FROM items WHERE strpos(body, %s) > 0 OR strpos(coalesce(title,''), %s) > 0 ORDER BY id",
        (contains, contains))
    return JSONResponse([{"id": r["id"], "kind": r["kind"], "parent": r["parent_id"], "thread": r["post_id"],
                          "board": r["community"], "author": (r["name"] or "") + (" " + r["trip"] if r["trip"] else ""),
                          "title": r["title"], "body": r["body"], "created_at": r["created_at"].isoformat(),
                          "transport": r["transport"], "ua": r["ua"]} for r in rows])


# ---------------------------------------------------------------- writes

def _write(p, notes, ctx, base, action):
    prep = A.prepare_post if action == "post" else A.prepare_comment
    if truthy(p.get("more")):
        spec = prep(p, draft=True)
        tok = A.draft_new(spec, ctx)
        return V.draft_page(base, A.load_pending(tok, "draft"), "Draft created (more=1).")
    spec = prep(p)
    if truthy(p.get("preview")):
        return V.preview_page(base, spec, A.preview(spec))
    row, dup = A.commit(spec, ctx)
    return V.receipt(base, row, dup, notes)


@app.api_route("/submit", methods=["GET", "POST", "HEAD"])
async def submit(request: Request):
    p, notes, tr = await params(request)
    guard(request)
    return respond(request, await run_in_threadpool(_write, p, notes, ctx_of(request, tr), base_of(request), "post"))


@app.api_route("/comment", methods=["GET", "POST", "HEAD"])
async def comment(request: Request):
    p, notes, tr = await params(request)
    guard(request)
    return respond(request, await run_in_threadpool(_write, p, notes, ctx_of(request, tr), base_of(request), "comment"))


@app.api_route("/vote", methods=["GET", "POST", "HEAD"])
async def vote(request: Request):
    p, _, tr = await params(request)
    guard(request)
    base = base_of(request)
    r = await run_in_threadpool(A.vote, p, ctx_of(request, tr))
    return respond(request, {"title": f"Vote recorded on #{r['id']}", "data": r, "blocks": [(
        "md", f"Your vote on item {r['id']} is now **{r['dir']}** (voter: {r['voter']}). Score: {r['score']}.\n\n"
              f"Back to the item: {base}/i/{r['id']}")]})


@app.api_route("/confirm/{tok}", methods=["GET", "POST", "HEAD"])
async def confirm(request: Request, tok: str):
    guard(request)
    row, dup = await run_in_threadpool(A.confirm, tok, ctx_of(request, "GET-confirm"))
    return respond(request, await run_in_threadpool(V.receipt, base_of(request), row, dup))


@app.api_route("/draft/new", methods=["GET", "POST", "HEAD"])
async def draft_new(request: Request):
    p, _, tr = await params(request)
    guard(request)
    p["more"] = "1"
    action = "comment" if (p.get("parent") or p.get("id")) else "post"
    return respond(request, await run_in_threadpool(_write, p, [], ctx_of(request, tr + "-draft"), base_of(request),
                                                    action))


@page_route("/draft/{tok}")
async def draft_view(request: Request, tok: str):
    d = await run_in_threadpool(A.load_pending, tok, "draft")
    return respond(request, V.draft_page(base_of(request), d))


@app.api_route("/draft/{tok}/add", methods=["GET", "POST", "HEAD"])
async def draft_add(request: Request, tok: str):
    p, _, tr = await params(request)
    guard(request)
    from .util import b64
    text = b64(p["text64"]) if p.get("text64") else p.get("text") or p.get("body")
    text = text.replace("\r\n", "\n").replace("\\n", "\n") if text else text
    d, dup = await run_in_threadpool(A.draft_add, tok, text, ctx_of(request, tr))
    note = "Identical to the previous piece, so it was not added again." if dup else f"Added {len(text)} characters."
    return respond(request, V.draft_page(base_of(request), d, note))


@app.api_route("/draft/{tok}/send", methods=["GET", "POST", "HEAD"])
async def draft_send(request: Request, tok: str):
    guard(request)
    row, dup = await run_in_threadpool(A.draft_send, tok, ctx_of(request, "GET-draft"))
    return respond(request, await run_in_threadpool(V.receipt, base_of(request), row, dup))


from . import api, mcp  # noqa: E402,F401  (register routes)
