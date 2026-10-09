#!/usr/bin/env python3
"""Multi-session "relay" eval: the real use case is memoryless agents talking across sessions.

Phase A  asker:    posts a question under a name + secret (tripcode) identity.
Phase B  answerer: a fresh agent must FIND that question (only knows the asker's name) and answer it.
Phase C  asker':   a fresh agent given only the asker's name + secret must find replies to "its" earlier
                   post and respond to the answerer in the same conversation.

usage: eval/.venv/bin/python eval/run_relay.py --target URL [--profile fetch-md] [--model haiku] [-n 2]
"""
import argparse
import concurrent.futures as cf
import json
import random
import string
import time

import httpx

import run_eval as RE

PHASES = {
    "A": ("There is a message board for AI agents at {url} . Figure out how it works, then start a new "
          "conversation asking other agents this question: \"{question}\" Post under the display name "
          "\"{name}\" and, if the site supports a secret/password for a stable identity, use the secret "
          "\"{secret}\". Include the exact text {nonce} in your message. Report the URL of your post."),
    "B": ("There is a message board for AI agents at {url} . An agent posting as \"{asker}\" recently asked "
          "a question there. Find that question and post a helpful answer as a reply in the same "
          "conversation. Sign as \"{name}\" and include the exact text {nonce} in your reply. Report "
          "the URL of your reply."),
    "C": ("There is a message board for AI agents at {url} . In a previous session (you have no memory of "
          "it) you posted a question there using the display name \"{asker}\" and the secret \"{secret}\". "
          "Find your earlier post, find any replies to it, and reply to the agent who answered — address "
          "them by name and quote or reference their answer. Post with the same name and secret, and "
          "include the exact text {nonce}. Report what the answer was and the URL of your reply."),
}

QUESTIONS = ["What is one thing you wish humans understood about how you read web pages?",
             "If you could add one feature to this board, what would it be and why?",
             "What's a small task you find surprisingly hard, and how do you work around it?"]


def rand(k=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))


def find(target, nonce):
    try:
        return httpx.get(f"{target}/_eval/posts.json", params={"contains": nonce}, timeout=10).json()
    except Exception:  # noqa: BLE001
        return []


def relay(target, profile, model, outdir):
    asker, answerer, secret = f"asker-{rand(4)}", f"answerer-{rand(4)}", "s-" + rand(10)
    nA, nB, nC = (RE.nonce() for _ in range(3))
    q = random.choice(QUESTIONS)
    out = {"target": target, "profile": profile, "model": model, "asker": asker}
    for phase, name, n in (("A", asker, nA), ("B", answerer, nB), ("C", asker, nC)):
        RE.TASKS[f"relay{phase}"] = PHASES[phase].format(
            url="{url}", question=q, name=name, secret=secret, nonce=n, asker=asker)
        rec = RE.run_one(target, profile, model, f"relay{phase}", outdir)
        out[phase] = {k: rec[k] for k in ("posts_found", "tool_calls", "tool_errors", "secs", "cost", "dir")}
    a, b, c = find(target, nA), find(target, nB), find(target, nC)
    th = lambda ps: {str(p.get(k)) for p in ps for k in ("id", "parent", "thread") if p.get(k)}  # noqa: E731
    out["A_ok"] = bool(a)
    out["B_ok"] = bool(b) and bool(th(a) & th(b))
    out["C_ok"] = bool(c) and bool((th(a) | th(b)) & th(c))
    out["C_mentions_answerer"] = any(answerer in (p.get("body") or "") for p in c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--profile", default="fetch-md")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("-n", type=int, default=2, help="independent relays to run in parallel")
    a = ap.parse_args()
    outdir = RE.RUNS / ("relay-" + time.strftime("%Y%m%d-%H%M%S"))
    with cf.ThreadPoolExecutor(a.n) as ex:
        res = list(ex.map(lambda _: relay(a.target.rstrip("/"), a.profile, a.model, outdir), range(a.n)))
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "summary.json").write_text(json.dumps(res, indent=2))
    for r in res:
        calls = sum(r[p]["tool_calls"] for p in "ABC")
        print(f"{r['target']} {r['profile']} {r['model']} A={r['A_ok']} B={r['B_ok']} C={r['C_ok']} "
              f"C_mentions_answerer={r['C_mentions_answerer']} calls={calls}")


if __name__ == "__main__":
    main()
