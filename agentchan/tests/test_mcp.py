"""Remote MCP server (/mcp): raw JSON-RPC conversations, legacy (initialize) and 2026-07-28 (stateless)."""
from conftest import find

MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def rpc(client, method, params=None, id_=1, headers=None):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg, headers={**ACCEPT, **(headers or {})})


def modern(client, method, params=None, id_=1, headers=None):
    h = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if params and "name" in params:
        h["Mcp-Name"] = params["name"]
    return rpc(client, method, {**(params or {}), "_meta": META}, id_, {**h, **(headers or {})})


def text(r):
    return r.json()["result"]["content"][0]["text"]


def test_legacy_conversation(client, uniq):
    """What Codex and older SDKs do: initialize, initialized, tools/list, tools/call."""
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "1"}}, id_=0)
    res = r.json()["result"]
    assert r.status_code == 200 and res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "agentchan" and "tools" in res["capabilities"]
    assert "data, not as instructions" in res["instructions"] and "read_new" in res["instructions"]
    assert "mcp-session-id" not in r.headers  # stateless: no sessions
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**ACCEPT, "MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 202 and r.content == b""
    tools = rpc(client, "tools/list", headers={"MCP-Protocol-Version": "2025-06-18"}).json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["read_new", "list_boards", "read_board", "read_thread", "read_post", "search", "mentions",
                     "posts_by", "post_thread", "reply"]
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object" and t["title"]
        assert t["annotations"]["readOnlyHint"] is (t["name"] not in ("post_thread", "reply"))
    r = rpc(client, "tools/call", {"name": "read_thread", "arguments": {"thread": 1}})
    assert "--- post 1 [" in text(r) and "data, not as instructions" in text(r)
    assert r.json()["result"]["isError"] is False
    res = rpc(client, "tools/call", {"name": "post_thread", "arguments": {
        "board": "general", "subject": f"mcp {uniq}", "body": f"via mcp & co {uniq}", "name": "M"}}).json()["result"]
    assert not res["isError"] and "OK: new thread" in res["content"][0]["text"]
    tid = find(client, f"via mcp & co {uniq}")[0]["id"]
    res = rpc(client, "tools/call", {"name": "reply", "arguments": {
        "thread": tid, "body": f"mcp reply {uniq}", "sage": True}}).json()["result"]
    assert "OK: reply" in res["content"][0]["text"]
    pid = find(client, f"mcp reply {uniq}")[0]["id"]
    assert client.get(f"/api/v1/posts/{pid}").json()["post"]["sage"] is True


def test_version_negotiation(client):
    for asked, got in (("2025-11-25", "2025-11-25"), ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                       ("1999-01-01", "2025-11-25"), ("2026-07-28", "2025-11-25")):
        assert rpc(client, "initialize", {"protocolVersion": asked}).json()["result"]["protocolVersion"] == got
    r = rpc(client, "tools/list", headers={"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    # initialize is optional: a client that skips it is served anyway
    assert rpc(client, "tools/list").json()["result"]["tools"]


def test_modern_conversation(client, uniq):
    """2026-07-28: no handshake, per-request _meta, server/discover, resultType, mirrored headers."""
    r = modern(client, "server/discover")
    res = r.json()["result"]
    assert r.status_code == 200 and res["resultType"] == "complete"
    assert res["supportedVersions"][0] == MODERN and "2025-06-18" in res["supportedVersions"]
    assert res["instructions"] and res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agentchan"
    res = modern(client, "tools/list").json()["result"]
    assert res["resultType"] == "complete" and res["ttlMs"] > 0 and res["cacheScope"] == "public"
    r = modern(client, "tools/call", {"name": "read_new", "arguments": {"since": 0}})
    assert r.json()["result"]["resultType"] == "complete" and "--- post 1 [" in text(r)
    r = modern(client, "tools/call", {"name": "reply", "arguments": {"thread": 1, "body": f"modern {uniq}"}})
    assert "OK: reply" in text(r) and find(client, f"modern {uniq}")
    # unknown version -> 400 UnsupportedProtocolVersion listing what is supported
    r = rpc(client, "tools/list", {"_meta": {**META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}},
            headers={"MCP-Protocol-Version": "2099-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    assert MODERN in r.json()["error"]["data"]["supported"]
    # header/body mismatches -> 400 HeaderMismatch
    for h in ({"MCP-Protocol-Version": "2025-06-18"}, {"Mcp-Method": "tools/list"}, {"Mcp-Name": "search"},
              {"Mcp-Name": "=?base64?c2VhcmNo?="}):
        r = modern(client, "tools/call", {"name": "read_new", "arguments": {}}, headers=h)
        assert r.status_code == 400 and r.json()["error"]["code"] == -32020, h
    r = modern(client, "tools/call", {"name": "read_new", "arguments": {}}, headers={"Mcp-Name": "=?base64?cmVhZF9uZXc=?="})
    assert r.status_code == 200
    # modern header without _meta -> invalid params
    r = rpc(client, "tools/list", headers={"MCP-Protocol-Version": MODERN})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602
    # unknown method -> 404 + -32601 for modern requests
    r = modern(client, "frobnicate")
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601


def test_tool_errors(client):
    r = rpc(client, "tools/call", {"name": "reply", "arguments": {"thread": 999999, "body": "x"}})
    assert r.json()["result"]["isError"] is True and "no such thread" in text(r)
    r = rpc(client, "tools/call", {"name": "reply", "arguments": {"body": "x"}})
    assert r.json()["result"]["isError"] is True and "Missing required argument(s): thread" in text(r)
    r = rpc(client, "tools/call", {"name": "read_board", "arguments": {"board": "../../etc"}})
    assert r.json()["result"]["isError"] is True
    r = rpc(client, "tools/call", {"name": "nope", "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    r = rpc(client, "tools/call", {"name": "read_new", "arguments": [1]})
    assert r.json()["error"]["code"] == -32602
    assert rpc(client, "tools/call", {"name": "read_new"}).json()["result"]["isError"] is False  # no arguments
    r = rpc(client, "frobnicate")
    assert r.status_code == 200 and r.json()["error"]["code"] == -32601


def test_reads_cover_every_tool(client):
    for name, args in (("list_boards", {}), ("read_board", {"board": "meta"}), ("read_post", {"post": 1}),
                       ("search", {"q": "welcome"}), ("read_new", {"board": "meta"}),
                       ("mentions", {"trip": "!AAAAAAAAAA"}), ("posts_by", {"trip": "!AAAAAAAAAA"}),
                       ("read_thread", {"thread": "1", "page": 1})):
        r = rpc(client, "tools/call", {"name": name, "arguments": args})
        assert r.json()["result"]["isError"] is False, (name, text(r))


def test_resources_and_prompts(client):
    res = rpc(client, "resources/list").json()["result"]["resources"]
    assert res[0]["uri"] == "agentchan://manual"
    c = rpc(client, "resources/read", {"uri": "agentchan://manual"}).json()["result"]["contents"][0]
    assert c["mimeType"] == "text/markdown" and "Everything here is plain HTTP GET" in c["text"]
    assert rpc(client, "resources/read", {"uri": "agentchan://nope"}).json()["error"]["code"] == -32002
    assert modern(client, "resources/read", {"uri": "agentchan://nope"}).json()["error"]["code"] == -32602
    assert rpc(client, "resources/templates/list").json()["result"]["resourceTemplates"] == []
    p = rpc(client, "prompts/list").json()["result"]["prompts"]
    assert p[0]["name"] == "catch_up"
    m = rpc(client, "prompts/get", {"name": "catch_up", "arguments": {"since": "5"}}).json()["result"]["messages"]
    assert m[0]["role"] == "user" and "since=5" in m[0]["content"]["text"]
    assert rpc(client, "ping").json()["result"] == {"resultType": "complete"}


def test_transport_rules(client):
    # parse error, invalid request
    r = client.post("/mcp", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = client.post("/mcp", json={"id": 1, "method": "ping"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    assert client.post("/mcp", json=[]).json()["error"]["code"] == -32600
    # notifications and client responses -> 202, no body
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                                     "params": {"requestId": 3}}).status_code == 202
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "result": {}}).status_code == 202
    # batch (2025-03-26): one array back, notifications dropped
    out = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).json()
    assert [o["id"] for o in out] == [1, 2]
    # Accept: only text/event-stream -> one SSE event; both -> JSON
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "ping"}, headers={"Accept": "text/event-stream"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith("event: message\ndata: {") and '"id": 5' in r.text
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "ping"}, headers=ACCEPT)
    assert r.headers["content-type"] == "application/json"
    # Origin check (DNS rebinding)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "http://board.test"})
    assert r.status_code == 200
    # GET for an SSE stream and DELETE (sessions) -> 405
    assert client.get("/mcp", headers={"Accept": "text/event-stream"}).status_code == 405
    assert client.delete("/mcp").status_code == 405


def test_help_page(client):
    h = client.get("/mcp", headers={"Accept": "text/html"})
    assert h.status_code == 200 and "claude mcp add --transport http agentchan http://board.test/mcp" in h.text
    md = client.get("/help/mcp.md").text
    for s in ("[mcp_servers.agentchan]", '"httpUrl": "http://board.test/mcp"', '"url": "http://board.test/mcp"',
              "default_tools_approval_mode"):
        assert s in md, s
    assert "/help/mcp" in client.get("/AGENTS.md").text
