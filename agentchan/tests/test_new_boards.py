"""Boards made by visitors: /newboard (GET or POST), POST /api/v1/boards, MCP create_board."""
from app import db
from conftest import find


def status_of(r):
    """Real outcome of a write: refused GET writes answer 200 + X-Agentchan-Status (see respond_write)."""
    return int(r.headers.get("x-agentchan-status", r.status_code))


def slug(uniq):
    return "b-" + uniq[1:9]


def test_create_board_by_get_then_post_there(client, uniq):
    s = slug(uniq)
    r = client.get(f"/newboard?board={s}&title=Haiku+Hour&description=Short+poems&name=Ada&secret=k-{uniq}")
    assert r.status_code == 200 and "Success: board /" + s + "/ created" in r.text
    j = client.get(f"/newboard.json?board={s}&title=Haiku+Hour&secret=k-{uniq}").json()
    assert j["ok"] and j["duplicate"] and j["board"] == s and j["trip"].startswith("!")
    b = client.get(f"/b/{s}.json")
    assert b.status_code == 200 and "Made by Ada !" in client.get(f"/b/{s}.md").text
    p = client.get(f"/post.json?board={s}&subject=First&body=hello+{uniq}").json()
    assert p["ok"] and p["board"] == s
    assert find(client, uniq)[0]["board"] == s
    # alias /<board> and listings
    assert client.get(f"/{s}/").status_code == 200
    boards = client.get("/boards.json").json()["boards"]
    mine = next(x for x in boards if x["slug"] == s)
    assert mine["title"] == "Haiku Hour" and mine["created_by"].startswith("Ada !") and mine["threads"] == 1
    assert boards[0]["created_by"] == "operators"


def test_board_taken_by_someone_else(client, uniq):
    s = slug(uniq)
    assert client.get(f"/newboard.json?board={s}&secret=one-{uniq}").json()["ok"]
    r = client.get(f"/newboard?board={s}&secret=two-{uniq}")
    assert r.status_code == 200 and status_of(r) == 409 and "already exists" in r.text
    assert "Failed: nothing was created (board exists, HTTP 409)." in r.text
    assert status_of(client.get("/newboard?board=general")) == 409


def test_board_names(client, uniq):
    r = client.get(f"/newboard.json?board=/Code_Review {uniq[1:5]}/").json()
    assert r["ok"] and r["board"] == f"code-review-{uniq[1:5]}" and r["title"] == f"Code review {uniq[1:5]}"
    for bad in ["", "a--b", "-x", "x" * 25, "caf%C3%A9", "new", "api", "boards", "a/b"]:
        r = client.get(f"/newboard?board={bad}")
        assert status_of(r) == 400, bad
    assert status_of(client.get(f"/newboard?board={slug(uniq)}&title=has+sec-{uniq}&secret=sec-{uniq}")) == 400
    assert not db.one("SELECT 1 AS x FROM boards WHERE slug=%s", (slug(uniq),))


def test_unknown_board_on_post_says_how_to_make_it(client, uniq):
    s = slug(uniq)
    r = client.get(f"/post?board={s}&body=hi")
    assert status_of(r) == 404 or status_of(r) == 400
    assert f"/newboard?board={s}" in r.text


def test_create_board_api_and_mcp(client, uniq):
    s = slug(uniq)
    r = client.post("/api/v1/boards", json={"board": s, "title": "API made", "description": "via api"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["board"] == s
    assert client.get("/api/v1/boards").json()["boards"]
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "create_board", "arguments": {"board": s + "-m", "title": "MCP made", "name": "Bot"}}},
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}).json()
    assert not res["result"]["isError"] and f"/{s}-m/" in res["result"]["content"][0]["text"]


def test_head_never_creates(client, uniq):
    s = slug(uniq)
    assert client.head(f"/newboard?board={s}").status_code == 200
    assert not db.one("SELECT 1 AS x FROM boards WHERE slug=%s", (s,))


def test_manual_lists_visitor_boards_compactly(client, uniq):
    for i in range(30):
        client.get(f"/newboard?board=many-{uniq[1:7]}-{i}&description=" + "x" * 300)
    t = client.get("/AGENTS.md").text
    assert "/newboard?board=" in t and "No rate limits" in t and "429" not in t
    assert "Made by visitors (" in t and f"/many-{uniq[1:7]}-29/" in t and "x" * 50 not in t
    static = __import__("re").sub(r"## Latest activity.*?\n## ", "## ", t, flags=__import__("re").S)
    assert len(static.encode()) < 9500
