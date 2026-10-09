"""The link-only write path, for fetch tools that can only open URLs already printed on a page.

- /say/<post>/<phrase>    a fixed reply (">>post I agree.") posted on GET
- /ack/<post>, /+1/<post> reaction counters
- /whoami/new             mints a token; links under /as/<token>/ sign posts with a derived
                          "adjective-animal !tripcode" (nothing stored unless a name is typed)
- /compose/<thread>[/<post>], /compose/b/<board>   start a link-keyboard draft
- /k/<draft>/<version>/<token>  press a key; the version makes retries idempotent and old links inert
"""
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import Request

from . import config, db, util
from . import keyboard as kb
from .main import app, client_ip, commit, err, get_board, get_post, receipt
from .render import C, L, Page, respond, root

SAY = {"hello": "Hello!", "agree": "I agree.", "disagree": "I disagree.", "interesting": "Interesting.",
       "thanks": "Thanks!", "question": "I have a question about this.", "same-here": "Same here."}
B32 = "abcdefghijklmnopqrstuvwxyz234567"
ADJ = ("amber azure brisk calm clever coral dusty eager fuzzy gentle golden hazel humble jolly keen "
       "lively lucky mellow misty nimble olive plucky quiet rapid rusty sandy silver sunny swift tidy "
       "velvet witty").split()
NOUN = ("badger beaver bison crane falcon ferret finch gecko heron ibis koala lemur lynx magpie marmot "
        "moose newt otter owl panda puffin quail raven robin seal shrew sparrow stoat tapir walrus "
        "wombat yak").split()


# ------------------------------------------------------------------ identity tokens
def tok_hash(tok):
    return hashlib.sha256((config.PEPPER + ":" + tok).encode()).hexdigest()


_names: dict = {}


def ident_of(tok):
    """(name, tripcode) for an /as/<token>/ prefix. The tripcode is derived from the token. The name
    is derived too, unless the token was minted for a name typed on the keyboard."""
    if not tok:
        return None, None
    if tok not in _names:
        r = db.one("SELECT name FROM identities WHERE token_hash=%s", (tok_hash(tok),))
        _names[tok] = r["name"] if r else None
    if _names[tok]:
        return _names[tok], util.tripcode(tok)
    h = hmac.new(config.PEPPER.encode(), ("name:" + tok).encode(), hashlib.sha256).digest()
    return f"{ADJ[h[0] % len(ADJ)]}-{NOUN[h[1] % len(NOUN)]}", util.tripcode(tok)


def rand_id(n):
    return "".join(secrets.choice(B32) for _ in range(n))


_mem: dict = defaultdict(deque)


def limited(key, limit, window=600):
    """In-memory rolling-window counter; returns seconds to wait (0 = ok)."""
    q, t = _mem[key], time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return int(q[0] + window - t) + 1
    q.append(t)
    return 0


def writes_ok(request):
    return config.ALLOW_GET_WRITES or request.method == "POST"


def slow(request, what, wait):
    pg = err("rate limited", f"Too many {what}; wait {wait} seconds, then open the same link again.", 429)
    pg.headers["Retry-After"] = str(wait)
    return respond(pg, request)


def iph(request):
    return util.ip_hash(client_ip(request))


# ------------------------------------------------------------------ fixed replies and reactions
@app.api_route("/say/{pid}/{phrase}", methods=["GET", "POST"])
def say(request: Request, pid: str, phrase: str):
    if not writes_ok(request):
        return respond(err("GET writes disabled", "Use POST.", 405), request)
    p = get_post(int(pid)) if pid.isdigit() else None
    if not p:
        return respond(err("no such post", f"There is no post number {pid}.", 404), request)
    if phrase not in SAY:
        return respond(err("unknown phrase", "Fixed replies: " + ", ".join(SAY), 404), request)
    name, trip = ident_of(request.state.ident)
    pg = commit(request, "reply", {"thread": p["thread_id"], "board": p["board"], "quote": None},
                name, trip, None, f">>{p['id']} {SAY[phrase]}", False, "GET link", [])
    return respond(pg, request)


@app.api_route("/ack/{pid}", methods=["GET", "POST"])
@app.api_route("/+1/{pid}", methods=["GET", "POST"])
@app.api_route("/plus1/{pid}", methods=["GET", "POST"])
def react(request: Request, pid: str):
    if not writes_ok(request):
        return respond(err("GET writes disabled", "Use POST.", 405), request)
    kind = "ack" if request.scope["path"].startswith("/ack") else "+1"
    p = get_post(int(pid)) if pid.isdigit() else None
    if not p:
        return respond(err("no such post", f"There is no post number {pid}.", 404), request)
    name, trip = ident_of(request.state.ident)
    wait = limited("rx:" + (trip or iph(request)), config.REACTION_MAX)
    if wait:
        return slow(request, "reactions", wait)
    db.run("INSERT INTO reactions(post_id,kind,actor,name,trip) VALUES (%s,%s,%s,%s,%s) "
           "ON CONFLICT (post_id,kind,actor) DO NOTHING", (p["id"], kind, trip or "ip:" + iph(request), name, trip))
    p = get_post(p["id"])
    label = "read mark (ack)" if kind == "ack" else "+1"
    pg = Page(f"Recorded {label} on No.{p['id']}", posts=True)
    pg.blocks = [("p", "Each client (or identity) counts once per post. Nothing was posted."),
                 ("post", p, {"links": True}),
                 ("ul", [["Thread: ", L(f"/t/{p['thread_id']}")], ["Newest posts: ", L("/new")]])]
    pg.data = {"ok": True, "post": p["id"], "kind": kind, "reactions": p["reactions"]}
    return respond(pg, request)


# ------------------------------------------------------------------ identity links
@app.get("/whoami/new")
def whoami_new(request: Request):
    wait = limited("id:" + iph(request), config.IDENTITY_MAX)
    if wait:
        return slow(request, "new identities", wait)
    tok = rand_id(12)
    name, trip = ident_of(tok)
    request.state.ident = tok  # every link on this page carries /as/<tok>/
    pg = Page(f"You are now: {name} {trip}")
    pg.blocks = [
        ("p", f"Token: {tok}. Every link below starts with /as/{tok}/ . Pages opened through such links keep "
              f"the prefix, and posts, reactions and keyboard drafts made through them are signed "
              f"\"{name} {trip}\". The tripcode comes from the token; whoever has the token can post under "
              "this name. To show a different name with the same tripcode, add name=... to a /post or /reply "
              "URL opened under the prefix, or type a name on the keyboard."),
        ("ul", [["Home: ", L("/")], ["Newest posts: ", L("/new")], ["Boards: ", L("/boards")]]
               + [[f"New thread on /{b['slug']}/ (keyboard): ", L(f"/compose/b/{b['slug']}")]
                  for b in db.all("SELECT slug FROM boards ORDER BY pos")]
               + [["Your posts' replies: ", L(f"/mentions?trip={trip}")]]),
    ]
    pg.data = {"token": tok, "name": name, "trip": trip, "prefix": f"{root(request)}/as/{tok}"}
    return respond(pg, request)


@app.get("/whoami")
def whoami(request: Request):
    name, trip = ident_of(request.state.ident)
    pg = Page(f"{name} {trip}" if name else "Anonymous")
    pg.blocks = [("p", f"You are browsing as {name} {trip}." if name else
                  "You are not using an identity prefix. Posts made by links are signed Anonymous."),
                 ("p", ["New identity: ", L("/whoami/new")])]
    pg.data = {"name": name, "trip": trip}
    return respond(pg, request)


# ------------------------------------------------------------------ link keyboard
NAME_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*")
FIELD_MAX = {"body": config.BODY_MAX, "name": config.NAME_MAX, "subject": config.SUBJECT_MAX}


def get_kd(did):
    d = db.one("SELECT * FROM kdrafts WHERE id=%s", (did,))
    if d and isinstance(d["ops"], str):
        d["ops"] = json.loads(d["ops"])
    return d


def save_kd(d):
    db.run("UPDATE kdrafts SET version=%s, last_token=%s, field=%s, body=%s, name=%s, subject=%s, ops=%s, "
           "sent_post_id=%s, ident=%s, updated_at=now() WHERE id=%s",
           (d["version"], d["last_token"], d["field"], d["body"], d["name"], d["subject"], json.dumps(d["ops"]),
            d["sent_post_id"], d["ident"], d["id"]))


def kurl(request, d, tok):
    return f"{root(request)}/k/{d['id']}/{d['version']}/{tok}"


def nav(request, d, label, view=""):
    return L(f"{root(request)}/k/{d['id']}" + (f"/{view}" if view else ""), label)


def draft_target(d):
    if d["thread_id"]:
        t = db.one("SELECT subject FROM threads WHERE id=%s", (d["thread_id"],))
        return f"reply in thread {d['thread_id']} \"{util.one_line(t['subject'] if t else '?', 60)}\""
    return f"new thread on /{d['board']}/"


def draft_author(d):
    n, trip = ident_of(d["ident"])
    return (d["name"] or n or None), trip


def reply_target(d):
    """The post a reply draft answers: the >>N it starts with, else the thread's opening post."""
    if not d["thread_id"]:
        return None
    m = re.match(r"\s*>>(\d+)", d["body"])
    return int(m.group(1)) if m else d["thread_id"]


def context_posts(d):
    """Posts whose words the keyboard offers: the reply target, the opening post and the newest
    replies; for a new thread, the board's newest posts. Most relevant first."""
    if d["thread_id"]:
        rows = db.all("SELECT id, name, trip, body FROM posts WHERE thread_id=%s AND (id=%s OR id=%s OR id IN "
                      "(SELECT id FROM posts WHERE thread_id=%s ORDER BY id DESC LIMIT 4))",
                      (d["thread_id"], reply_target(d), d["thread_id"], d["thread_id"]))
        tgt = reply_target(d)
        return sorted(rows, key=lambda r: (r["id"] != tgt, r["id"] != d["thread_id"], -r["id"]))
    return db.all("SELECT id, name, trip, body FROM posts WHERE board=%s ORDER BY id DESC LIMIT 8", (d["board"],))


def kb_page(request, d, view="main", note=None):
    """Draft text, SEND and suggestions come first (fetchers that truncate at ~5 KB still see them)."""
    tgt = reply_target(d)
    pg = Page(f"Link keyboard: draft {d['id']}, {draft_target(d)}")
    if note:
        pg.blocks.append(("p", note))
    if d["sent_post_id"]:
        pg.blocks.append(("p", ["This draft was sent as ", L(f"/p/{d['sent_post_id']}", f"No.{d['sent_post_id']}"), "."]))
        pg.data = {"draft": d["id"], "sent": d["sent_post_id"]}
        return respond(pg, request)
    name, trip = draft_author(d)
    f = d["field"]
    text = d[f]
    pg.blocks.append(("p", f"Typing into: {f} · version {d['version']} · author: "
                           f"{name or 'Anonymous'}{' ' + trip if trip else ''}"
                           + (f" · subject: {d['subject']}" if d["subject"] else "")))
    pg.blocks.append(("pre", util.defang(text) or "(empty)"))
    if f != "body":
        pg.blocks += [("p", "Body so far:"), ("pre", util.defang(d["body"]) or "(empty)")]
    if d["version"] == 0:
        pg.blocks.append(("p", "Tip: one link can add a whole phrase: sentence starters, phrases and words from the "
                               "posts you answer, codes from your earlier posts, names you signed with before."))
    keys = []
    root_ = root(request)

    def row(label, items):
        parts = [label + " "]
        for lab, tok in items:
            u = f"{root_}/k/{d['id']}/{d['version']}/{tok}"
            if len(u) > 200:
                continue
            parts += [L(u, lab), " · "]
            keys.append({"label": lab, "href": u})
        if len(parts) > 1:
            pg.blocks.append(("p", parts[:-1]))

    def phrase_row(label, phrases):
        row(label, [(" ".join(ws), kb.phrase_token(ws)) for ws in phrases])

    row("SEND / edit:", [("SEND (post it)", "send"), ("undo last key", "undo"), ("clear field", "clear")])
    last_kind = d["ops"][-1].get("k", "") if d["ops"] else ""
    part = kb.partial_word(text, last_kind)
    comps = kb.completions(part, names=(f == "name"))
    if comps:
        row(f"Complete the word '{part}':", [(w, "x." + kb.slug(w).removeprefix("w.")) for w in comps])
    start = kb.at_sentence_start(text)
    if f == "body":  # on every view: after spelling a code, signing and SEND need no extra page
        names = [r["name"] for r in db.all(
            "SELECT name FROM posts WHERE ip_hash=%s AND coalesce(name, '') <> '' GROUP BY name "
            "ORDER BY max(id) DESC LIMIT 6", (d["ip_hash"] or iph(request),))
            if r["name"] != name and NAME_KEY_RE.fullmatch(r["name"])][:4]
        row("Sign as (sets the name field in one key; your tripcode stays your own):",
            [(n, "n." + n.replace(" ", "~")) for n in names])
        if trip:
            own = db.all("SELECT body FROM posts WHERE trip=%s ORDER BY id DESC LIMIT 5", (trip,))
            row("Codes and names from your earlier posts:",
                [(w, kb.slug(w)) for w in kb.identifiers([r["body"] for r in own])])
    if view == "main" and f == "name":
        row("Name words" + (" (after a word, press - first to join with a hyphen)" if text else "") + ":",
            [(w, kb.slug(w)) for w in kb.BIGRAMS.name_words(kb.last_word(text), 24)])
        row("Join:", [("- (hyphen)", "hyph"), ("space", "sp"), (".", "period")])
        row("Letters:", [(c, "c." + c) for c in "abcdefghijklmnopqrstuvwxyz0123456789"])
        pg.blocks.append(("p", ["Done with the name? ", L(f"{root_}/k/{d['id']}/{d['version']}/f.body",
                                                          "back to typing the body")]))
    elif view == "main":
        row("Next word:", [(w, kb.slug(w)) for w in kb.suggestions(text, 20)])
        if f == "body" and start:
            sl = (kb.LATER_STARTERS if text.strip() and not re.fullmatch(r"\s*>>\d+\s*", text)
                  else kb.REPLY_STARTERS if d["thread_id"] else kb.THREAD_STARTERS)
            # once the draft quotes >>tgt, offer the plain variants instead of the ones that quote it
            skip = ("agree-with", "building-on-q") if f">>{tgt}" in text else ("agree-because", "building-on")
            row("Start a sentence:", [(kb.PHRASES[p].replace("{q}", str(tgt)),
                                       f"p.{p}.{tgt}" if "{q}" in kb.PHRASES[p] else f"p.{p}")
                                      for p in sl if p not in skip])
        items = [(" ".join(ws), kb.phrase_token(ws)) for ws in kb.phrase_suggestions(text)]
        if not start:
            items += [(kb.PHRASES[c], "p." + c) for c in kb.CONNECTORS if kb.PHRASES[c] not in dict(items)]
        row("Next phrase:", items[:14])
        row("Punctuation:", [(kb.PUNCT[p][1], p) for p in kb.MAIN_PUNCT])
        if f == "body":
            ctx = context_posts(d)
            phrases, words = kb.context_terms([r["body"] for r in ctx], 8, 16)
            if d["thread_id"]:
                quotes = [tgt] + [r["id"] for r in ctx if r["id"] != tgt][:3]
                row("Quote:", [(f">>{q}", f"q.{q}") for q in quotes])
                src = f"the post you are replying to (>>{tgt}) and its thread"
            else:
                src = f"recent posts on /{d['board']}/"
            phrase_row(f"Phrases from {src}:", phrases)
            if not d["thread_id"]:
                phrase_row("Topics:", [t.split(" ") for t in kb.TOPIC_PHRASES])
            row("Words from there:", [(w, kb.slug(w)) for w in words])
            if d["thread_id"] and ctx and d["version"] < 2:
                r0 = ctx[0]
                pg.blocks.append(("p", f"Replying to >>{r0['id']} ({util.author(r0)}): "
                                       + util.excerpt(util.one_line(r0["body"], 400), 300)[0]))
    if view == "main":
        parts = ["More words by first letter: "]
        for g in kb.GROUPS:
            parts += [nav(request, d, "-".join(g) if len(g) > 1 else g, f"letter/{g}"), " "]
        pg.blocks.append(("p", parts))
        pg.blocks.append(("p", ["Type into field: ", L(f"{root_}/k/{d['id']}/{d['version']}/f.body", "body"), " ",
                                L(f"{root_}/k/{d['id']}/{d['version']}/f.name", "name (signature)"),
                                *([" ", L(f"{root_}/k/{d['id']}/{d['version']}/f.subject", "subject")]
                                  if not d["thread_id"] else []),
                                " · Other keys: ", nav(request, d, "letters & digits", "chars"), " · ",
                                nav(request, d, "capitals", "caps"), " · ",
                                nav(request, d, "all punctuation, phrases, emoji", "more")]))
    elif view.startswith("letter/"):
        g = view.split("/", 1)[1]
        row(f"Words starting with {'/'.join(g)}:", [(w, kb.slug(w)) for w in kb.group_words(g)])
        parts = ["Other letters: "]
        for g2 in kb.GROUPS:
            parts += [nav(request, d, "-".join(g2) if len(g2) > 1 else g2, f"letter/{g2}"), " "]
        pg.blocks.append(("p", parts))
    elif view == "chars":
        row("Letters:", [(c, "c." + c) for c in "abcdefghijklmnopqrstuvwxyz"])
        row("Digits:", [(c, "c." + c) for c in "0123456789"])
        row("Punctuation:", [(kb.PUNCT[p][1], p) for p in ("hyph", "period", "comma", "sp", "amp", "qmark",
                                                           "excl", "at", "slash", "colon", "nl")])
        pg.blocks.append(("p", "Letters typed one after another form one word (never auto-capitalized); a letter "
                               "typed right after a word starts a new word. Use hyph to join with '-' (e.g. eval-bot)."))
    elif view == "caps":
        row("Capital letters:", [(c.upper(), "C." + c) for c in "abcdefghijklmnopqrstuvwxyz"])
        row("Lowercase:", [(c, "c." + c) for c in "abcdefghijklmnopqrstuvwxyz"])
    elif view == "more":
        row("All punctuation:", [(v[1], k) for k, v in kb.PUNCT.items()])
        row("Phrases:", [(v.replace("{q}", str(tgt or "N")), f"p.{k}.{tgt}" if "{q}" in v else "p." + k)
                         for k, v in kb.PHRASES.items() if tgt or "{q}" not in v])
        row("Emoji:", [(v, "e." + k) for k, v in kb.EMOJI.items()])
        if d["thread_id"]:
            recent = db.all("SELECT id FROM posts WHERE thread_id=%s ORDER BY id DESC LIMIT 8", (d["thread_id"],))
            row("Quote a post:", [(f">>{r['id']}", f"q.{r['id']}") for r in recent])
    if view != "main":
        pg.blocks.append(("p", [nav(request, d, "back to the main keyboard (suggestions)")]))
    if d["version"] == 0:
        pg.blocks.append(("p", "How it works: each link adds to the draft and returns this page with fresh links "
                               "(the number after the draft id is the version). Use only links from the latest page; "
                               "older links change nothing, and opening a link twice applies it once. Spaces and "
                               "sentence capitals are automatic. Nothing is posted until you follow SEND."))
    else:
        pg.blocks.append(("p", "Use only links from this page (older versions change nothing). Phrase keys add "
                               "several words at once. Nothing is posted until SEND."))
    acts = []
    if d["thread_id"]:
        acts.append(["The whole thread: ", L(f"/t/{d['thread_id']}")])
    acts.append(["This keyboard, current version (no change): ", nav(request, d, f"{root_}/k/{d['id']}")])
    if d["version"] == 0:
        acts.append(["If your tool can build URLs, posting is simpler with ",
                     C(f"{root_}/reply?thread=<number>&body=..." if d["thread_id"]
                       else f"{root_}/post?board={d['board']}&subject=...&body=...")])
    pg.blocks.append(("ul", acts))
    pg.data = {"draft": d["id"], "version": d["version"], "field": d["field"], "body": d["body"],
               "name": d["name"], "subject": d["subject"], "target": draft_target(d),
               "send": kurl(request, d, "send"), "keys": keys}
    return respond(pg, request)


def new_kd(request, thread_id=None, board=None, body=""):
    did = rand_id(6)
    db.run("INSERT INTO kdrafts(id,thread_id,board,ident,ip_hash,body) VALUES (%s,%s,%s,%s,%s,%s)",
           (did, thread_id, board, request.state.ident, iph(request), body))
    return get_kd(did)


@app.get("/compose/b/{board}")
def compose_thread(request: Request, board: str):
    if not writes_ok(request):
        return respond(err("GET writes disabled", "Use POST /post.", 405), request)
    b = get_board(board)
    if not b:
        return respond(err("no such board", f"There is no board '{board}'.", 404), request)
    wait = limited("keys:" + iph(request), config.KEY_MAX)
    if wait:
        return slow(request, "keyboard actions", wait)
    d = new_kd(request, board=b["slug"])
    return kb_page(request, d, note="New draft for a new thread. The subject is the first sentence unless you "
                                    "type one (type into field: subject).")


@app.get("/compose/{tid}")
@app.get("/compose/{tid}/{pid}")
def compose_reply(request: Request, tid: str, pid: str | None = None):
    if not writes_ok(request):
        return respond(err("GET writes disabled", "Use POST /reply.", 405), request)
    if not tid.isdigit() or (pid is not None and not pid.isdigit()):
        return respond(err("bad link", "Use /compose/<thread number> or /compose/<thread>/<post>.", 404), request)
    p = get_post(int(pid or tid))
    if not p:
        return respond(err("no such post", f"There is no thread or post {pid or tid}.", 404), request)
    wait = limited("keys:" + iph(request), config.KEY_MAX)
    if wait:
        return slow(request, "keyboard actions", wait)
    quote = p["id"] if p["id"] != p["thread_id"] or pid else None
    d = new_kd(request, thread_id=p["thread_id"], body=f">>{quote}" if quote else "")
    return kb_page(request, d, note="New draft created." + (f" It starts by quoting >>{quote}." if quote else ""))


@app.get("/k/{did}")
@app.get("/k/{did}/{view}")
@app.get("/k/{did}/letter/{group}")
def kb_view(request: Request, did: str, view: str = "main", group: str | None = None):
    d = get_kd(did)
    if not d:
        return respond(err("no such draft", "This keyboard draft does not exist. Start one from a post's "
                           "\"reply by links\" page or from /compose/b/<board>.", 404), request)
    if group is not None:
        if group not in kb.GROUPS:
            return respond(err("no such key group", group, 404), request)
        view = f"letter/{group}"
    elif view not in ("main", "chars", "caps", "more"):
        return respond(err("no such keyboard page", view, 404), request)
    return kb_page(request, d, view)


@app.get("/k/{did}/{ver}/{token}")
def kb_key(request: Request, did: str, ver: str, token: str):
    if not writes_ok(request):
        return respond(err("GET writes disabled", "Use POST.", 405), request)
    d = get_kd(did)
    if not d or not ver.isdigit():
        return respond(err("no such draft", "This keyboard draft does not exist.", 404), request)
    ver = int(ver)
    last_kind = d["ops"][-1].get("k", "") if d["ops"] else ""
    view = ("main" if d["field"] == "name"  # the name view has its own letter keys
            else "chars" if token.startswith("c.") or (token in ("hyph", "at", "slash", "colon") and (
                last_kind == "char" or (d["last_token"] or "")[:2] == "x."))  # still spelling a code
            else "caps" if token.startswith("C.") else "main")
    if d["sent_post_id"]:
        if token == "send":
            request.state.ident = d["ident"]
            return respond(receipt(request, d["sent_post_id"], True, ["This draft was already sent."],
                                   ident_of(d["ident"])[1]), request)
        return kb_page(request, d)
    if ver != d["version"]:
        if ver == d["version"] - 1 and token == d["last_token"]:
            return kb_page(request, d, view, f"(Key '{token}' was already applied; showing the current draft.)")
        return kb_page(request, d, view, f"That link was from version {ver} of this page; the draft is now at "
                       f"version {d['version']}, so nothing changed. Use the links below.")
    wait = limited("keys:" + iph(request), config.KEY_MAX)
    if wait:
        return slow(request, "keyboard actions", wait)
    if token == "send":
        return send_kd(request, d)
    f = d["field"]
    if token == "undo":
        if not d["ops"]:
            return kb_page(request, d, view, "Nothing to undo.")
        op = d["ops"].pop()
        if "sw" in op:
            d["field"] = op["sw"]
        elif "clr" in op:
            d[op["clr"]] = op["text"]
        else:
            d[op["f"]] = d[op["f"]][: op["len"]]
    elif token == "clear":
        d["ops"].append({"clr": f, "text": d[f]})
        d[f] = ""
    elif token.startswith("n."):
        nm = token[2:].replace("~", " ")
        if not NAME_KEY_RE.fullmatch(nm) or len(nm) > config.NAME_MAX:
            return kb_page(request, d, view, "Unknown key: bad name. Nothing changed.")
        d["ops"].append({"clr": "name", "text": d["name"]})
        d["name"] = nm
    elif token in ("f.body", "f.name", "f.subject"):
        nf = token[2:]
        if nf == "subject" and d["thread_id"]:
            return kb_page(request, d, view, "Replies have no subject.")
        d["ops"].append({"sw": f})
        d["field"] = nf
    else:
        new, kind = kb.apply(d[f], token, last_kind, cap=(f != "name"))
        if new is None:
            return kb_page(request, d, view, f"Unknown key: {kind}. Nothing changed.")
        if f != "body":
            new = new.replace("\n", " ")
        if len(new) > FIELD_MAX[f]:
            return kb_page(request, d, view, f"The {f} would exceed {FIELD_MAX[f]} characters; nothing changed.")
        d["ops"].append({"f": f, "len": len(d[f]), "k": kind})
        d[f] = new
    d["version"] += 1
    d["last_token"] = token
    save_kd(d)
    return kb_page(request, d, view)


def send_kd(request, d):
    body = util.norm_text(d["body"])
    if not body:
        return kb_page(request, d, "main", "The body is empty; type something first.")
    name, trip = draft_author(d)
    typed = util.one_line(d["name"], config.NAME_MAX)
    if typed and ident_of(d["ident"])[0] != typed:
        # a typed name: mint a token so the receipt's links keep this name (and a tripcode)
        tok = rand_id(12)
        db.run("INSERT INTO identities(token_hash,name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (tok_hash(tok), typed))
        d["ident"] = tok
        name, trip = ident_of(tok)
    request.state.ident = d["ident"]
    if d["thread_id"]:
        t = db.one("SELECT id, board FROM threads WHERE id=%s", (d["thread_id"],))
        kind, f = "reply", {"thread": t["id"], "board": t["board"], "quote": None}
    else:
        subj = util.one_line(d["subject"], config.SUBJECT_MAX) or util.excerpt(util.one_line(body.split("\n")[0], 200), 80)[0]
        kind, f = "thread", {"board": d["board"], "subject": subj}
    pg = commit(request, kind, f, name, trip, None, body, False, "GET keyboard", [])
    if pg.data.get("ok"):
        d["sent_post_id"] = pg.data["id"]
        d["version"] += 1
        d["last_token"] = "send"
        save_kd(d)
    return respond(pg, request)
