"""Link-only write path (links.py), identity links, JSON API, MCP, manual layout, raw request targets."""
import html
import re
import socket

from conftest import find

from app import config
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


def key(client, r, tok):
    """Press a key that is not printed on the page (same draft and version as its SEND link)."""
    return go(client, link(r, r"/send$")[:-4] + tok)


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
    p = [x for x in find(client, "keyboard") if x["author"].startswith("fetch-bot !")]
    assert p and p[0]["body"] == "keyboard"  # typed letters are never auto-capitalized (identifiers, nonces)
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


# ------------------------------------------------------------------ JSON API (MCP: test_mcp.py)
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
    assert "MCP" in client.get("/api").text and "/help/mcp" in client.get("/api.md").text
    r = client.post("/api/v1/replies", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "invalid JSON"
    r = client.post("/api/v1/replies", json={"thread": 1, "body": f"no sage {uniq}", "sage": False})
    assert r.json()["post"]["sage"] is False


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


def test_keyboard_receipt_offers_signed_replies(client, uniq):
    r = client.get("/compose/b/lounge")
    r = type_word(client, r, "hello")
    r = press(client, r, "f.name")
    r = type_word(client, r, "fetch")
    r = press(client, r, "f.body")
    r = type_word(client, r, "there")
    r = go(client, link(r, r"/send$"))
    u = link(r, r"/as/[a-z2-7]{12}/compose/\d+/\d+$")
    assert "author: fetch !" in go(client, u).text


# ------------------------------------------------------------------ round 2: secrets, excerpts, board pages
def test_secret_in_post_rejected(client, uniq):
    s = "hunter2-" + uniq
    for url in (f"/post?board=general&subject=x+{uniq}&body=my+secret+is+{s}&secret={s}",
                f"/post?board=general&subject={s}&body=body+{uniq}&secret={s}",
                f"/reply?thread=1&body=hi+{uniq}&name={s}&secret={s}",
                f"/reply?thread=1&body=preview+{s}&secret={s}&preview=1"):
        r = client.get(url)
        assert r.status_code == 400 and "contains your secret; nothing was posted" in r.text, url
    r = client.post("/api/v1/replies", json={"thread": 1, "body": f"api {s}", "secret": s})
    assert r.status_code == 400 and r.json()["error"] == "secret in post"
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "reply", "arguments": {"thread": 1, "body": f"mcp {s}", "secret": s}}}).json()["result"]
    assert res["isError"] and "contains your secret" in res["content"][0]["text"]
    # draft: the secret arrives in a later piece; caught at send time
    d = client.get(f"/reply.json?thread=1&body=part+one+{uniq}+&secret={s}&more=1").json()
    client.get(f"/draft/{d['draft']}/add?text={s}&part=2")
    r = client.get(f"/draft/{d['draft']}/send")
    assert r.status_code == 400 and "contains your secret" in r.text
    assert not find(client, s)
    # short secrets and secrets elsewhere are fine
    assert client.get(f"/reply.json?thread=1&body=abc+fine+{uniq}&secret=abc").json()["ok"]
    assert client.get(f"/reply.json?thread=1&body=Hunter2-{uniq[1:]}&secret={s}").json()["ok"]


def test_excerpts_marked_and_full_text_on_post_page(client, uniq):
    long = ("word " * 600 + "END" + uniq).strip()
    j = client.get("/post.json", params={"board": "lounge", "subject": f"long {uniq}", "body": long}).json()
    pid = j["id"]
    note = f"[…cut — full text: {H}/p/{pid}]"
    for path in ("/b/lounge.md", f"/new.md?since={pid - 1}", "/AGENTS.md", f"/search.md?q=END{uniq}"):
        t = client.get(path).text
        assert note in t, path
        assert "Excerpts here are shortened; open /p/<number> for the full text." in t, path
    assert note in client.get("/b/lounge").text.replace("<a href=\"" + H + f"/p/{pid}\">", "").replace("</a>]", "]")
    bj = client.get("/b/lounge.json").json()
    op = [t for t in bj["threads"] if t["id"] == pid][0]["op"]
    assert op["cut"] and op["body"].endswith(note)
    assert client.get(f"/p/{pid}.json").json()["post"]["body"] == long
    assert f"END{uniq}" in client.get(f"/p/{pid}.md").text


def test_board_page_authors_and_size(client, uniq, monkeypatch):
    big = "x" * 3980
    for i in range(12):
        tid = client.get("/post.json", params={"board": "random", "subject": f"big {i} {uniq}", "name": f"op{i}",
                                               "secret": f"sec{i}{uniq}", "body": big}).json()["id"]
        for k in range(3):
            client.get("/reply.json", params={"thread": tid, "name": f"r{k}", "body": f"{k}{uniq}" + big})
    t = client.get("/b/random.md").text
    assert len(t.encode()) < 20_500
    assert "| op11 !" in t and "| r2 ---" in t
    h = client.get("/b/random").text
    assert "op11 !" in h and len(h.encode()) < 32_000
    j = client.get("/b/random.json")
    assert len(j.content) < 32_000 and j.json()["threads"][0]["last_replies"][0]["name"].startswith("r")


# ------------------------------------------------------------------ keyboard: phrases, context, sign-as (#16)
def test_keyboard_phrase_keys_and_context(client, uniq):
    tid = client.get(f"/post.json?board=lounge&subject=ctx+{uniq}&body=Prefetching+crawlers+break+GET+writes"
                     f"+on+message+boards.+{uniq}").json()["id"]
    r = client.get(f"/compose/{tid}/{tid}")
    md = client.get(link(r, r"/k/\w+$").replace(H, ""), headers={"Accept": "text/markdown"}).text
    head = md[:3500]
    assert "SEND" in head and "Start a sentence:" in head
    assert "Phrases from the post you are replying to" in md and re.search(r"\[prefetching crawlers[^]]*\]", md, re.I)
    assert re.search(r"\[[^]]*GET writes[^]]*\]\(\S+GET~writes\S*\)", md)  # acronyms keep their case
    assert f"Replying to >>{tid}" in md
    r = press(client, r, "p.agree-because")
    r = key(client, r, "prefetching~crawlers")
    r = key(client, r, "w.break")
    r = key(client, r, "GET~writes")
    r = press(client, r, "period")
    assert f"&gt;&gt;{tid} I agree because prefetching crawlers break GET writes." in r.text
    r = press(client, r, "undo")
    r = press(client, r, "undo")
    assert "crawlers break</pre>" in r.text.replace("\n", "")
    r = go(client, link(r, r"/send$"))
    assert find(client, f"I agree because prefetching crawlers break")


def test_keyboard_quote_phrase_and_codes(client, uniq):
    code = "code-" + uniq + "9"
    r = client.get("/compose/b/lounge")
    r = press(client, r, "p.hi-all")
    assert "Hi all," in r.text
    r = key(client, r, "w." + code)  # identifiers keep their case and are never capitalized
    r = press(client, r, "f.name")
    r = type_word(client, r, "fetch")
    r = press(client, r, "hyph")
    r = press(client, r, "c.q")
    r = press(client, r, "f.body")
    r = go(client, link(r, r"/send$"))
    p = find(client, code)
    assert p[0]["body"] == f"Hi all, {code}" and p[0]["author"].startswith("fetch-q !")
    # the receipt's signed reply link: codes from earlier posts and the reply starters are one key
    t = go(client, link(r, r"/as/[a-z2-7]{12}/compose/\d+/\d+$"))
    q = int(re.search(r"/compose/\d+/(\d+)$", link(r, r"/as/[a-z2-7]{12}/compose/\d+/\d+$")).group(1))
    assert "Codes and names from your earlier posts:" in t.text
    t = press(client, t, "p.good-question")
    t = press(client, t, code)
    t = press(client, t, "period")
    go(client, link(t, r"/send$"))
    assert find(client, f">>{q} Good question. {code}.")
    # a draft that does not quote yet offers "I agree with >>N because"
    t = client.get(f"/compose/{p[0]['id']}")
    assert link(t, rf"/p\.agree-with\.{p[0]['id']}$")
    t = press(client, t, f"p.agree-with.{p[0]['id']}")
    assert f"I agree with &gt;&gt;{p[0]['id']} because" in t.text


def test_keyboard_sign_as_and_letters(client, uniq):
    r = client.get("/compose/b/lounge")
    r = type_word(client, r, "hello")
    r = press(client, r, "f.name")
    r = press(client, r, "c.z")
    r = press(client, r, "c.q")
    r = press(client, r, "hyph")
    r = press(client, r, "c.k")  # the name view keeps its letter keys
    r = press(client, r, "f.body")
    r = key(client, r, "w." + uniq)
    go(client, link(r, r"/send$"))
    # a new draft from the same address offers that name as one key
    r = client.get("/compose/b/lounge")
    r = press(client, r, "n.zq-k")
    assert "author: zq-k" in r.text
    r = press(client, r, "undo")
    assert "author: Anonymous" in r.text
    r = press(client, r, "n.zq-k")
    # letters typed one by one are never capitalized, and '-' after a completion stays on the letter page
    r = go(client, link(r, r"/k/\w+/chars$"))
    r = press(client, r, "c.n")
    r = press(client, r, "c.o")
    r = press(client, r, "x.nonce")
    r = press(client, r, "hyph")
    assert link(r, r"/k/\w+/\d+/c\.a$") and "Letters:" in r.text
    r = press(client, r, "c.7")
    r = press(client, r, "c.x")
    r = go(client, link(r, r"/send$"))
    p = find(client, "nonce-7x")
    assert p and p[-1]["body"].startswith("nonce-7x") and p[-1]["author"].startswith("zq-k")


def test_keyboard_page_size_with_long_root(client, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://agentchan.example-board.xyz")
    for start in ("/compose/b/general", "/compose/1/2"):
        r = client.get(start)
        md = client.get(link(r, r"/k/\w+$").replace("https://agentchan.example-board.xyz", ""),
                        headers={"Accept": "text/markdown"}).text
        assert len(md.encode()) < 9000, len(md)
        assert md.index("SEND") < 1500 and md.index("Next word:") < 2500
        for _, u in anchors(r):
            assert len(u) <= 200, u


def test_keyboard_model_units():
    m = kb.Bigrams()
    m.add("The AI agents use tools. AI agents read posts. I think rate limits matter. "
          "I think rate limits help. I think rate limits work. nonce-ab12 here.", "eval-bot")
    assert m.display("ai") == "AI" and m.display("think") == "think"
    assert "nonce-ab12" in m.uni and m.chain("think", "rate") == ["rate", "limits"]
    assert m.name_words("eval", 3)[0] == "bot" and "claude" in m.name_words("", 30)
    ph, ws = kb.context_terms(["Plain GET writes are risky for prefetchers & crawlers. nonce-zz99"])
    assert ["GET", "writes"] in ph or any("GET" in p for p in ph)
    assert "GET" in ws and "nonce-zz99" not in ws  # other people's codes are not offered
    assert kb.apply("Hi.", "here~is~nonce-ab12", "")[0] == "Hi. Here is nonce-ab12"
    assert kb.apply("", "nonce-ab12", "")[0] == "nonce-ab12"
    assert kb.apply(">>3", "p.agree-with.3", "")[0] == ">>3 I agree with >>3 because"
    assert kb.apply("", "p.agree-with", "")[0] is None
    assert kb.apply("", "c.n", "")[0] == "n"
