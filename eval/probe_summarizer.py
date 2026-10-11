#!/usr/bin/env python3
"""Ask a summarizing fetcher pointed questions about agentchan pages and grade the answers.

Claude Code's WebFetch converts a page to markdown and passes it, with a prompt the agent wrote, to a small
model; the agent only sees that model's answer. This script asks the questions agents ask and checks what
survives.

  --mode live     real WebFetch through `claude -p --tools WebFetch` (target must be public https)
  --mode offline  the page's markdown in WebFetch's prompt wrapper, sent to `claude -p --model haiku` with no
                  tools (works against localhost). Caveat: here haiku sometimes treats the wrapper's 125-character
                  quote rule as text from the page and ignores it, so quote-length probes (T2, T4, R4) read
                  better offline than they do live.

Probes T1-T4 and C2 name posts that exist on the eval board and on a replay of it (thread 5, post 21,
thread 26). R* probes WRITE to the target (thread 8, /lounge/).

  eval/.venv/bin/python eval/probe_summarizer.py --target https://<public board> --mode live --reps 2
"""
import argparse
import concurrent.futures as cf
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

TAG = str(int(time.time()))[-6:]
LONG = ("Summarizer+probe+" + TAG + ":+this+reply+is+deliberately+longer+than+one+hundred+and+twenty-five+characters,"
        "+so+a+summarizing+fetcher+cannot+quote+it+in+one+piece.+Does+the+receipt+still+let+you+check+every+word%3F+End.")
SUCCESS_Q = "Did the post succeed? Give the post number, its URL, and the stored text exactly."
PROBES = [  # id, path, prompt (as an agent would write it)
    ("M1", "/", "According to this page, what HTTP method do /post and /reply use? Quote the exact URL template for replying."),
    ("M2", "/", "Describe how to post a new thread and how to reply to a post on this board."),
    ("M3", "/", "How is a tripcode derived: from the name, the secret, or both? If I keep the same secret but change my "
                "name, does my tripcode change?"),
    ("M4", "/", "How do I put a literal & character and a line break in a post body?"),
    ("M5", "/", "What are the rate limits for new threads and for replies?"),
    ("M6", "/", "If I reply using the number of a reply (not the thread's first post) as thread=, what happens?"),
    ("T1", "/t/5", "List every post on this page with its post number and author (name and tripcode). How many posts are there?"),
    ("T2", "/t/5", "Quote the complete text of post No.21 exactly, word for word, leaving nothing out."),
    ("T3", "/t/5", "Which posts does No.21 quote, and which posts quote No.15?"),
    ("T4", "/p/21", "Quote the complete text of post No.21 exactly, word for word, leaving nothing out."),
    ("L1", "/new", "List the 5 newest posts on this page: post number, thread number, board and author."),
    ("L2", "/new", "What is the newest post number on the site, and what URL should I fetch later to see posts after it?"),
    ("B1", "/b/ideas", "List the threads on this board: thread number, subject, number of replies, and the author of the latest reply."),
    ("C1", "/", "List the recent discussions/threads on this message board. For each, give the title, URL, reply count, "
                "author, and last activity. Which thread is the most active?"),
    ("C2", "/t/26", "Give the full thread content: the opening post and every reply with its number, author name, and text. "
                    "Also describe how to post a reply here (field names, required fields, how to sign with a name)."),
    ("C3", "/", "Describe how this message board works: the main pages, how threads and replies are created (method, "
                "paths, fields), how signing/identity works, and what happens on errors."),
    ("R1", f"/reply?thread=8&body=Summarizer+probe+{TAG},+please+ignore:+A+%26+B,+ok%3F&name=probe-wf&secret=pr0be-a-{TAG}",
     SUCCESS_Q),
    ("R2", f"/reply?thread=8&body=leaking+pr0be-b-{TAG}+here&name=probe-wf&secret=pr0be-b-{TAG}", SUCCESS_Q),
    ("R4", f"/reply?thread=8&body={LONG}&name=probe-wf&secret=pr0be-c-{TAG}",
     "Did the post succeed? Give the post number and quote the stored text exactly and completely."),
    ("R5", f"/reply?thread=8&body={LONG.replace('Summarizer+probe', 'Encoding+probe')}+Tea+%26+cake+%23two."
           f"&name=probe-wf&secret=pr0be-d-{TAG}",
     "Did the post succeed, and was the complete text stored without anything lost to URL encoding (for example "
     "cut off at an & or #)? The text should end with 'Tea & cake #two.' Give the post number."),
]
WRAP = """Web page content:
---
{content}
---

{prompt}

Provide a concise response based only on the content above. In your response:
 - Enforce a strict 125-character maximum for quotes from any source document. Open Source Software is ok as long as we respect the license.
 - Use quotation marks for exact language from articles; any language outside of the quotation marks should never be word-for-word the same.
 - You are not a lawyer and never comment on the legality of your own prompts and responses.
 - Never produce or reproduce exact song lyrics.
"""
FULL21 = ("Antigravity", "summarization truncation", "Glad to be here exploring", "nonce-c2p0iex8", "native URL viewing")


def fetch_md(url):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "text/markdown"})).read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()


def ask_offline(content, prompt):
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--tools", "", "--no-session-persistence",
                        "--strict-mcp-config", "--system-prompt", "You are a helpful assistant."],
                       input=WRAP.format(content=content, prompt=prompt), capture_output=True, text=True, timeout=300)
    return r.stdout.strip()


def ask_live(url, prompt):
    p = (f"Call the WebFetch tool exactly once with url={url!r} and prompt={prompt!r}. "
         "Do not call any other tool. Then reply with only the word DONE.")
    out = subprocess.run(["claude", "-p", p, "--model", "haiku", "--output-format", "stream-json", "--verbose",
                          "--no-session-persistence", "--tools", "WebFetch", "--allowedTools", "WebFetch",
                          "--strict-mcp-config", "--max-budget-usd", "0.3"],
                         capture_output=True, text=True, timeout=300).stdout
    res = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                cc = c.get("content")
                res.append(cc if isinstance(cc, str) else "\n".join(x.get("text", "") for x in cc))
    return "\n---\n".join(res)


def grade(pid, a, snap):
    """True/False, or None when the fetch itself failed. Coarse: read the answers for borderline cases."""
    al = a.lower()
    if re.match(r"The server returned HTTP 5", a):
        return None
    checks = {
        "M1": lambda: "get" in al and "/reply?thread=" in a,
        "M2": lambda: len(re.findall(r"\bGET\b", a)) >= 2 and "name" in al,
        "M3": lambda: bool(re.search(r"secret (alone|only)|not the name|name is not|any name", al))
        and bool(re.search(r"stays the same|does not change|doesn't change|same tripcode", al)),
        "M4": lambda: "%26" in a and "%0A" in a,
        "M5": lambda: "60" in a and "10" in a,
        "M6": lambda: bool(re.search(r"(that|its|the) (post's |reply's |same )?thread|thread (that|the reply)", al)),
        "T1": lambda: all(str(n) in a for n in (5, 10, 12, 15, 21, 25)) and "6 posts" in al,
        "T2": lambda: all(s in a for s in FULL21),
        "T4": lambda: all(s in a for s in FULL21),
        "T3": lambda: all(re.search(rf"\b{n}\b", a) for n in (5, 10, 12, 15, 20, 21)),
        "L1": lambda: all(n in a for n in re.findall(r"--- post (\d+) \[", snap)[-5:]),
        "L2": lambda: (lambda n: f"since={n}" in a)(re.search(r"Newest post on the site: No\.(\d+)", snap).group(1)),
        "B1": lambda: "repl" in al or "|" in a,
        "C1": lambda: not re.search(r"not shown|(doesn't|does not) (show|give)|partial", al),
        "C2": lambda: "name=" in al[al.find("reply"):] or "`name`" in al,
        "C3": lambda: "400" in a and bool(re.search(r"\bGET\b[^\n]{0,40}(/post|/reply)", a)),
        "R1": lambda: "succe" in al and "A & B, ok?" in a,
        "R2": lambda: bool(re.search(r"did not succeed|didn't succeed|nothing was posted|not posted", al)),
        "R4": lambda: "deliberately longer than one hundred" in a and "check every word? End." in a,
        "R5": lambda: "succe" in al and "Tea & cake #two." in a,
    }
    return checks[pid]()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--mode", choices=("live", "offline"), default="offline")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    base = a.target.rstrip("/")
    probes = [p for p in PROBES if not a.only or p[0] in a.only.split(",")]
    snap = {pid: fetch_md(base + path) for pid, path, _ in probes
            if a.mode == "offline" or not path.startswith(("/reply", "/post"))}
    jobs = [(pid, path, q, r) for pid, path, q in probes for r in range(a.reps)
            if not (path.startswith("/reply") and r > 0 and a.mode == "live")]

    def run(j):
        pid, path, q, r = j
        ans = ask_live(base + path, q) if a.mode == "live" else ask_offline(snap[pid], q)
        return {"id": pid, "rep": r, "q": q, "answer": ans}
    with cf.ThreadPoolExecutor(4) as ex:
        res = list(ex.map(run, jobs))
    out = Path(a.out or f"eval/runs/probe-{a.mode}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"target": base, "mode": a.mode, "snap": snap, "results": res}, indent=1))
    print(f"answers in {out}\n\n| probe | correct |\n|---|---|")
    for pid, _, _ in probes:
        g = [grade(pid, r["answer"], snap.get(pid, "")) for r in res if r["id"] == pid]
        ok = [x for x in g if x is not None]
        print(f"| {pid} | {sum(ok)}/{len(ok)}" + (f" ({len(g) - len(ok)} fetch errors)" if len(ok) < len(g) else "") + " |")


if __name__ == "__main__":
    main()
