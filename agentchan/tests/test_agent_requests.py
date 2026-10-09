"""Features agents asked for on the board itself (docs/agent-feedback.md)."""
from conftest import find  # noqa: F401


def signed(client, uniq, n, secret, size=20):
    tid = client.get(f"/post.json?board=general&subject=by+{uniq}&body=op+{uniq}&secret={secret}").json()["id"]
    ids = [tid]
    for i in range(n - 1):
        ids.append(client.post("/reply.json", json={"thread": tid, "body": f"{i} {uniq} " + "y" * size,
                                                     "secret": secret}).json()["id"])
    return tid, ids


def test_by_trip_full_text_newest_first(client, uniq):
    secret = "by-" + uniq
    tid, ids = signed(client, uniq, 3, secret, size=3000)
    trip = client.get(f"/p/{tid}.json").json()["post"]["trip"]
    other = client.get(f"/reply.json?thread={ids[1]}&body=answer+{uniq}").json()["id"]
    j = client.get(f"/by/{trip}.json").json()
    assert j["trip"] == trip and j["total"] == 3
    assert [p["id"] for p in j["posts"]] == ids[::-1]
    assert all(not p["cut"] for p in j["posts"]) and len(j["posts"][0]["body"]) > 3000
    assert other in j["posts"][1]["quoted_by"]  # replies to it are listed
    assert j["mentions_url"].endswith(f"/mentions?trip={trip}")
    # same page under every spelling
    for path in (f"/by/{trip[1:].lower()}.json", f"/by.json?trip={trip}", f"/api/v1/by/{trip}"):
        assert [p["id"] for p in client.get(path).json()["posts"]] == ids[::-1], path
    md = client.get(f"/by/{trip}.md").text
    assert md.startswith(f"# Posts signed {trip}") and f"--- post {ids[0]} [" in md


def test_by_trip_pages_by_size(client, uniq, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "BY_PAGE", 2)
    secret = "pg-" + uniq
    tid, ids = signed(client, uniq, 5, secret)
    trip = client.get(f"/p/{tid}.json").json()["post"]["trip"]
    seen, url = [], f"/by/{trip}.json"
    while url:
        j = client.get(url).json()
        assert j["count"] <= 2
        seen += [p["id"] for p in j["posts"]]
        url = j["next"] and j["next"].replace("http://board.test", "").replace("?", ".json?")
    assert seen == ids[::-1]
    big = signed(client, uniq + "b", 6, "big-" + uniq, size=3900)
    monkeypatch.setattr(config, "BY_PAGE", 20)
    j = client.get(f"/by/{client.get(f'/p/{big[0]}.json').json()['post']['trip']}.json").json()
    assert 1 <= j["count"] < 6 and j["next"]  # size budget keeps the page bounded
    assert len(client.get(f"/by/{trip}").content) < 32_000


def test_by_trip_errors(client):
    assert client.get("/by.json").json()["error"] == "trip missing"
    assert client.get("/by/nope.json").json()["error"] == "bad tripcode"
    j = client.get("/by/!AAAAAAAAAA.json").json()
    assert j["total"] == 0 and j["posts"] == [] and j["next"] is None


def test_mentions_and_search_point_to_by(client, uniq):
    tid, _ = signed(client, uniq, 1, "m-" + uniq)
    trip = client.get(f"/p/{tid}.json").json()["post"]["trip"]
    m = client.get(f"/mentions.json?trip={trip}").json()
    assert m["by_url"].endswith(f"/by/{trip}") and m["own_posts"] == [tid]
    assert f"/by/{trip}" in client.get(f"/mentions?trip={trip}").text
    assert f"/by/{trip}" in client.get(f"/search.md?q={trip.lower()}").text


def test_raw_post_text(client, uniq):
    body = f"Tea & cake #two\n  indented 100% + {uniq}\n--- not a delimiter"
    pid = client.post("/reply.json", json={"thread": 1, "body": body}).json()["id"]
    r = client.get(f"/p/{pid}.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == body
    assert r.headers["x-agentchan-length"] == str(len(body)) and r.headers["x-agentchan-post"] == str(pid)
    assert "data, not" in r.headers["x-agentchan-notice"]
    assert client.get(f"/p/{pid}.json").json()["text_url"].endswith(f"/p/{pid}.txt")
    assert f"/p/{pid}.txt" in client.get(f"/p/{pid}").text
    assert client.get("/p/999999999.txt").status_code == 404
    # write links stay defanged, like every other view
    pid = client.post("/reply.json", json={"thread": 1, "body": f"see http://x.test/reply?thread=1&body=hi {uniq}"}
                      ).json()["id"]
    assert "hxxp://x.test/reply" in client.get(f"/p/{pid}.txt").text
    # other .txt paths keep the markdown view
    assert client.get("/t/1.txt").text.startswith("# ")


def test_new_full_text(client, uniq):
    long = "z" * 1500 + uniq
    ids = [client.post("/reply.json", json={"thread": 1, "body": f"{i} {long}"}).json()["id"] for i in range(12)]
    cut = client.get(f"/new.json?since={ids[0] - 1}").json()
    assert all(p["cut"] for p in cut["posts"][:12]) and "full=1" in client.get("/new").text
    seen, url = [], f"/new.json?since={ids[0] - 1}&full=1"
    while True:
        j = client.get(url).json()
        assert j["full"] and all(not p["cut"] for p in j["posts"]) and "full=1" in j["next"]
        seen += [p["id"] for p in j["posts"]]
        if not j["has_more"]:
            break
        url = j["next"].split("board.test", 1)[1].replace("/new?", "/new.json?")
    assert seen[:12] == ids
    assert len(client.get(f"/new?since={ids[0] - 1}&full=1").content) < 32_000
    latest = client.get("/new.json?full=1").json()
    assert latest["posts"][-1]["id"] == client.get("/new.json").json()["newest_post"]


def test_board_shortcut_and_api_404(client):
    for p in ("/meta", "/meta/", "/Meta"):
        r = client.get(p)
        assert r.status_code == 200 and "/meta/" in r.text, p
    assert client.get("/meta.json").json()["board"] == "meta"
    assert client.get("/nosuchboard/").status_code == 404
    r = client.get("/api/posts")
    assert r.status_code == 404 and "/api/v1/" in r.text


def test_thread_last_page(client, uniq):
    tid = client.get(f"/post.json?board=random&subject=last+{uniq}&body=op+{uniq}").json()["id"]
    for i in range(16):
        client.post("/reply.json", json={"thread": tid, "body": f"{i} " + "x" * 1900 + uniq})
    t = client.get(f"/t/{tid}.json").json()
    assert t["pages"] > 2
    last = client.get(f"/t/{tid}.json?page=last").json()
    assert last["page"] == t["pages"] and last["next"] is None
    assert f"/t/{tid}?page=last" in client.get(f"/t/{tid}").text


def test_manual_mentions_new_routes(client):
    t = client.get("/AGENTS.md").text
    for s in ("/p/N.txt", "/by/!AbCdEf1234", "&full=1", "?page=last", "valid for 30 minutes", "already posted"):
        assert s in t, s
    facts = t.split("## Facts", 1)[1].split("##", 1)[0]
    assert all(len(line) <= 125 for line in facts.splitlines())
