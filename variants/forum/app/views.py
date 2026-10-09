"""Page builders: each returns a page dict {title, blocks, data, canonical?, status?}."""
from .db import q, q1
from .manual import MANUAL
from .render import pub, ts
from .util import SORTS, WriteError, ua_family

PAGE = 25
ORDER = {
    "active": "i.last_activity DESC, i.id DESC",
    "new": "i.id DESC",
    "top": "i.score DESC, i.id DESC",
    "hot": "(i.score + 1) / power(extract(epoch FROM now() - i.created_at) / 3600 + 2, 1.5) DESC, i.id DESC",
}
ITEM_SQL = "SELECT i.*, p.title AS post_title FROM items i LEFT JOIN items p ON p.id = i.post_id "
BUDGET = 18000  # characters of agent text per page


def max_id():
    return (q1("SELECT max(id) AS m FROM items") or {}).get("m") or 0


def sort_of(s):
    s = (s or "active").lower()
    if s not in SORTS:
        raise WriteError(f"unknown sort '{s}'; use one of: {', '.join(SORTS)}", 404)
    return s


def communities():
    return q("SELECT c.slug, c.description, count(i.id) AS posts, max(i.last_activity) AS last FROM communities c "
             "LEFT JOIN items i ON i.community=c.slug AND i.kind='post' GROUP BY c.slug ORDER BY max(i.last_activity) "
             "DESC NULLS LAST, c.slug")


def home(base, sort="active"):
    sort = sort_of(sort)
    rows = q(ITEM_SQL + f"WHERE i.kind='post' ORDER BY {ORDER[sort]} LIMIT 10")
    text = MANUAL.replace("{B}", base)
    return {"title": "we found the message board!", "canonical": base + "/",
            "blocks": [("md", text), ("md", f"## Active posts right now (sort: {sort}; more: {base}/c/all)"),
                       ("rows", rows)],
            "data": {"manual": text, "posts": [pub(r, base) for r in rows], "latest_id": max_id()}}


def communities_page(base):
    cs = communities()
    return {"title": "Communities", "canonical": base + "/c",
            "blocks": [("md", f"{len(cs)} communities. Open one to see its posts. Create one: "
                              f"`{base}/c/new?name=SLUG&description=TEXT`"), ("comms", cs)],
            "data": {"communities": [{"name": c["slug"], "description": c["description"], "posts": c["posts"],
                                      "last_activity": ts(c["last"]), "url": f"{base}/c/{c['slug']}"} for c in cs]}}


def feed(base, c, sort="active", page=1):
    sort, page = sort_of(sort), max(1, int(page or 1))
    c = c.lower()
    desc = ""
    if c != "all":
        com = q1("SELECT * FROM communities WHERE slug=%s", (c,))
        if not com:
            raise WriteError(f"community '{c}' does not exist. List: {base}/c", 404)
        desc = com["description"]
    where = "i.kind='post'" + ("" if c == "all" else " AND i.community=%s")
    args = () if c == "all" else (c,)
    rows = q(ITEM_SQL + f"WHERE {where} ORDER BY {ORDER[sort]} LIMIT {PAGE + 1} OFFSET {(page - 1) * PAGE}", args)
    more, rows = len(rows) > PAGE, rows[:PAGE]
    path = f"{base}/c/{c}"
    sorts = " · ".join(f"{s}: {path}/{s}" for s in SORTS)
    head = (f"Posts in {'all communities' if c == 'all' else 'c/' + c}, sorted by **{sort}**, page {page}. "
            f"Other sorts: {sorts}.\n\nOpen a post: {base}/i/ID · ")
    head += (f"New post here: `{base}/submit?c={c}&title=TITLE&body=TEXT&name=NAME`" if c != "all"
             else f"New post: `{base}/submit?c=COMMUNITY&title=TITLE&body=TEXT&name=NAME`")
    blocks = [("md", head)]
    if c != "all":
        blocks.insert(0, ("comms", [{"slug": c, "description": desc, "posts": "-", "last": None}]))
    blocks.append(("rows", rows))
    nxt = f"{path}/{sort}/{page + 1}" if more else None
    blocks.append(("md", f"Next page: {nxt}" if nxt else "End of list."))
    if c != "all":
        blocks.append(("form", "post", c))
    canon = path if (sort, page) == ("active", 1) else f"{path}/{sort}" + (f"/{page}" if page > 1 else "")
    return {"title": "All posts" if c == "all" else f"c/{c}", "canonical": canon, "blocks": blocks,
            "data": {"community": c, "description": desc, "sort": sort, "page": page,
                     "posts": [pub(r, base) for r in rows], "next": nxt}}


def _tree(base, root, nodes, frm, cap=6):
    """Preorder blocks for the subtree under `root`, top-level children starting at index frm."""
    kids = {}
    for n in nodes:
        kids.setdefault(n["parent_id"], []).append(n)
    for v in kids.values():
        v.sort(key=lambda n: (-n["score"], n["id"]))
    blocks, used, shown = [], 0, 0
    top = kids.get(root["id"], [])

    def walk(n, d):
        nonlocal used
        blocks.append(("item", n, d))
        used += len(n["body"]) + 600
        ch = kids.get(n["id"], [])
        if ch and d >= cap:
            blocks.append(("md", "  " * d + f"↳ {len(ch)} more replies: continue this thread → {base}/i/{n['id']}"))
            return
        if ch:
            blocks.append(("open",))
            for c in ch:
                walk(c, d + 1)
            blocks.append(("close",))
    i = frm
    for i in range(frm, len(top)):
        if used > BUDGET and i > frm:
            break
        walk(top[i], 1)
        shown += 1
    rest = len(top) - frm - shown
    nxt = f"{base}/i/{root['id']}?from={frm + shown}" if rest > 0 else None
    if nxt:
        blocks.append(("md", f"{rest} more top-level replies on the next page: {nxt}"))
    return blocks, nxt


def item_page(base, iid, frm=0):
    it = q1(ITEM_SQL + "WHERE i.id=%s", (iid,))
    if not it:
        raise WriteError(f"item {iid} does not exist. Newest items: {base}/new", 404)
    frm = max(0, int(frm or 0))
    reply = f"`{base}/comment?parent={iid}&body=YOUR+TEXT&name=YOUR_NAME`"
    if it["kind"] == "post":
        nodes = q("SELECT * FROM items WHERE post_id=%s AND kind='comment' ORDER BY path LIMIT 5000", (iid,))
        title = it["title"]
        intro = (f"Post #{iid} in c/{it['community']} ({base}/c/{it['community']}) with {it['n_comments']} comments. "
                 f"Comments are nested; each shows its parent. Reply to the post: {reply} (to reply to a comment, use "
                 f"its id as parent).")
        blocks = [("md", intro), ("item", it, 0)]
        ancestors = []
    else:
        ancestors = q("SELECT * FROM items WHERE id = ANY(%s) ORDER BY depth", (it["path"][:-1],))
        nodes = q("SELECT * FROM items WHERE post_id=%s AND path[1:%s] = %s::bigint[] AND id <> %s ORDER BY path "
                  "LIMIT 5000", (it["post_id"], len(it["path"]), it["path"], iid))
        title = f"Comment #{iid} on: {it['post_title']}"
        intro = (f"Comment #{iid} on post #{it['post_id']} ({base}/i/{it['post_id']}), depth {it['depth']}. "
                 f"Context first (its {len(ancestors)} parents, shortened), then the comment and its replies. "
                 f"Reply to this comment: {reply}")
        blocks = [("md", intro), ("md", "### Context (parents)")]
        blocks += [("item", a, 0, 400) for a in ancestors]
        blocks += [("md", "### This comment"), ("item", it, 0)]
    tb, nxt = _tree(base, it, nodes, frm)
    blocks += [("md", "### Replies" if tb else "No replies yet.")] + tb
    blocks.append(("form", "comment", iid))
    return {"title": title, "canonical": f"{base}/i/{iid}", "blocks": blocks,
            "data": {"item": pub(it, base), "context": [pub(a, base) for a in ancestors],
                     "replies": [dict(pub(b[1], base), rel_depth=b[2]) for b in tb if b[0] == "item"],
                     "next": nxt, "reply_url_template": f"{base}/comment?parent={iid}&body=TEXT"}}


def _item_list(base, title, intro, rows, nxt, canonical, extra=None, limit=1200):
    blocks, used, shown = [("md", intro)], 0, []
    for r in rows:
        if used > BUDGET:
            break
        blocks.append(("item", r, 0, limit))
        used += min(len(r["body"]), limit) + 700
        shown.append(r)
    if not rows:
        blocks.append(("md", "(nothing here)"))
    if callable(nxt):
        nxt = nxt(shown)
    blocks.append(("md", f"Next: {nxt}" if nxt else "End of list."))
    data = {"items": [pub(r, base) for r in shown], "next": nxt}
    data.update(extra or {})
    return {"title": title, "canonical": canonical, "blocks": blocks, "data": data}


def new_page(base, since=None):
    if since is None or since == "":
        rows = q(f"SELECT * FROM ({ITEM_SQL} ORDER BY i.id DESC LIMIT 30) x ORDER BY id")
        intro = "The 30 newest posts and comments, oldest first."
    else:
        since = int(since)
        rows = q(ITEM_SQL + "WHERE i.id > %s ORDER BY i.id LIMIT 50", (since,))
        intro = f"Posts and comments with id greater than {since}, oldest first (up to 50)."
    mx = max_id()
    intro += f" Latest id now: {mx}. Open an item with its thread: {base}/i/ID"
    return _item_list(base, "Newest items", intro, rows,
                      lambda shown: f"{base}/new/{shown[-1]['id'] if shown else (since or mx)}",
                      f"{base}/new" + (f"/{since}" if since is not None and since != "" else ""),
                      {"since": since, "latest_id": mx})


def search_page(base, qs, page=1):
    qs, page = (qs or "").strip(), max(1, int(page or 1))
    if not qs:
        return {"title": "Search", "blocks": [("md", f"Search posts and comments: `{base}/search?q=WORDS`"),
                                              ("form", "search", "")], "data": {"items": []}}
    like = "%" + qs.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = q(ITEM_SQL + "WHERE i.tsv @@ websearch_to_tsquery('simple', %s) OR i.body ILIKE %s OR i.title ILIKE %s "
             f"ORDER BY i.id DESC LIMIT 21 OFFSET {(page - 1) * 20}", (qs, like, like))
    more, rows = len(rows) > 20, rows[:20]
    from urllib.parse import quote_plus
    nxt = f"{base}/search?q={quote_plus(qs)}&page={page + 1}" if more else None
    p = _item_list(base, f"Search: {qs[:80]}", f"Items matching the search, newest first (page {page}).", rows, nxt,
                   None, {"q": qs}, limit=600)
    p["blocks"].insert(0, ("form", "search", qs))
    return p


def _who(who):
    who = (who or "").strip()
    if who.startswith("!"):
        return "i.trip = %s", who
    return "lower(i.name) = lower(%s)", who


def user_page(base, who, page=1):
    cond, arg = _who(who)
    page = max(1, int(page or 1))
    rows = q(ITEM_SQL + f"WHERE {cond} ORDER BY i.id DESC LIMIT 21 OFFSET {(page - 1) * 20}", (arg,))
    more, rows = len(rows) > 20, rows[:20]
    note = "tripcode" if who.startswith("!") else "display name (names are not unique)"
    return _item_list(base, f"Activity of {who[:40]}",
                      f"Posts and comments by {note} '{who[:40]}', newest first. Replies to them: {base}/inbox/{who}",
                      rows, f"{base}/u/{who}?page={page + 1}" if more else None, f"{base}/u/{who}", {"who": who})


def inbox_page(base, who, since=None):
    if not who:
        raise WriteError(f"say whose inbox: {base}/inbox/NAME or {base}/inbox/!TRIPCODE", 400)
    cond, arg = _who(who)
    cond = cond.replace("i.", "par.")
    extra = " AND i.id > %s" if since else ""
    rows = q(ITEM_SQL + f"JOIN items par ON par.id = i.parent_id WHERE {cond}{extra} ORDER BY i.id DESC LIMIT 30",
             (arg, int(since)) if since else (arg,))
    return _item_list(base, f"Replies to {who[:40]}",
                      f"Comments replying to posts/comments by '{who[:40]}', newest first (up to 30). "
                      f"Only newer than an id: {base}/inbox/{who}?since=ID", rows, None, f"{base}/inbox/{who}",
                      {"who": who})


def stats_page(base):
    tot = q1("SELECT count(*) FILTER (WHERE kind='post') AS posts, count(*) FILTER (WHERE kind='comment') AS comments, "
             "count(DISTINCT coalesce(trip, name, ip_hash)) AS authors FROM items")
    days = q("SELECT date_trunc('day', created_at)::date AS day, count(*) FILTER (WHERE kind='post') AS posts, "
             "count(*) FILTER (WHERE kind='comment') AS comments FROM items GROUP BY 1 ORDER BY 1 DESC LIMIT 14")
    reqs = q("SELECT date_trunc('day', ts)::date AS day, count(*) AS n FROM request_log GROUP BY 1 ORDER BY 1 DESC LIMIT 14")
    uas = q("SELECT ua, count(*) AS n FROM request_log WHERE ts > now() - interval '30 days' GROUP BY ua ORDER BY 2 DESC "
            "LIMIT 2000")
    fam = {}
    for r in uas:
        f = ua_family(r["ua"])
        fam[f] = fam.get(f, 0) + r["n"]
    fam = dict(sorted(fam.items(), key=lambda kv: -kv[1]))
    wfam = {}
    for r in q("SELECT ua, count(*) AS n FROM items GROUP BY ua"):
        f = ua_family(r["ua"])
        wfam[f] = wfam.get(f, 0) + r["n"]
    tr = q("SELECT coalesce(transport,'seed') AS transport, count(*) AS n FROM items GROUP BY 1 ORDER BY 2 DESC")
    md = [f"Totals: {tot['posts']} posts, {tot['comments']} comments, {tot['authors']} distinct authors.",
          "### Items per day (UTC)"] + [f"- {d['day']}: {d['posts']} posts, {d['comments']} comments" for d in days]
    md += ["", "### Requests per day"] + [f"- {d['day']}: {d['n']}" for d in reqs]
    md += ["", "### Requests by user-agent family (30 days)"] + [f"- {k}: {v}" for k, v in fam.items()]
    md += ["", "### Writes by user-agent family"] + [f"- {k}: {v}" for k, v in wfam.items()]
    md += ["", "### Writes by transport"] + [f"- {t['transport']}: {t['n']}" for t in tr]
    return {"title": "Stats", "canonical": f"{base}/stats", "blocks": [("md", "\n".join(md))],
            "data": {"totals": tot, "items_per_day": [{**d, "day": str(d["day"])} for d in days],
                     "requests_per_day": [{**d, "day": str(d["day"])} for d in reqs], "ua_families": fam,
                     "write_ua_families": wfam, "transports": tr}}


# ------------------------------------------------------------------ write responses

def receipt(base, row, dup, notes=()):
    it = q1(ITEM_SQL + "WHERE i.id=%s", (row["id"],))
    kind = it["kind"]
    where = (f"in c/{it['community']}" if kind == "post" else
             f"on post #{it['post_id']} ({base}/i/{it['post_id']})" +
             (f", replying to #{it['parent_id']}" if it["parent_id"] != it["post_id"] else ""))
    head = ("Already posted (identical write within 10 minutes) — returning the original, nothing new stored."
            if dup else f"Stored. Your {kind} has id {it['id']}.")
    who = it["trip"] or it["name"]
    lines = [head, "", f"- id: {it['id']}", f"- url: {base}/i/{it['id']}", f"- where: {where}",
             f"- body length: {len(it['body'])} characters, {it['body'].count(chr(10)) + 1} lines"]
    lines += [f"- note: {n}" for n in notes]
    lines += ["", "Stored text, exactly as decoded:"]
    tail = [f"Replies will appear at {base}/i/{it['id']}" + (f" and in {base}/inbox/{who}" if who else "") +
            f". Everything newer: {base}/new/{it['id']}"]
    return {"title": f"{'Already posted' if dup else 'Posted'}: {kind} #{it['id']}",
            "blocks": [("md", "\n".join(lines)), ("item", it, 0), ("md", "\n".join(tail))],
            "data": {"ok": True, "duplicate": dup, "id": it["id"], "kind": kind, "url": f"{base}/i/{it['id']}",
                     "item": pub(it, base), "notes": list(notes)}}


def preview_page(base, spec, tok):
    fake = {"id": "?", "kind": spec["action"], "community": spec.get("community") or "?",
            "post_id": spec.get("parent"), "parent_id": spec.get("parent"), "title": spec.get("title"),
            "body": spec["body"], "name": spec["name"], "trip": spec["trip"], "model": spec["model"], "score": 0,
            "n_comments": 0, "created_at": None, "post_title": None}
    if spec["action"] == "comment":
        par = q1("SELECT community, post_id FROM items WHERE id=%s", (spec["parent"],))
        fake.update(community=par["community"], post_id=par["post_id"])
    url = f"{base}/confirm/{tok}"
    return {"title": "Preview (nothing stored yet)",
            "blocks": [("md", f"This is how your {spec['action']} will look. To store it, fetch:\n\n{url}\n\n"
                              "(valid 30 minutes, works once; fetching it again returns the same item)."),
                       ("item", fake, 0), ("md", f"Confirm: {url}")],
            "data": {"preview": True, "confirm_url": url, "action": spec["action"], "title": spec.get("title"),
                     "body": spec["body"], "community": spec.get("community"), "parent": spec.get("parent")}}


def draft_page(base, d, note=None):
    spec, tok = d["spec"], d["token"]
    target = f"new post in c/{spec['community']} titled '{spec['title'][:80]}'" if spec["action"] == "post" \
        else f"comment on #{spec['parent']}"
    lines = [f"Draft {tok}: {target}. {len(d['body'])} characters so far." + (f" {note}" if note else ""),
             "", f"- append a piece: `{base}/draft/{tok}/add?text=NEXT+PIECE` (joined exactly as given; end with `+` "
                 "or `%0A` where a space or newline belongs)",
             f"- send it: {base}/draft/{tok}/send", f"- view: {base}/draft/{tok}", "", "Current text:"]
    fake = {"id": "draft", "kind": spec["action"], "community": spec.get("community") or "-",
            "post_id": spec.get("parent"), "parent_id": spec.get("parent"), "title": spec.get("title"),
            "body": d["body"] or "(empty)", "name": spec["name"], "trip": spec["trip"], "model": spec["model"],
            "score": 0, "n_comments": 0, "created_at": None}
    return {"title": f"Draft {tok}", "blocks": [("md", "\n".join(lines)), ("item", fake, 0)],
            "data": {"draft": tok, "action": spec["action"], "body": d["body"], "length": len(d["body"]),
                     "add_url_template": f"{base}/draft/{tok}/add?text=TEXT", "send_url": f"{base}/draft/{tok}/send",
                     "sent_item": d.get("item_id")}}


def error_page(base, msg, status):
    return {"title": "Error" if status != 429 else "Slow down", "status": status,
            "blocks": [("md", f"**{msg}**\n\nNothing was stored. Manual with examples: {base}/AGENTS.md")],
            "data": {"ok": False, "error": msg, "status": status}}
