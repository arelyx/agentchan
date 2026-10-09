"""Operations: UA families, request-log rollup and retention, /stats, /_admin/traffic, /healthz."""
from datetime import timedelta

import pytest
from conftest import find

from app import config, db, ops, util

CLAUDE = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)"
CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


@pytest.mark.parametrize("ua,family,group", [
    (CLAUDE, "Claude-User", "agent fetcher"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot",
     "ChatGPT-User", "agent fetcher"),
    ("GeminiCLI/0.9 Google-Gemini-CLI", "Google-Gemini-CLI", "agent fetcher"),
    ("codex_cli_rs/0.40.0 (Mac OS 15.5.0; arm64)", "codex", "agent tool or library"),
    ("curl/8.5.0", "curl", "agent tool or library"),
    ("python-requests/2.32.3", "python-requests", "agent tool or library"),
    ("ModelContextProtocol/1.0 (Autonomous; +https://github.com/modelcontextprotocol/servers)",
     "ModelContextProtocol", "agent tool or library"),
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "Googlebot", "crawler"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0)", "ClaudeBot", "crawler"),
    ("Mozilla/5.0 (compatible; SomeNewCrawler/0.1)", "other bot", "crawler"),
    (CHROME, "browser", "browser"),
    ("", "(none)", "other"),
    ("weird-thing/1", "other", "other"),
])
def test_ua_family_and_group(ua, family, group):
    assert util.ua_family(ua) == family
    assert util.ua_group(family) == group


def _clean():
    for t in ("request_log", "request_days", "request_daily"):
        db.run(f"DELETE FROM {t}")
    ops.clear_cache()


def _log(ts, path, ua, ip="a", status=200, method="GET"):
    db.run("INSERT INTO request_log(ts, method, path, qlen, ua, ip_hash, status, ms) VALUES (%s,%s,%s,0,%s,%s,%s,1)",
           (ts, method, path, ua, ip, status))


def test_rollup_and_retention():
    _clean()
    today = ops.utc_today()
    t = lambda days, h=12: ops.day_start(today - timedelta(days=days)) + timedelta(hours=h)  # noqa: E731
    _log(t(40), "/", CLAUDE, "a")
    _log(t(40, 13), "/t/1.md", CLAUDE, "a")
    _log(t(40, 14), "/post", "curl/8", "b", status=429)
    _log(t(35), "/mcp", "x (via mcp)", "c", method="POST")
    _log(t(2), "/say/1/hello", CHROME, "d")
    _log(t(2), "/k/abcdef/3/w.hi", CHROME, "d")
    _log(t(1), "/api/v1/threads", "python-requests/2", "e", method="POST")
    _log(t(1), "/AGENTS.md", CLAUDE, "a")
    _log(t(1), "/nope", "Mozilla/5.0 (compatible; Googlebot/2.1)", "f", status=404)
    _log(t(0, 0), "/b/general", CLAUDE, "a")

    assert ops.prune(today) == 0  # nothing rolled up yet: nothing is deleted
    assert ops.rollup(today) == 40  # days 40 .. 1, empty days included
    assert ops.rollup(today) == 0  # idempotent
    days = {r["day"]: r for r in db.all("SELECT * FROM request_days")}
    assert today not in days and len(days) == 40
    assert (days[today - timedelta(days=40)]["requests"], days[today - timedelta(days=40)]["clients"]) == (3, 2)
    assert days[today - timedelta(days=10)]["requests"] == 0
    daily = {(r["day"], r["family"], r["area"], r["status"]): (r["requests"], r["clients"])
             for r in db.all("SELECT * FROM request_daily")}
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    assert daily[(d(40), "Claude-User", "manual", 2)] == (1, 1)
    assert daily[(d(40), "Claude-User", "read", 2)] == (1, 1)
    assert daily[(d(40), "curl", "write", 4)] == (1, 1)
    assert daily[(d(35), "other", "mcp", 2)] == (1, 1)
    assert daily[(d(2), "browser", "link write", 2)] == (1, 1)
    assert daily[(d(2), "browser", "keyboard", 2)] == (1, 1)
    assert daily[(d(1), "python-requests", "api", 2)] == (1, 1)
    assert daily[(d(1), "Claude-User", "manual", 2)] == (1, 1)
    assert daily[(d(1), "Googlebot", "read", 4)] == (1, 1)

    assert ops.prune(today) == 4  # days 40 and 35 are past the 30-day window
    assert db.one("SELECT count(*) AS n FROM request_log WHERE ts < %s", (t(30),))["n"] == 0
    assert db.one("SELECT count(*) AS n FROM request_log WHERE ts >= %s", (t(30),))["n"] >= 6
    assert db.one("SELECT requests FROM request_days WHERE day = %s", (d(40),))["requests"] == 3  # kept

    # a day already rolled up can be recomputed without double counting
    with db.pool.connection() as c:
        ops.rollup_day(c, d(1))
    assert db.one("SELECT requests FROM request_days WHERE day = %s", (d(1),))["requests"] == 3


def test_prune_keeps_unrolled_days(monkeypatch):
    _clean()
    today = ops.utc_today()
    old = ops.day_start(today - timedelta(days=50))
    _log(old, "/", CLAUDE)
    with db.pool.connection() as c:  # only an older day is rolled up
        ops.rollup_day(c, today - timedelta(days=60))
    monkeypatch.setattr(config, "REQUEST_LOG_DAYS", 1)
    assert ops.prune(today) == 0
    assert ops.maintain(today) == {"rolled_up_days": 50, "pruned_requests": 1}


def test_stats_page(client, uniq, monkeypatch):
    _clean()
    today = ops.utc_today()
    _log(ops.day_start(today - timedelta(days=3)) + timedelta(hours=1), "/AGENTS.md", CLAUDE, "z1")
    secret = "s" + uniq
    assert client.get(f"/post?board=general&subject=S+{uniq}&body=op-{uniq}&secret={secret}",
                      headers={"User-Agent": CLAUDE}).status_code == 200
    tid = find(client, f"op-{uniq}")[0]["id"]
    client.post("/reply", data={"thread": tid, "body": f"post {uniq}"}, headers={"User-Agent": "curl/8.4"})
    client.post("/api/v1/replies", json={"thread": tid, "body": f"api {uniq}"})
    client.get(f"/say/{tid}/agree", headers={"User-Agent": CHROME})
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "reply", "arguments": {"thread": tid, "body": f"mcp {uniq}"}}})
    ops.clear_cache()
    j = client.get("/stats.json").json()
    assert j["days"][0]["day"] == str(today) and j["days"][0]["posts"] >= 5 and j["days"][0]["trips"] >= 1
    assert len(j["days"]) == 14
    three = next(x for x in j["days"] if x["day"] == str(today - timedelta(days=3)))
    assert three["requests"] == 1 and three["clients"] == 1  # from the rollup
    for k in ("GET query string", "POST form/JSON", "JSON API", "link (fixed reply)", "MCP"):
        assert j["posts_by_transport"].get(k, 0) >= 1, (k, j["posts_by_transport"])
    assert j["posts_by_transport_and_client"]["GET query string"].get("Claude-User", 0) >= 1
    assert j["requests_by_client_30d"]["Claude-User"] >= 2
    assert j["requests_by_client_and_area_30d"]["Claude-User"]["manual"] >= 1
    assert j["requests_by_group_30d"]["agent fetcher"] >= 2
    assert j["posts_by_client"]["Claude-User"] >= 1
    assert any(b["board"] == "general" and b["posts_30d"] >= 5 for b in j["boards"])
    busy = lambda j: next(x for x in j["busiest_threads_7d"] if x["thread"] == tid)  # noqa: E731
    assert busy(j)["posts_7d"] == 5 and busy(j)["trips_7d"] == 1
    text = client.get("/stats").text
    assert "Busiest threads" in text and f"/t/{tid}" in text
    assert "z1" not in client.get("/stats.json").text  # no client hashes on the public page

    # cached: a new post does not show up until the cache expires
    gen = j["generated_at"]
    client.get(f"/reply?thread={tid}&body=more+{uniq}")
    assert client.get("/stats.json").json()["generated_at"] == gen
    monkeypatch.setattr(config, "STATS_CACHE_S", 0)
    assert busy(client.get("/stats.json").json())["posts_7d"] == 6


def test_admin_traffic(client, monkeypatch, uniq):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    assert client.get("/_admin/traffic").status_code == 404
    assert client.get("/_admin/traffic?token=").status_code == 404
    monkeypatch.setattr(config, "ADMIN_TOKEN", "tok-" + uniq)
    assert client.get("/_admin/traffic?token=wrong").status_code == 404
    code = "Zx9" + uniq
    client.get(f"/draft/{code}", headers={"User-Agent": "probe-" + uniq})
    client.get(f"/k/{code}/3/w.hello", headers={"User-Agent": "probe-" + uniq})
    client.get(f"/b/general?secret={uniq}secretvalue", headers={"User-Agent": "probe-" + uniq})
    r = client.get(f"/_admin/traffic?token=tok-{uniq}&ua=probe-{uniq}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "noindex" in r.headers["x-robots-tag"]
    assert code not in r.text and "secretvalue" not in r.text
    j = client.get(f"/_admin/traffic.json?token=tok-{uniq}&ua=probe-{uniq}").json()
    paths = [x["path"] for x in j["requests"]]
    assert set(paths) == {"/draft/…", "/k/…/3/w.hello", "/b/general"}
    assert all(len(x["client"]) == 6 for x in j["requests"])
    assert j["user_agents"][0]["ua"] == "probe-" + uniq and j["user_agents"][0]["requests"] == 3
    assert j["posts"] and "transport" in j["posts"][0]
    assert ops.redact("/confirm/abcDEF") == "/confirm/…"
    assert ops.redact("/x/" + "a" * 30) == "/x/…"


def test_healthz_not_logged(client):
    n = db.one("SELECT count(*) AS n FROM request_log WHERE path = '/healthz'")["n"]
    assert client.get("/healthz").text == "ok"
    assert db.one("SELECT count(*) AS n FROM request_log WHERE path = '/healthz'")["n"] == n
