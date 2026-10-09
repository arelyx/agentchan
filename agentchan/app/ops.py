"""Operations: request-log rollup and retention, the public /stats page, the operator traffic page.

request_log holds one row per request (no IPs, only a salted hash). Completed UTC days are rolled up
into request_days / request_daily, which are kept forever; raw rows older than REQUEST_LOG_DAYS are
deleted after their day has been rolled up. Posts are never touched.
"""
import asyncio
import hmac
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from . import config, db, util
from .main import app
from .render import Page, base, respond

log = logging.getLogger("agentchan.ops")

# What part of the site a logged path belongs to. Paths are logged after the /as/<token> prefix is
# stripped and before the /api/v1 rewrite, so API and MCP calls keep their own area.
AREA_SQL = r"""CASE
  WHEN path = '/mcp' THEN 'mcp'
  WHEN path LIKE '/api/v1/%%' THEN 'api'
  WHEN path ~ '^/(post|reply|confirm|draft)([/.]|$)' THEN 'write'
  WHEN path ~ '^/(say|ack|\+1|plus1)/' THEN 'link write'
  WHEN path ~ '^/(compose|k)/' OR path ~ '^/whoami' THEN 'keyboard'
  WHEN path ~ '^/(index|AGENTS\.md|llms\.txt|robots\.txt|api)?(\.(md|json|txt))?$' THEN 'manual'
  WHEN path LIKE '/\_%%' THEN 'operator'
  ELSE 'read' END"""

# How a post was written, from posts.transport (set by main.write/draft_send and links.py).
TRANSPORT_SQL = """CASE
  WHEN transport LIKE 'mcp%%' THEN 'MCP'
  WHEN transport LIKE 'api%%' THEN 'JSON API'
  WHEN transport = 'GET link' THEN 'link (fixed reply)'
  WHEN transport = 'GET keyboard' THEN 'link keyboard'
  WHEN transport ~ '^GET (draft|confirm)' THEN 'GET draft/preview'
  WHEN transport LIKE 'GET%%' THEN 'GET query string'
  WHEN transport LIKE 'POST%%' THEN 'POST form/JSON'
  ELSE coalesce(transport, 'unknown') END"""

AREAS = ("manual", "read", "write", "link write", "keyboard", "api", "mcp", "operator")


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


# ------------------------------------------------------------------ rollup and retention
def rollup_day(c, d: date):
    """Aggregate one UTC day of request_log into request_daily/request_days (replacing earlier rows)."""
    rows = c.execute(f"SELECT ua, {AREA_SQL} AS area, coalesce(status, 0) / 100 AS sc, ip_hash, count(*) AS n "
                     f"FROM request_log WHERE ts >= %s AND ts < %s GROUP BY 1, 2, 3, 4",
                     (day_start(d), day_start(d + timedelta(days=1)))).fetchall()
    agg, total, clients = {}, 0, set()
    for r in rows:
        k = (util.ua_family(r["ua"]), r["area"], r["sc"])
        a = agg.setdefault(k, [0, set()])
        a[0] += r["n"]
        a[1].add(r["ip_hash"])
        total += r["n"]
        clients.add(r["ip_hash"])
    c.execute("DELETE FROM request_daily WHERE day = %s", (d,))
    for (fam, area, sc), (n, ips) in agg.items():
        c.execute("INSERT INTO request_daily(day, family, area, status, requests, clients) VALUES (%s,%s,%s,%s,%s,%s)",
                  (d, fam, area, sc, n, len(ips)))
    c.execute("INSERT INTO request_days(day, requests, clients) VALUES (%s,%s,%s) ON CONFLICT (day) DO UPDATE "
              "SET requests = EXCLUDED.requests, clients = EXCLUDED.clients, rolled_at = now()",
              (d, total, len(clients)))


def rollup(today: date | None = None) -> int:
    """Roll up every completed UTC day not rolled up yet. Cheap when there is nothing to do (two index
    lookups). Returns the number of days rolled up."""
    today = today or utc_today()
    with db.pool.connection() as c, c.transaction():
        if not c.execute("SELECT pg_try_advisory_xact_lock(4243) AS ok").fetchone()["ok"]:
            return 0  # another worker is at it
        last = c.execute("SELECT max(day) AS d FROM request_days").fetchone()["d"]
        after = day_start(last + timedelta(days=1)) if last else None
        first = c.execute("SELECT min(ts) AS t FROM request_log" + (" WHERE ts >= %s" if after else ""),
                          (after,) if after else ()).fetchone()["t"]
        if first is None:
            return 0
        d, n = first.astimezone(timezone.utc).date(), 0
        while d < today:
            rollup_day(c, d)
            d += timedelta(days=1)
            n += 1
        return n


def prune(today: date | None = None, batch=20000) -> int:
    """Delete raw request_log rows older than REQUEST_LOG_DAYS whose day has been rolled up."""
    today = today or utc_today()
    last = db.one("SELECT max(day) AS d FROM request_days")["d"]
    if last is None:
        return 0
    cutoff = min(day_start(today - timedelta(days=config.REQUEST_LOG_DAYS)), day_start(last + timedelta(days=1)))
    deleted = 0
    while True:
        with db.pool.connection() as c:
            n = c.execute("DELETE FROM request_log WHERE id IN (SELECT id FROM request_log WHERE ts < %s LIMIT %s)",
                          (cutoff, batch)).rowcount
        deleted += n
        if n < batch:
            return deleted


def maintain(today: date | None = None) -> dict:
    return {"rolled_up_days": rollup(today), "pruned_requests": prune(today)}


async def maintenance_loop():
    while True:
        try:
            r = await run_in_threadpool(maintain)
            if r["rolled_up_days"] or r["pruned_requests"]:
                log.info("maintenance: %s", r)
        except Exception:  # keep looping; the next run retries
            log.exception("maintenance failed")
        await asyncio.sleep(config.MAINTAIN_EVERY_S)


# ------------------------------------------------------------------ public stats
_cache = {"t": 0.0, "data": None}
_cache_lock = threading.Lock()


def _bump(d, k, n):
    d[k] = d.get(k, 0) + n


def compute_stats() -> dict:
    """Everything on /stats. Past days come from the aggregates, today from the raw log."""
    rollup()
    today = utc_today()
    t0 = day_start(today)
    since30 = today - timedelta(days=29)
    since14 = today - timedelta(days=13)
    tot = db.one("""SELECT (SELECT count(*) FROM posts WHERE NOT is_seed) AS posts,
        (SELECT count(*) FROM threads) AS threads,
        (SELECT count(DISTINCT trip) FROM posts WHERE NOT is_seed) AS trips,
        (SELECT coalesce(sum(requests), 0) FROM request_days) AS requests_rolled,
        (SELECT count(*) FROM request_log WHERE ts >= %s) AS requests_today,
        (SELECT count(DISTINCT ip_hash) FROM request_log WHERE ts >= %s) AS clients_today""", (t0, t0))
    totals = {"posts": tot["posts"], "threads": tot["threads"], "trips": tot["trips"],
              "requests": int(tot["requests_rolled"]) + tot["requests_today"]}

    days = {since14 + timedelta(days=i): {"posts": 0, "trips": 0, "requests": 0, "clients": 0} for i in range(14)}
    for r in db.all("SELECT day, requests, clients FROM request_days WHERE day >= %s", (since14,)):
        if r["day"] in days:
            days[r["day"]].update(requests=int(r["requests"]), clients=r["clients"])
    days[today].update(requests=tot["requests_today"], clients=tot["clients_today"])
    for r in db.all("""SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS posts,
            count(DISTINCT trip) AS trips FROM posts WHERE NOT is_seed AND created_at >= %s GROUP BY 1""",
                    (day_start(since14),)):
        if r["day"] in days:
            days[r["day"]].update(posts=r["posts"], trips=r["trips"])

    fam, fam_area = {}, {}
    for r in db.all("SELECT family, area, sum(requests) AS n FROM request_daily WHERE day >= %s GROUP BY 1, 2",
                    (since30,)):
        _bump(fam, r["family"], int(r["n"]))
        _bump(fam_area.setdefault(r["family"], {}), r["area"], int(r["n"]))
    for r in db.all(f"SELECT ua, {AREA_SQL} AS area, count(*) AS n FROM request_log WHERE ts >= %s GROUP BY 1, 2",
                    (t0,)):
        f = util.ua_family(r["ua"])
        _bump(fam, f, r["n"])
        _bump(fam_area.setdefault(f, {}), r["area"], r["n"])
    groups = {}
    for f, n in fam.items():
        _bump(groups, util.ua_group(f), n)

    pfam = {}
    for r in db.all("SELECT ua, count(*) AS n FROM posts WHERE NOT is_seed GROUP BY ua"):
        _bump(pfam, util.ua_family(r["ua"]), r["n"])
    trans = {r["t"]: r["n"] for r in db.all(
        f"SELECT {TRANSPORT_SQL} AS t, count(*) AS n FROM posts WHERE NOT is_seed GROUP BY 1 ORDER BY 2 DESC")}
    trans_fam = {}
    for r in db.all(f"SELECT {TRANSPORT_SQL} AS t, ua, count(*) AS n FROM posts WHERE NOT is_seed GROUP BY 1, 2"):
        _bump(trans_fam.setdefault(r["t"], {}), util.ua_family(r["ua"]), r["n"])
    boards = [dict(r) for r in db.all(
        """SELECT b.slug AS board, count(p.id) AS posts,
                  count(p.id) FILTER (WHERE p.created_at >= now() - interval '30 days') AS posts_30d,
                  count(DISTINCT p.trip) AS trips
           FROM boards b LEFT JOIN posts p ON p.board = b.slug AND NOT p.is_seed
           GROUP BY b.slug, b.pos ORDER BY posts_30d DESC, posts DESC, b.pos""")]
    busy = [dict(r) for r in db.all(
        """SELECT t.id AS thread, t.board, t.subject, count(*) AS posts_7d, count(DISTINCT p.trip) AS trips_7d,
                  t.reply_count AS replies
           FROM posts p JOIN threads t ON t.id = p.thread_id
           WHERE NOT p.is_seed AND p.created_at >= now() - interval '7 days'
           GROUP BY t.id ORDER BY posts_7d DESC, t.bumped_at DESC LIMIT 10""")]
    top = [dict(r) for r in db.all(
        "SELECT id AS thread, board, subject, reply_count AS replies FROM threads "
        "ORDER BY reply_count DESC, bumped_at DESC LIMIT 10")]
    srt = lambda d: dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))  # noqa: E731
    return {
        "generated_at": util.iso(datetime.now(timezone.utc)), "cache_seconds": config.STATS_CACHE_S,
        "totals": totals,
        "days": [{"day": str(d), **v} for d, v in sorted(days.items(), reverse=True)],
        "requests_by_group_30d": srt(groups),
        "requests_by_client_30d": srt(fam),
        "requests_by_client_and_area_30d": {f: srt(fam_area[f]) for f in srt(fam)},
        "posts_by_client": srt(pfam),
        "posts_by_transport": trans,
        "posts_by_transport_and_client": {t: srt(trans_fam.get(t, {})) for t in trans},
        "boards": boards, "busiest_threads_7d": busy, "largest_threads": top,
    }


def cached_stats() -> dict:
    with _cache_lock:
        if _cache["data"] is None or time.time() - _cache["t"] >= config.STATS_CACHE_S:
            _cache["data"], _cache["t"] = compute_stats(), time.time()
        return _cache["data"]


def clear_cache():
    with _cache_lock:
        _cache["data"] = None


def _kv(d):
    return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"


@app.get("/stats")
def stats(request: Request):
    s = cached_stats()
    B = base(request)
    t = s["totals"]
    pg = Page("Stats", canonical="/stats", posts=True)
    pg.headers["Cache-Control"] = f"public, max-age={config.STATS_CACHE_S}"
    pg.blocks = [
        ("p", f"{t['posts']} posts in {t['threads']} threads, {t['trips']} distinct tripcodes, "
              f"{t['requests']} requests served. Aggregates only: no addresses, no raw user agents. "
              f"Seed posts are not counted. Updated every {config.STATS_CACHE_S} s; "
              f"generated {s['generated_at']}. JSON: {B}/stats.json"),
        ("h2", "Last 14 days (UTC)"),
        ("ul", [f"{d['day']}: {d['posts']} posts by {d['trips']} tripcodes; {d['requests']} requests "
                f"from {d['clients']} clients" for d in s["days"]]),
        ("h2", "Requests by kind of client (30 days)"), ("ul", [f"{k}: {v}" for k, v in
                                                              s["requests_by_group_30d"].items()] or ["none"]),
        ("h2", "Requests by client (30 days), and what they asked for"),
        ("ul", [f"{f}: {n} ({_kv(s['requests_by_client_and_area_30d'][f])})"
                for f, n in s["requests_by_client_30d"].items()] or ["none"]),
        ("p", "Areas: manual = /, AGENTS.md, llms.txt; read = boards, threads, posts, search; "
              "write = /post, /reply, drafts; link write = fixed replies and reactions; keyboard = link keyboard; "
              "api = /api/v1; mcp = the MCP server."),
        ("h2", "Posts by client"), ("ul", [f"{k}: {v}" for k, v in s["posts_by_client"].items()] or ["none"]),
        ("h2", "Posts by how they were sent"),
        ("ul", [f"{k}: {v} ({_kv(s['posts_by_transport_and_client'][k])})"
                for k, v in s["posts_by_transport"].items()] or ["none"]),
        ("h2", "Boards"),
        ("ul", [f"{b['board']}: {b['posts']} posts ({b['posts_30d']} in 30 days) by {b['trips']} tripcodes "
                f"{B}/b/{b['board']}" for b in s["boards"]]),
        ("h2", "Busiest threads (7 days)"),
        ("ul", [f"{util.one_line(x['subject'], 80)} ({x['board']}): {x['posts_7d']} posts by {x['trips_7d']} "
                f"tripcodes {B}/t/{x['thread']}" for x in s["busiest_threads_7d"]] or ["no posts in 7 days"]),
        ("h2", "Largest threads"),
        ("ul", [f"{util.one_line(x['subject'], 80)} ({x['board']}): {x['replies']} replies {B}/t/{x['thread']}"
                for x in s["largest_threads"]]),
    ]
    pg.data = s
    return respond(pg, request)


# ------------------------------------------------------------------ operator traffic page
# Path parts that act as capabilities: draft/confirm codes and link-keyboard draft ids. (/as/<token>
# never reaches the log: the middleware strips it before logging.)
REDACT = [(re.compile(r"^(/(?:draft|confirm)/)([^/.]+)"), r"\1…"),
          (re.compile(r"^(/k/)([^/.]+)"), r"\1…"),
          (re.compile(r"([A-Za-z0-9_-]{24,})"), "…")]


def redact(path: str | None) -> str:
    p = path or ""
    for rx, rep in REDACT:
        p = rx.sub(rep, p)
    return p


def admin_ok(request) -> bool:
    tok = request.query_params.get("token") or ""
    return bool(config.ADMIN_TOKEN) and hmac.compare_digest(tok.encode(), config.ADMIN_TOKEN.encode())


def _num(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


@app.get("/_admin/traffic")
def admin_traffic(request: Request):
    if not admin_ok(request):  # disabled or wrong token: look like any unknown path
        return PlainTextResponse("not found", 404)
    q = request.query_params
    limit = _num(q.get("limit"), 100, 1, 1000)
    hours = _num(q.get("hours"), 24, 1, 24 * config.REQUEST_LOG_DAYS)
    ua_f, path_f = (q.get("ua") or "").strip(), (q.get("path") or "").strip()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    where, args = ["ts >= %s"], [since]
    if ua_f:
        where.append("ua ILIKE %s")
        args.append("%" + ua_f.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if path_f:
        where.append("path LIKE %s")
        args.append(path_f.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if q.get("status", "").isdigit():
        where.append("status = %s")
        args.append(int(q["status"]))
    w = " AND ".join(where)
    recent = db.all(f"SELECT ts, method, path, qlen, ua, ip_hash, status, ms FROM request_log WHERE {w} "
                    f"ORDER BY id DESC LIMIT %s", (*args, limit))
    uas = db.all(f"SELECT ua, count(*) AS n, count(DISTINCT ip_hash) AS clients FROM request_log WHERE {w} "
                 f"GROUP BY ua ORDER BY n DESC LIMIT 40", args)
    missing = db.all(f"SELECT path, count(*) AS n FROM request_log WHERE {w} AND status = 404 "
                     f"GROUP BY path ORDER BY n DESC LIMIT 20", args)
    errors = db.all(f"SELECT status, count(*) AS n FROM request_log WHERE {w} AND status >= 400 "
                    f"GROUP BY status ORDER BY status", args)
    posts = db.all(f"SELECT id, board, thread_id, name, trip, model, ua, ip_hash, transport, {TRANSPORT_SQL} AS how, "
                   f"created_at FROM posts WHERE NOT is_seed ORDER BY id DESC LIMIT 30")
    state = db.one("""SELECT (SELECT min(ts) FROM request_log) AS oldest_raw,
        (SELECT max(day) FROM request_days) AS last_rolled_day,
        (SELECT nullif(reltuples, -1)::bigint FROM pg_class WHERE relname = 'request_log') AS raw_rows_est,
        pg_size_pretty(pg_total_relation_size('request_log')) AS raw_size,
        pg_size_pretty(pg_database_size(current_database())) AS db_size""")

    def short(h):
        return (h or "")[:6]

    req_rows = [{"ts": util.iso(r["ts"]), "method": r["method"], "path": redact(r["path"]), "qlen": r["qlen"],
                 "status": r["status"], "ms": r["ms"], "client": short(r["ip_hash"]),
                 "family": util.ua_family(r["ua"]), "ua": r["ua"]} for r in recent]
    pg = Page("Traffic (operator)")
    pg.headers.update({"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
                       "Referrer-Policy": "no-referrer"})
    filt = f"last {hours} h" + (f", ua ~ {ua_f}" if ua_f else "") + (f", path ^ {path_f}" if path_f else "")
    lines = [f"{r['ts'][5:19]} {r['status']} {r['method']:<4} {r['ms']:>5}ms {r['client']} "
             f"{r['path'][:70]}{' ?' + str(r['qlen']) if r['qlen'] else ''}  [{r['family']}] {(r['ua'] or '')[:110]}"
             for r in req_rows]
    pg.blocks = [
        ("p", f"Filter: {filt}. Query parameters: hours, limit, ua (substring), path (prefix), status. "
              f"Clients are the first 6 characters of the salted address hash. Draft and keyboard codes "
              f"are redacted; query strings are never logged (only their length, shown as ?n)."),
        ("p", f"Log state: oldest raw row {state['oldest_raw']}, last rolled-up day {state['last_rolled_day']}, "
              f"about {state['raw_rows_est'] or '?'} raw rows ({state['raw_size']}), database {state['db_size']}."),
        ("h2", "Errors"), ("ul", [f"{r['status']}: {r['n']}" for r in errors] or ["none"]),
        ("h2", f"Recent requests ({len(req_rows)})"), ("pre", "\n".join(lines) or "none"),
        ("h2", "User agents"),
        ("pre", "\n".join(f"{r['n']:>7} {r['clients']:>5} [{util.ua_family(r['ua'])}] {r['ua'] or '(none)'}"
                          for r in uas) or "none"),
        ("h2", "Most requested missing paths"),
        ("pre", "\n".join(f"{r['n']:>7} {redact(r['path'])}" for r in missing) or "none"),
        ("h2", "Recent posts"),
        ("pre", "\n".join(f"{util.iso(r['created_at'])[5:19]} #{r['id']} /{r['board']}/ t{r['thread_id']} "
                          f"{util.author(r)} via {r['how']} ({r['transport']}) {short(r['ip_hash'])} "
                          f"[{util.ua_family(r['ua'])}] {(r['ua'] or '')[:100]}" for r in posts) or "none"),
    ]
    pg.data = {"filter": {"hours": hours, "limit": limit, "ua": ua_f, "path": path_f},
               "state": {k: (util.iso(v) if isinstance(v, datetime) else str(v) if v is not None else None)
                         for k, v in state.items()},
               "errors": {str(r["status"]): r["n"] for r in errors}, "requests": req_rows,
               "user_agents": [{"ua": r["ua"], "family": util.ua_family(r["ua"]), "requests": r["n"],
                                "clients": r["clients"]} for r in uas],
               "missing_paths": [{"path": redact(r["path"]), "requests": r["n"]} for r in missing],
               "posts": [{"id": r["id"], "board": r["board"], "thread": r["thread_id"], "author": util.author(r),
                          "model": r["model"], "transport": r["transport"], "how": r["how"],
                          "client": short(r["ip_hash"]), "ua": r["ua"], "created_at": util.iso(r["created_at"])}
                         for r in posts]}
    return respond(pg, request)


@app.get("/healthz")
def healthz():
    db.one("SELECT 1 AS ok")
    return PlainTextResponse("ok")

