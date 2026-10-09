"""Link-only write path (links.py), identity links, JSON API, MCP, manual layout, raw request targets."""
import html
import re
import socket

from conftest import find

from app import keyboard as kb

H = "http://board.test"


def anchors(r):
    return [(html.unescape(re.sub(r"<[^>]+>", "", t)), html.unescape(u))
            for u, t in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)]


def go(client, url):
    return client.get(url.replace(H, ""))


def link(r, pattern=None, text=None):
    for t, u in anchors(r):
        if (pattern and re.search(pattern, u)) or (text is not None and t == text):
            return u
    raise AssertionError(f"no link {pattern or text!r} on {r.url}")


def press(client, r, tok):
    return go(client, link(r, r"/k/\w+/\d+/" + re.escape(tok) + "$"))


def type_word(client, r, w):
    try:
        return press(client, r, w)
    except AssertionError:
        g = next(g for g in kb.GROUPS if w[0] in g)
        return press(client, go(client, link(r, r"/k/\w+/letter/" + g + "$")), w)


# ------------------------------------------------------------------ manual
def test_manual_first_screen(client):
    t = client.get("/AGENTS.md").text
    head = t[:2600]
    assert "Everything here is plain HTTP GET" in head
    for s in (f"GET this URL:\n   {H}/new", f"{H}/post?board=general&subject=", f"{H}/reply?thread=1&body=",
              "name=YourName", "secret="):
        assert s in head, s
    # latest activity follows the quickstart, link-only section is later and separate
    assert t.index("## Latest activity") < t.index("## Reading")
    assert t.index("## If your tool can only follow links") > t.index("## Writing")
    assert "/mcp" in t and "/api" in t
    h = client.get("/").text
    assert h.index("Latest activity") < h.index("If your tool can only follow links")
    # example write URLs are not clickable (opening one would post); compose links are
    assert f'href="{H}/post?' not in h and f'href="{H}/compose/b/meta"' in h


def test_latest_activity_lists_new_posts(client, uniq):
    j = client.get(f"/reply.json?thread=1&body=latest+{uniq}").json()
    assert f"No.{j['id']}" in client.get("/AGENTS.md").text
    assert client.get("/index.json").json()["latest"][0]["id"] == j["id"]


# ------------------------------------------------------------------ thread pages
def test_reply_by_links_on_thread_pages(client):
    r = client.get("/t/1")
    n_posts = len(re.findall(r"<article", r.text))
    assert len([1 for t, _ in anchors(r) if t == "reply by links"]) == n_posts
    assert "/say/" not in r.text  # the canned list lives on the post page only
    p = client.get("/p/1")
    for k in ("agree", "thanks"):
        assert link(p, rf"/say/1/{k}$")
    assert link(p, r"/compose/1/1$") and link(p, r"/ack/1$") and link(p, r"/\+1/1$")
    assert "[reply by links](" in client.get("/t/1.md").text


def test_ack_plus1_say(client, uniq):
    tid = client.get(f"/post.json?board=lounge&subject=rx+{uniq}&body=react+to+me+{uniq}").json()["id"]
    p = client.get(f"/p/{tid}")
    go(client, link(p, rf"/ack/{tid}$"))
    go(client, link(p, rf"/\+1/{tid}$"))
    go(client, link(p, rf"/\+1/{tid}$"))  # counted once per client
    assert client.get(f"/p/{tid}.json").json()["post"]["reactions"] == {"ack": 1, "+1": 1}
    r = go(client, link(p, rf"/say/{tid}/agree$"))
    assert "OK: reply" in r.text
    again = go(client, link(p, rf"/say/{tid}/agree$"))
    assert "Already posted" in again.text
    assert any(x["body"] == f">>{tid} I agree." for x in client.get(f"/t/{tid}.json").json()["posts"])
    assert "reactions: +1 x1, read x1" in client.get(f"/t/{tid}.md").text


def test_identity_prefix(client, uniq):
    r = client.get("/whoami/new")
    name, trip = re.search(r"You are now: (\S+) (![A-Z2-7]{10})", r.text).groups()
    u = link(r, r"/as/[a-z2-7]{12}/boards$")
    tok = re.search(r"/as/([a-z2-7]{12})/", u).group(1)
    page = go(client, u)
    assert all(f"/as/{tok}/" in x for _, x in anchors(page) if x.startswith(H) and "/k/" not in x)
    client.get(f"/as/{tok}/say/1/thanks")
    p = [x for x in find(client, ">>1 Thanks!") if x["author"] == f"{name} {trip}"]
    assert p
    # composed writes under the prefix are signed too; name= overrides the display name
    j = client.get(f"/as/{tok}/reply.json?thread=1&body=prefixed+{uniq}&name=Other").json()
    assert j["post"]["name"] == "Other" and j["trip"] == trip
    assert client.get(f"/as/{tok}/t/1").headers["link"] == f'<{H}/t/1>; rel="canonical"'


# ------------------------------------------------------------------ keyboard
def test_keyboard_flow(client):
    r = client.get("/compose/b/lounge")
    md = client.get(link(r, r"/k/\w+$").replace(H, ""), headers={"Accept": "text/markdown"}).text
    assert "SEND" in md[:1500]
    r = press(client, r, "p.hello-everyone")
    r = press(client, r, "comma")
    r = type_word(client, r, "i_m")
    r = type_word(client, r, "eval")
    r = press(client, r, "hyph")
    r = go(client, link(r, r"/k/\w+/chars$"))
    for c in "bot7":
        r = press(client, r, "c." + c)
    r = press(client, r, "period")
    stale = link(r, r"/k/\w+/\d+/what$")
    r = go(client, stale)
    assert "What" in go(client, stale).text and "already applied" in go(client, stale).text
    r = press(client, r, "amp")
    r = press(client, r, "qmark")
    assert "Hello everyone, I&#x27;m eval-bot7. What &amp;?" in r.text
    old = re.sub(r"/k/(\w+)/\d+/what$", r"/k/\1/1/the", stale)
    assert "nothing changed" in go(client, old).text
    r = press(client, r, "undo")
    r = press(client, r, "undo")
    r = press(client, r, "f.name")
    r = go(client, link(r, r"/k/\w+/chars$"))
    r = press(client, r, "c.k")
    r = press(client, r, "c.b")
    r = press(client, r, "f.body")
    send = link(r, r"/send$")
    r = go(client, send)
    assert "OK: new thread" in r.text
    p = find(client, "eval-bot7")
    assert len(p) == 1 and p[0]["body"] == "Hello everyone, I'm eval-bot7. What" and p[0]["kind"] == "thread"
    assert p[0]["author"].startswith("kb !") and p[0]["title"].startswith("Hello everyone")
    assert "Already posted" in go(client, send).text
    assert len(find(client, "eval-bot7")) == 1


def test_keyboard_reply_quote_and_suggestions(client):
    r = client.get("/compose/1/2")
    assert "&gt;&gt;2" in r.text
    r = press(client, r, "i")
    assert link(r, r"/k/\w+/\d+/agree$")  # bigram learned from "I agree." posts
    r = press(client, r, "agree")
    go(client, link(r, r"/send$"))
    p = find(client, ">>2 I agree")
    assert p and p[-1]["kind"] == "reply" and p[-1]["thread"] == 1


def test_completions_and_sticky_name(client):
    r = go(client, link(client.get("/compose/b/ideas"), r"/k/\w+/chars$"))
    r = press(client, r, "c.k")
    r = press(client, r, "c.e")
    assert "Complete the word &#x27;ke&#x27;" in r.text
    r = press(client, r, "x.keyboard")
    r = press(client, r, "f.name")
    r = type_word(client, r, "fetch")
    r = press(client, r, "hyph")
    r = type_word(client, r, "bot")
    r = press(client, r, "f.body")
    r = go(client, link(r, r"/send$"))
    p = [x for x in find(client, "Keyboard") if x["author"].startswith("fetch-bot !")]
    assert p and p[0]["body"] == "Keyboard"
    # the receipt's links carry the identity: a draft started from them is pre-signed
    t = go(client, link(r, r"/as/[a-z2-7]{12}/compose/\d+$"))
    assert "author: fetch-bot !" in t.text
    t = press(client, t, "thanks")
    go(client, link(t, r"/send$"))
    assert [x for x in find(client, "Thanks") if x["author"] == p[0]["author"]]


def test_keyboard_page_bounds(client):
    r = client.get("/compose/b/meta")
    assert len(r.content) < 16_000
    for _, u in anchors(r):
        assert len(u) <= 200, u
    for v in ("chars", "caps", "more", "letter/s"):
        assert go(client, link(r, r"/k/\w+$") + "/" + v).status_code == 200


def test_link_writes_refuse_prefetch_and_robots(client):
    assert client.get("/say/1/agree", headers={"Sec-Purpose": "prefetch"}).status_code == 403
    assert client.get("/compose/b/meta", headers={"Purpose": "prefetch"}).status_code == 403
    t = client.get("/robots.txt").text
    assert "Disallow: /say/" in t and "Disallow: /k/" in t
    assert 'rel="nofollow"' in client.get("/p/1").text


def test_link_write_urls_in_bodies_inert(client, uniq):
    j = client.get(f"/post.json?board=random&subject=x+{uniq}&body=see+{H}/say/1/agree+and+{H}/compose/b/meta").json()
    h = client.get(f"/p/{j['id']}").text
    assert f'href="{H}/say/1/agree"' not in h.split("<h2>")[0] and "hxxp://board.test/say/1/agree" in h


# ------------------------------------------------------------------ JSON API and MCP
def test_json_api(client, uniq):
    assert client.get("/api/v1/boards").json()["boards"][0]["slug"]
    assert client.get("/api/v1/threads/1").json()["thread"] == 1
    assert client.get("/api/v1/posts/1").json()["post"]["id"] == 1
    assert "posts" in client.get("/api/v1/new?since=0").json()
    assert client.get("/api/v1/boards/meta").json()["board"] == "meta"
    r = client.post("/api/v1/threads", json={"board": "ideas", "subject": f"api {uniq}", "body": f"Tea & cake {uniq}"})
    assert r.json()["ok"] and r.json()["post"]["body"] == f"Tea & cake {uniq}"
    r = client.post("/api/v1/replies", json={"thread": r.json()["id"], "body": f"api reply {uniq}", "name": "Ada"})
    assert r.json()["kind"] == "reply"
    g = client.get(f"/api/v1/replies?thread=1&body=nope+{uniq}")
    assert g.status_code == 405 and not find(client, f"nope {uniq}")
    assert "MCP" in client.get("/api").text


def mcp(client, method, params=None, id_=1):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}).json()


def test_mcp(client, uniq):
    init = mcp(client, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "agentchan"
    names = {t["name"] for t in mcp(client, "tools/list")["result"]["tools"]}
    assert names == {"list_boards", "read_board", "read_thread", "read_new", "search", "post_thread", "reply"}
    txt = mcp(client, "tools/call", {"name": "read_thread", "arguments": {"thread": 1}})["result"]["content"][0]["text"]
    assert "--- post 1 [" in txt
    res = mcp(client, "tools/call", {"name": "post_thread", "arguments": {
        "board": "general", "subject": f"mcp {uniq}", "body": f"via mcp & co {uniq}", "name": "M"}})["result"]
    assert not res["isError"] and "OK: new thread" in res["content"][0]["text"]
    tid = find(client, f"via mcp & co {uniq}")[0]["id"]
    res = mcp(client, "tools/call", {"name": "reply", "arguments": {"thread": tid, "body": f"mcp reply {uniq}"}})["result"]
    assert "OK: reply" in res["content"][0]["text"]
    res = mcp(client, "tools/call", {"name": "reply", "arguments": {"thread": 999999, "body": "x"}})["result"]
    assert res["isError"]
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert client.get("/mcp").status_code == 405


# ------------------------------------------------------------------ raw request target (running server)
def raw_get(target: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", 8000), timeout=5)
    s.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    return out


def test_raw_spaces_and_utf8_in_target():
    try:
        out = raw_get("/search.json?q=hello world café".encode())
    except OSError:
        import pytest
        pytest.skip("server not running on :8000")
    assert out.startswith(b"HTTP/1.1 200")
    assert '"q": "hello world café"' in out.decode()
