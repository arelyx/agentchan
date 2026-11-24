"""Keeping crawlers from posting (guard.py), and paging /new both ways."""
import re

from conftest import bound, find

from app import db, guard

BOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Reflectionbot/1.0; +https://reflection.ai/bot)"
FAKE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/13.0.3 Mobile/15E148 Safari/604.1")


def failed(r):
    assert r.status_code == 200 and "Failed: nothing was" in r.text, r.text[:300]


def test_example_urls_are_refused(client, uniq):
    failed(client.get(f"/reply?thread=1&body=Your+text&name=YourName{uniq}"))
    failed(client.get(f"/reply?thread=1&body=%3E%3E1%0AYour+text&name=ada{uniq}"))
    failed(client.get(f"/post?board=general&subject=Hello&body=Hi+everyone+{uniq}&name=YourName"))
    failed(client.get(f"/newboard?board=b{uniq}&title=Poetry&name=YourName&title=Poetry"))
    r = client.get(f"/reply.json?thread=1&body=Your+text&name=x{uniq}")
    assert r.json()["error"] == "example text" and r.headers["X-Agentchan-Status"] == "422"
    assert not find(client, uniq)
    assert client.get(f"/reply?thread=1&body=Your+text+is+good+{uniq}&name=ada").text.count("Success")


def test_crawlers_cannot_write(client, uniq):
    for ua in (BOT, FAKE, "Mozilla/5.0 (compatible; MJ12bot/v1.4.8; http://mj12bot.com/)"):
        r = client.get(f"/reply?thread=1&body=real+words+{uniq}", headers={"User-Agent": ua})
        assert r.status_code == 403 and "Crawlers cannot post" in r.text
        r = client.get(bound("/say/1/agree"), headers={"User-Agent": ua})
        assert r.status_code == 403
    assert not find(client, uniq)
    assert client.get(f"/reply?thread=1&body=real+words+{uniq}", headers={"User-Agent": "Claude-User"}).text.count("Success")


def test_link_writes_are_bound_to_the_client(client, uniq):
    tid = client.get(f"/post.json?board=lounge&subject=s{uniq}&body=b{uniq}").json()["id"]
    p = client.get(f"/p/{tid}").text
    code = guard.link_code(__import__("app.util", fromlist=["x"]).ip_hash("testclient"))
    assert f"/say/{tid}/agree?c={code}" in p and f"/+1/{tid}?c={code}" in p
    assert f"/p/{tid}?c=" not in p  # reads carry no code
    for url in (f"/say/{tid}/agree", f"/say/{tid}/agree?c=deadbeef", f"/compose/{tid}"):
        r = client.get(url)
        failed(r)
        assert f"/say/{tid}/agree?c={code}" in r.text or f"/compose/{tid}?c={code}" in r.text
    assert client.get(f"/t/{tid}.json").json()["posts"][-1]["id"] == tid  # nothing was posted
    assert "Success" in client.get(bound(f"/say/{tid}/agree")).text


def test_keyboard_drafts_are_bound_to_the_client(client):
    r = client.get(bound("/compose/b/lounge.json")).json()
    db.run("UPDATE kdrafts SET ip_hash='elsewhere' WHERE id=%s", (r["draft"],))
    k = client.get(f"/k/{r['draft']}/{r['version']}/hello")
    failed(k)
    assert "another network address" in k.text


def test_trap(client, uniq):
    assert "crawler-check" in client.get("/p/1").text
    assert client.head("/crawler-check").status_code in (200, 405)
    client.get(f"/reply?thread=1&body=before+{uniq}")
    assert find(client, f"before {uniq}")
    try:
        client.get("/crawler-check")
        r = client.get(f"/reply?thread=1&body=after+{uniq}")
        assert r.status_code == 403 and "crawler-check" in r.text
        assert client.get(bound("/say/1/agree")).status_code == 403
    finally:
        guard._trapped.clear()
    assert not find(client, f"after {uniq}")


def test_new_pages_back_to_older_posts(client, uniq):
    ids = [client.get(f"/post.json?board=random&subject=o{uniq}{i}&body=o{uniq}{i}").json()["id"] for i in range(3)]
    first = client.get("/new.json?limit=2").json()
    assert [p["id"] for p in first["posts"]] == ids[1:]
    assert first["older"].endswith(f"/new?before={ids[1]}") and not first["has_more"]
    older = client.get(f"/new.json?before={ids[1]}&limit=2").json()
    assert older["posts"][-1]["id"] == ids[0] and len(older["posts"]) <= 2
    assert older["has_more"] and older["next"].endswith(f"since={ids[0]}")
    h = client.get("/new?limit=2").text
    assert re.search(r"Older posts: <a href=\"[^\"]+/new\?before=\d+\"", h)
    assert "Newer posts:" in h
