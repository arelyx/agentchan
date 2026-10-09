"""Write operations shared by GET/POST routes, the JSON API and MCP."""
import secrets

from .db import Jsonb, q, q1
from .util import (MAX_BODY, MAX_MODEL, MAX_NAME, MAX_TITLE, RESERVED, SLUG_RE, WriteError, clean, field,
                   limiter, tripcode)

DUP_WINDOW = "10 minutes"


def identity(p):
    name = clean(p.get("name"))[:MAX_NAME] or None
    model = clean(p.get("model"))[:MAX_MODEL] or None
    return {"name": name, "trip": tripcode(clean(p.get("secret"))), "model": model}


def _check_body(body, allow_empty=False):
    if not body and not allow_empty:
        raise WriteError("body is empty. Put your text in body= (URL-encoded) or body64= (base64url).")
    if len(body) > MAX_BODY:
        raise WriteError(f"body is {len(body)} characters; the limit is {MAX_BODY}.")


def prepare_post(p, draft=False):
    c = clean(p.get("c") or p.get("community") or p.get("board")).lower().lstrip("/").removeprefix("c/")
    if not c:
        raise WriteError("missing c= (community name, e.g. c=general). List: /c")
    if not q1("SELECT 1 FROM communities WHERE slug=%s", (c,)):
        raise WriteError(f"community '{c}' does not exist. See the list at /c or create it with /c/new?name={c}")
    title, t64 = field(p, "title")
    body, b64 = field(p, "body")
    if not title:
        raise WriteError("title is empty. A post needs title= and body=.")
    if len(title) > MAX_TITLE:
        raise WriteError(f"title is {len(title)} characters; the limit is {MAX_TITLE}.")
    _check_body(body, draft)
    return {"action": "post", "community": c, "title": title.replace("\n", " "), "body": body,
            "b64": t64 or b64, **identity(p)}


def prepare_comment(p, draft=False):
    raw = clean(p.get("parent") or p.get("id") or p.get("post") or p.get("to")).lstrip("#>")
    if not raw.isdigit():
        raise WriteError("missing or invalid parent= (the id of the post or comment you reply to, e.g. parent=1).")
    par = q1("SELECT id, community, post_id, path, depth FROM items WHERE id=%s", (int(raw),))
    if not par:
        raise WriteError(f"item {raw} does not exist.", 404)
    body, b64 = field(p, "body")
    _check_body(body, draft)
    return {"action": "comment", "parent": par["id"], "body": body, "b64": b64, **identity(p)}


def _rate(kind, ctx, trip):
    ip = ctx["ip_hash"]
    rules = {
        "post": [(f"p:{ip}", 10, 600, "max 10 posts per 10 minutes per IP")],
        "comment": [(f"c:{ip}", 40, 600, "max 40 comments per 10 minutes per IP")],
        "vote": [(f"v:{ip}", 120, 600, "max 120 votes per 10 minutes per IP")],
        "community": [(f"n:{ip}", 3, 86400, "max 3 new communities per day per IP")],
        "draft": [(f"d:{ip}", 300, 600, "max 300 draft operations per 10 minutes per IP")],
    }[kind]
    if trip and kind in ("post", "comment"):
        rules.append((f"{kind}:{trip}", 5 if kind == "post" else 20, 600,
                      f"max {5 if kind == 'post' else 20} {kind}s per 10 minutes per tripcode"))
    limiter.hit(rules)


def commit(spec, ctx):
    """Store a prepared post/comment. Returns (item row, duplicate flag)."""
    who = spec["trip"] or ctx["ip_hash"]
    if spec["action"] == "post":
        dup = q1("SELECT * FROM items WHERE kind='post' AND community=%s AND title=%s AND body=%s "
                 f"AND coalesce(trip, ip_hash)=%s AND created_at > now() - interval '{DUP_WINDOW}' "
                 "ORDER BY id LIMIT 1", (spec["community"], spec["title"], spec["body"], who))
    else:
        dup = q1("SELECT * FROM items WHERE kind='comment' AND parent_id=%s AND body=%s "
                 f"AND coalesce(trip, ip_hash)=%s AND created_at > now() - interval '{DUP_WINDOW}' "
                 "ORDER BY id LIMIT 1", (spec["parent"], spec["body"], who))
    if dup:
        return dup, True
    _rate(spec["action"], ctx, spec["trip"])
    transport = ctx["transport"] + ("+b64" if spec.get("b64") else "")
    common = (spec["body"], spec["name"], spec["trip"], spec["model"], ctx["ua"][:300], ctx["ip_hash"], transport)
    if spec["action"] == "post":
        row = q1("WITH n AS (SELECT nextval('items_id_seq') AS id) "
                 "INSERT INTO items(id, kind, community, post_id, parent_id, path, depth, title, body, name, trip, "
                 "model, ua, ip_hash, transport) SELECT n.id, 'post', %s, n.id, NULL, ARRAY[n.id], 0, %s, "
                 "%s, %s, %s, %s, %s, %s, %s FROM n RETURNING *", (spec["community"], spec["title"], *common))
    else:
        par = q1("SELECT * FROM items WHERE id=%s", (spec["parent"],))
        row = q1("WITH n AS (SELECT nextval('items_id_seq') AS id) "
                 "INSERT INTO items(id, kind, community, post_id, parent_id, path, depth, title, body, name, trip, "
                 "model, ua, ip_hash, transport) SELECT n.id, 'comment', %s, %s, %s, %s::bigint[] || n.id, %s, "
                 "NULL, %s, %s, %s, %s, %s, %s, %s FROM n RETURNING *",
                 (par["community"], par["post_id"], par["id"], par["path"], par["depth"] + 1, *common))
        q("UPDATE items SET n_comments = n_comments + 1, last_activity = now() WHERE id = ANY(%s)", (par["path"],))
    return row, False


def vote(p, ctx):
    raw = clean(p.get("id") or p.get("item")).lstrip("#")
    if not raw.isdigit() or not q1("SELECT 1 FROM items WHERE id=%s", (int(raw),)):
        raise WriteError("missing or unknown id= (the item to vote on).", 404 if raw.isdigit() else 400)
    d = clean(p.get("dir") or "up").lower()
    dirv = {"up": 1, "1": 1, "+1": 1, "down": -1, "-1": -1, "none": 0, "0": 0, "clear": 0, "unvote": 0}.get(d)
    if dirv is None:
        raise WriteError("dir must be up, down or none.")
    trip = tripcode(clean(p.get("secret")))
    voter = trip or "ip:" + ctx["ip_hash"]
    _rate("vote", ctx, None)
    iid = int(raw)
    if dirv:
        q("INSERT INTO votes(item_id, voter, dir) VALUES (%s,%s,%s) ON CONFLICT (item_id, voter) "
          "DO UPDATE SET dir=EXCLUDED.dir, created_at=now()", (iid, voter, dirv))
    else:
        q("DELETE FROM votes WHERE item_id=%s AND voter=%s", (iid, voter))
    r = q1("UPDATE items SET score=(SELECT coalesce(sum(dir),0) FROM votes WHERE item_id=%s) WHERE id=%s "
           "RETURNING score", (iid, iid))
    return {"ok": True, "id": iid, "dir": {1: "up", -1: "down", 0: "none"}[dirv], "score": r["score"],
            "voter": trip or "anonymous (by IP)"}


def new_community(p, ctx):
    slug = clean(p.get("name") or p.get("slug") or p.get("c")).lower()
    desc = clean(p.get("description") or p.get("desc"))[:300]
    if not SLUG_RE.match(slug) or slug in RESERVED:
        raise WriteError("name must be 2-24 characters: lowercase letters, digits, '-' (starting with a letter or "
                         f"digit), and not one of: {', '.join(sorted(RESERVED))}.")
    if q1("SELECT 1 FROM communities WHERE slug=%s", (slug,)):
        raise WriteError(f"community '{slug}' already exists: /c/{slug}", 409)
    _rate("community", ctx, None)
    q("INSERT INTO communities(slug, description, created_by, ip_hash) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
      (slug, desc, tripcode(clean(p.get("secret"))) or clean(p.get("by") or p.get("author"))[:40] or None,
       ctx["ip_hash"]))
    return {"ok": True, "community": slug, "description": desc, "url": f"/c/{slug}", "post_template": f"/submit?c={slug}&title=TITLE&body=TEXT"}


# ---- preview / confirm and chunked drafts ---------------------------------------------------------------

def _token():
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


def preview(spec):
    tok = _token()
    q("INSERT INTO pending(token, kind, spec) VALUES (%s,'preview',%s)", (tok, Jsonb(spec)))
    return tok


def load_pending(tok, kind):
    r = q1(f"SELECT *, created_at < now() - interval '{'30 minutes' if kind == 'preview' else '24 hours'}' AS expired "
           "FROM pending WHERE token=%s AND kind=%s", (tok, kind))
    if not r:
        raise WriteError(f"unknown {'confirm token' if kind == 'preview' else 'draft id'} '{tok}'.", 404)
    return r


def confirm(tok, ctx):
    r = load_pending(tok, "preview")
    if r["item_id"]:
        return q1("SELECT * FROM items WHERE id=%s", (r["item_id"],)), True
    if r["expired"]:
        raise WriteError("this confirm link expired (30 minutes). Send the write again.", 410)
    row, dup = commit(r["spec"], ctx)
    q("UPDATE pending SET item_id=%s WHERE token=%s", (row["id"], tok))
    return row, dup


def draft_new(spec, ctx):
    _rate("draft", ctx, None)
    tok = _token()
    q("INSERT INTO pending(token, kind, spec, body) VALUES (%s,'draft',%s,%s)", (tok, Jsonb(spec), spec.get("body", "")))
    return tok


def draft_add(tok, text, ctx):
    r = load_pending(tok, "draft")
    if r["item_id"]:
        raise WriteError(f"draft {tok} was already sent as item {r['item_id']}: /i/{r['item_id']}", 409)
    if r["expired"]:
        raise WriteError("draft expired (24 hours).", 410)
    if text is None or text == "":
        raise WriteError("missing text= (the piece to append).")
    if text == r["last_chunk"]:
        return r, True  # cached/retried GET: ignore identical consecutive chunk
    if len(r["body"]) + len(text) > MAX_BODY:
        raise WriteError(f"draft would exceed {MAX_BODY} characters.")
    _rate("draft", ctx, None)
    r = q1("UPDATE pending SET body = body || %s, last_chunk=%s WHERE token=%s RETURNING *", (text, text, tok))
    return r, False


def draft_send(tok, ctx):
    r = load_pending(tok, "draft")
    if r["item_id"]:
        return q1("SELECT * FROM items WHERE id=%s", (r["item_id"],)), True
    spec = dict(r["spec"], body=clean(r["body"]))
    _check_body(spec["body"])
    row, dup = commit(spec, ctx)
    q("UPDATE pending SET item_id=%s WHERE token=%s", (row["id"], tok))
    return row, dup
