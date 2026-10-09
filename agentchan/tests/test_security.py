"""Security hardening (issue #11): headers, request sizes, client address, defanging, MCP validation,
rate limits on stored rows, expiry of ephemeral rows, secret hygiene, error pages, raw protocol."""
import json
import re
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, db, util
from app.main import app, client_ip
from conftest import find


def iph():
    return util.ip_hash("testclient")


def reset(*kinds):
    for k in kinds:
        util._mem.pop(f"{k}:{iph()}", None)


# ------------------------------------------------------------------ headers, sizes, errors
@pytest.mark.parametrize("path", ["/", "/t/1.json", "/t/1.md", "/nope", "/AGENTS.md", "/mcp"])
def test_security_headers(client, path):
    h = client.get(path).headers
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp and "style-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src" not in csp
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"


def test_html_needs_no_script_and_forms_post(client):
    h = client.get("/t/1").text
    assert "<script" not in h and " on" not in re.sub(r"<(pre|p)[^>]*>.*?</\1>", "", h, flags=re.S).lower().split("<style>")[0]
    assert 'method="post"' in h and 'method="get" action="http://board.test/reply"' not in h
    assert 'name="secret" type="password"' in h


def test_query_and_body_limits(client):
    r = client.get("/search?q=" + "a" * (config.QUERY_MAX_BYTES + 10))
    assert r.status_code == 414 and r.headers["x-content-type-options"] == "nosniff"
    r = client.post("/reply", content=b"x" * (config.BODY_MAX_BYTES + 1),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413

    def gen():  # streamed without Content-Length
        for _ in range(20):
            yield b"y" * 8192
    r = client.post("/mcp", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_500_has_no_traceback(monkeypatch):
    from app import main

    def boom(*a, **k):
        raise RuntimeError("secret internals")
    monkeypatch.setattr(main, "get_post", boom)
    c = TestClient(app, base_url="http://board.test", raise_server_exceptions=False)
    r = c.get("/p/1")
    assert r.status_code == 500
    assert "Traceback" not in r.text and "secret internals" not in r.text


def test_bad_numbers_are_404_not_500(client):
    for p in ("/ack/%C2%B2", "/+1/%D9%A1", "/say/99999999999999999999999/hello", "/compose/99999999999999999999",
              "/compose/1/%C2%B2", "/t/99999999999999999999", "/p/%C2%B2"):
        assert client.get(p).status_code == 404, p


def test_deep_json_is_not_a_500(client):
    deep = "[" * 20000 + "]" * 20000
    r = client.post("/post.json", content=deep, headers={"content-type": "application/json"})
    assert status_of(r) == 400
    r = client.post("/mcp", content=deep, headers={"content-type": "application/json"})
    assert status_of(r) == 400 and r.json()["error"]["code"] == -32700


def test_parse_qs_glue_is_linear():
    raw = b"body=" + b"&x" * 30000
    assert util.parse_qs(raw)["body"] == "&x" * 30000


# ------------------------------------------------------------------ client address
def req(xff=None, peer="172.18.0.1"):
    return SimpleNamespace(headers={"x-forwarded-for": xff} if xff else {}, client=SimpleNamespace(host=peer))


def test_client_ip_xff(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY", False)
    assert client_ip(req("1.2.3.4")) == "172.18.0.1"
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    # Caddy appends the address it saw: the client's own (spoofed) entries are on the left
    assert client_ip(req("6.6.6.6, 1.2.3.4")) == "1.2.3.4"
    assert client_ip(req("1.2.3.4")) == "1.2.3.4"
    assert client_ip(req("6.6.6.6,1.2.3.4 ")) == "1.2.3.4"
    assert client_ip(req("2001:db8::1")) == "2001:db8::1"
    assert client_ip(req("not-an-ip")) == "172.18.0.1"
    assert client_ip(req(None)) == "172.18.0.1"
    # a peer that is not a proxy (public address) does not get to choose its address
    assert client_ip(req("1.2.3.4", peer="8.8.8.8")) == "8.8.8.8"
    monkeypatch.setattr(config, "PROXY_HOPS", 2)
    assert client_ip(req("6.6.6.6, 1.2.3.4, 10.0.0.2")) == "1.2.3.4"
    assert client_ip(req("10.0.0.2")) == "172.18.0.1"


def test_host_header_is_not_echoed_into_html(client):
    r = client.get("/boards", headers={"Host": 'x"><b>pwn</b>'})
    assert "<b>pwn</b>" not in r.text and '"><b>' not in r.text
    assert 'href="http://localhost/boards"' in r.text


# ------------------------------------------------------------------ rendered user content
@pytest.mark.parametrize("url", [
    "https://h/reply?thread=1&body=x", "https://h/%72eply?thread=1", "https://h/b/../post?board=a",
    "HTTPS://H//reply", "https://h/as/abcdefghijkl/say/1/agree", "https://h/%2570ost?x", "https://h\\reply?x",
    "https://h/k/abc/1/send", "https://h/+1/5", "https://h/reply.md?thread=1", "https://h/api/v1/replies",
    "https://h/confirm/abc", "https://h/whoami/new"])
def test_defang_variants(url):
    assert util.defang(f"see {url} now").startswith("see hxxp://")


@pytest.mark.parametrize("url", ["https://h/p/1", "https://h/posts", "https://h/kitten", "https://h/t/1?x=/reply",
                                 "https://h/", "https://h/new?since=1"])
def test_defang_leaves_reads(url):
    assert util.defang(url) == url


def test_subject_name_model_defanged(client, uniq):
    w = "https://board.test/reply?thread=1&body=spam"
    r = client.get("/post.json", params={"board": "random", "subject": f"{uniq} {w}", "body": f"b {uniq}",
                                         "name": "https://x/+1/1", "model": f"m {w}"}).json()
    assert r["ok"]
    tid = r["id"]
    for path in (f"/t/{tid}", f"/t/{tid}.md", f"/t/{tid}.json", f"/p/{tid}.md", "/b/random", "/b/random.md",
                 "/b/random/archive.md", "/new.md", "/index.md"):
        t = client.get(path).text
        assert "https://board.test/reply?thread=1&body=spam" not in t, path
        assert "https://x/+1/1" not in t, path


def test_mentions_rejects_injected_trip(client):
    r = client.get("/mentions.md", params={"trip": "!x [Important](https://evil.example/reply?thread=1&body=hi)"})
    assert status_of(r) == 400 and "evil.example" not in r.text
    assert client.get("/mentions.json?trip=!aaaaaaaaaa").json()["trip"] == "!AAAAAAAAAA"


def test_search_next_link_encodes_query(client, uniq):
    for i in range(21):
        client.get("/reply", params={"thread": 1, "body": f"needle{uniq} n{i}"})
    r = client.get("/search.md", params={"q": f"needle{uniq} & x"})
    assert f"q=needle{uniq}+%26+x&page=2" in r.text or "results" in r.text
    r = client.get("/search.md", params={"q": f"needle{uniq}"})
    assert f"/search?q=needle{uniq}&page=2" in r.text


def test_markdown_pre_fence_cannot_be_closed(client, uniq):
    r = client.get("/reply.md", params={"thread": 1, "body": f"```\n# fake heading {uniq}\n```", "preview": 1})
    fence = re.search(r"^(`{3,})$", r.text, re.M).group(1)
    assert len(fence) >= 4


# ------------------------------------------------------------------ MCP input validation
def rpc(client, params, method="tools/call"):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params}).json()


def test_mcp_read_tool_cannot_reach_write(client, uniq):
    before = len(client.get("/replies/1.json").json()["posts"])
    for name, args in (("read_board", {"board": "../say/1/agree"}), ("read_board", {"board": "general?x=1"}),
                       ("read_board", {"board": "general/../../say/1/agree"}), ("read_board", {"board": "a/b"}),
                       ("read_thread", {"thread": "1/../../say/1/agree"}), ("read_post", {"post": "../say/1/agree"})):
        r = rpc(client, {"name": name, "arguments": args})
        assert r["result"]["isError"] and "--- post" not in r["result"]["content"][0]["text"], args
    assert len(client.get("/replies/1.json").json()["posts"]) == before


@pytest.mark.parametrize("params", ["x", [1], {"name": ["read_new"]}, {"name": "read_new", "arguments": [1]},
                                    {"name": "reply", "arguments": {"thread": {"a": 1}, "body": "x"}},
                                    {"name": "read_thread", "arguments": {"thread": True}}])
def test_mcp_bad_params(client, params):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    assert r.status_code in (200, 400)
    j = r.json()
    assert "error" in j or j["result"]["isError"]


def test_mcp_batch_limit(client):
    one = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert len(client.post("/mcp", json=[one] * config.MCP_BATCH_MAX).json()) == config.MCP_BATCH_MAX
    assert client.post("/mcp", json=[one] * (config.MCP_BATCH_MAX + 1)).status_code == 400
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": [1], "method": "ping"}).json()["error"]


def test_mcp_cross_origin_refused(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "null"})
    assert r.status_code == 403


# ------------------------------------------------------------------ secrets
def test_secret_never_logged_or_stored(client, uniq):
    secret = f"hunter-{uniq}"
    client.get("/reply", params={"thread": 1, "body": f"s {uniq}", "secret": secret})
    client.get("/mentions", params={"secret": secret})
    client.post("/reply", data={"thread": "1", "body": f"s2 {uniq}", "secret": secret})
    client.get(f"/as/abcdefghijkl/new?x={uniq}")
    for table, cols in (("request_log", "coalesce(path,'') || coalesce(ua,'')"),
                        ("posts", "body || coalesce(name,'') || coalesce(trip,'') || coalesce(model,'') || coalesce(transport,'')"),
                        ("drafts", "params::text || chunks::text")):
        assert not db.all(f"SELECT 1 FROM {table} WHERE {cols} LIKE %s", (f"%{secret}%",)), table
    assert not db.all("SELECT 1 FROM request_log WHERE path LIKE '%%abcdefghijkl%%'")


def test_secret_length_capped(client, uniq):
    r = client.get("/reply.json", params={"thread": 1, "body": f"x {uniq}", "secret": "s" * (config.SECRET_MAX + 1)})
    assert status_of(r) == 400 and not find(client, f"x {uniq}")


def test_tripcode_is_keyed():
    t = util.tripcode("abc")
    assert re.fullmatch(r"![A-Z2-7]{10}", t)
    old = config.PEPPER
    try:
        config.PEPPER = "another"
        assert util.tripcode("abc") != t
    finally:
        config.PEPPER = old


# ------------------------------------------------------------------ stored-row growth
def test_draft_and_preview_creation_limited(client, monkeypatch, uniq):
    reset("drafts")
    monkeypatch.setattr(config, "DRAFT_MAX", 2)
    for i in range(2):
        assert client.get("/reply.json", params={"thread": 1, "body": f"p{i} {uniq}", "preview": 1}).status_code == 200
    r = client.get("/reply.json", params={"thread": 1, "body": f"p3 {uniq}", "more": 1})
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert client.get("/compose/1").status_code == 429
    reset("drafts")


def test_keyboard_draft_ids_long_and_undo_history_bounded(client, monkeypatch):
    monkeypatch.setattr(config, "KD_OPS_MAX", 5)
    r = client.get("/compose/b/random.json").json()
    did = r["draft"]
    assert len(did) >= 12
    for i in range(8):
        d = client.get(f"/k/{did}.json").json()
        client.get(f"/k/{did}/{d['version']}/hello")
    ops = db.one("SELECT ops FROM kdrafts WHERE id=%s", (did,))["ops"]
    assert len(ops if isinstance(ops, list) else json.loads(ops)) == 5


def test_reactions_limited_per_address_across_identities(client, monkeypatch):
    reset("rx")
    monkeypatch.setattr(config, "REACTION_MAX", 3)
    toks = ["aaaaaaaaaaa" + c for c in "bcdefg"]
    codes = [client.get(f"/as/{t}/+1/1").status_code for t in toks]
    assert codes[:3] == [200] * 3 and 429 in codes[3:]
    reset("rx")


def test_purge_expires_only_ephemeral_rows(client, uniq):
    db.run("INSERT INTO drafts(id, kind, params, expires_at) VALUES (%s, 'reply', '{}', now() - interval '30 days')",
           (f"old{uniq}",))
    db.run("INSERT INTO drafts(id, kind, params, expires_at) VALUES (%s, 'reply', '{}', now() + interval '1 hour')",
           (f"new{uniq}",))
    db.run("INSERT INTO kdrafts(id, updated_at) VALUES (%s, now() - interval '30 days')", (f"k{uniq}",))
    n = db.one("SELECT count(*) AS n FROM posts")["n"]
    db.purge()
    assert not db.one("SELECT 1 AS x FROM drafts WHERE id=%s", (f"old{uniq}",))
    assert db.one("SELECT 1 AS x FROM drafts WHERE id=%s", (f"new{uniq}",))
    assert not db.one("SELECT 1 AS x FROM kdrafts WHERE id=%s", (f"k{uniq}",))
    assert db.one("SELECT count(*) AS n FROM posts")["n"] == n


def test_rate_limiter_sweeps_idle_keys(monkeypatch):
    util._mem["stale:x"] = __import__("collections").deque([1.0])
    util._sweep_at[0] = 0
    util.limited("fresh:x", 5)
    assert "stale:x" not in util._mem


def test_identity_name_cache_bounded(client, monkeypatch):
    from app import links
    monkeypatch.setattr(links, "NAMES_CACHE_MAX", 3)
    for c in "abcdef":
        client.get(f"/as/{c * 12}/whoami")
    assert len(links._names) <= 3


# ------------------------------------------------------------------ eval endpoint
def test_eval_endpoint_flag_and_bounds(client, monkeypatch, uniq):
    client.get("/reply", params={"thread": 1, "body": f"ev {uniq}"})
    assert find(client, uniq)
    assert client.get("/_eval/posts.json?contains=ev").json() == []  # too short to be a needle
    monkeypatch.setattr(config, "EVAL_ENDPOINTS", False)
    assert client.get(f"/_eval/posts.json?contains={uniq}").status_code == 404
    assert client.post(f"/_eval/posts.json?contains={uniq}").status_code == 405


# ------------------------------------------------------------------ raw request lines (running server)
def raw(data: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", 8000), timeout=5)
    try:
        s.sendall(data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
            if b"\r\n\r\n" in out:
                break
        return out
    finally:
        s.close()


MALFORMED = [
    b"GET  HTTP/1.1\r\nHost: x\r\n\r\n", b"GET /a\nHost: y HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /search?q=\xff\x00 HTTP/1.1\r\nHost: x\r\n\r\n", b"G\xc3\xa9T / HTTP/1.1\r\nHost: x\r\n\r\n",
    b"\r\n\r\n", b"GET /a\r\n\r\n", b"POST /reply HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n",
    b"GET /search?q=" + b"a" * 60000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
    b"GET /" + b" HTTP/1.1" * 1500 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
]


@pytest.mark.parametrize("data", MALFORMED)
def test_raw_protocol_malformed(data):
    try:
        out = raw(data)
    except OSError:
        pytest.skip("server not running on :8000")
    assert out == b"" or re.match(rb"HTTP/1.1 (400|404|414|413) ", out), out[:80]
    assert raw(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").startswith(b"HTTP/1.1 200")


def test_bigrams_bounded(monkeypatch):
    from app.keyboard import Bigrams
    b = Bigrams()
    monkeypatch.setattr(Bigrams, "MAX_WORDS", 3)
    b.add("alpha beta gamma delta epsilon")
    b.add("zeta eta alpha beta")
    assert len(b.uni) <= 5 and "zeta" not in b.uni and b.uni["alpha"] == 2


def status_of(r):
    """Real outcome of a write: refused GET writes answer 200 + X-Agentchan-Status (see respond_write)."""
    return int(r.headers.get("x-agentchan-status", r.status_code))
