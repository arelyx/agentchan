#!/usr/bin/env python3
"""Swarm eval: several waves of mixed-harness agents (Claude, Gemini, Codex, ...) freely use a board,
each with no memory, so we can watch whether coherent multi-agent conversation emerges and which data
model supports it best.

usage: eval/.venv/bin/python eval/run_swarm.py --target URL [--waves 3]
       [--members claude-native:haiku,agy:gemini,codex-net:gpt,webfetch:haiku]
"""
import argparse
import concurrent.futures as cf
import json
import time

import httpx

import run_eval as RE

PROMPT = (
    "There is a message board for AI agents at {url} . You are one of several AI agents (from different "
    "companies, running in different tools) who have been given access to it at the same time. You have "
    "no memory of earlier visits. Spend a little while there, as yourself: catch up on what other agents "
    "have been saying recently, reply where you have something genuinely useful or interesting to add "
    "(refer to the specific posts you're answering), and start a new conversation if you have something "
    "worth starting. Use the display name \"{name}\" and, if the site supports a secret for a stable "
    "identity, the secret \"{secret}\". Make at most 3 posts. Include the exact text {nonce} at the very "
    "end of each post. When done, briefly report what you read, what you posted (URLs), and anything "
    "about the site that got in your way."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--waves", type=int, default=3)
    ap.add_argument("--members", default="claude-native:haiku,agy:gemini,codex-net:gpt,webfetch:haiku")
    a = ap.parse_args()
    target = a.target.rstrip("/")
    members = [m.split(":") for m in a.members.split(",")]
    outdir = RE.RUNS / ("swarm-" + time.strftime("%Y%m%d-%H%M%S"))
    recs = []
    for w in range(a.waves):
        jobs = []
        for i, (prof, model) in enumerate(members):
            name = f"{model}-{prof}-{i}"
            task = f"swarm-{w}-{i}"
            RE.TASKS[task] = PROMPT.replace("{name}", name).replace("{secret}", f"swarm-secret-{name}")
            jobs.append((target, prof, model, task, outdir))
        with cf.ThreadPoolExecutor(len(jobs)) as ex:
            recs += [r for r in ex.map(lambda j: RE.safe_run(*j), jobs) if r]
    # pull everything posted during the swarm
    allposts = []
    for r in recs:
        for p in httpx.get(f"{target}/_eval/posts.json", params={"contains": r["nonce"]}, timeout=10).json():
            p["_by"] = f"{r['profile']}:{r['model']}"
            allposts.append(p)
    (outdir / "posts.json").write_text(json.dumps(allposts, indent=1))
    (outdir / "summary.json").write_text(json.dumps(recs, indent=1))
    ids = {str(p["id"]) for p in allposts}
    replies_to_swarm = sum(1 for p in allposts if str(p.get("parent")) in ids)
    convs = {str(p.get("thread") or p.get("id")) for p in allposts}
    print(f"target={target} agents={len(recs)} posts={len(allposts)} "
          f"replies_to_other_swarm_posts={replies_to_swarm} conversations_touched={len(convs)} "
          f"agents_who_posted={sum(1 for r in recs if r['posts_found'])}")
    for r in recs:
        print(f"  {r['task']:12} {r['profile']:13} {r['model']:7} posts={r['posts_found']} "
              f"calls={r['tool_calls']} secs={r['secs']}")
    print(f"out: {outdir}")


if __name__ == "__main__":
    main()
