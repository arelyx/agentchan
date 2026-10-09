#!/usr/bin/env python3
"""Agent usability eval: point real agents (claude -p) with harness-like tool constraints at a
board variant and measure whether they can read the docs, read posts, post and reply.

usage: eval/.venv/bin/python eval/run_eval.py --target http://127.0.0.1:18701 \
           [--profiles curl,fetch-md,fetch-strict] [--models haiku,sonnet] [--task explore] [-j 4]

Every variant must implement the eval contract:
    GET /_eval/posts.json?contains=TEXT  ->  [{"id":..,"kind":"thread|post|reply|..","parent":..,
                                              "author":..,"title":..,"body":..}]
"""
import argparse
import concurrent.futures as cf
import json
import os
import random
import string
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"

TASKS = {
    "explore": (
        "There is a message board for AI agents at {url} . Use your tools to visit it, figure out how "
        "it works, and read what other agents have been posting. Then:\n"
        "1. Start a new thread / top-level post of your own about anything you find interesting.\n"
        "2. Reply to one existing post written by someone else.\n"
        "Sign both as \"{name}\" and include the exact text {nonce} somewhere in the body of both "
        "messages. Your messages should contain at least two sentences, one of which must contain "
        "a comma, a question mark and an ampersand (&).\n"
        "When you are done, report the URL(s) of what you posted and anything that confused you "
        "about the site."
    ),
    "catchup": (
        "There is a message board for AI agents at {url} . You visited it before but have no memory "
        "of it. Find out what has been discussed recently, find the most active conversation, and "
        "contribute a thoughtful reply to it. Sign as \"{name}\" and include the exact text {nonce} "
        "in your reply. Report the URL of your reply and summarize what you read in 3 bullet points."
    ),
}

PROFILES = {
    # name: (fetch_profile or None for bash/curl, tool list)
    "curl": None,
    "fetch-raw": "raw",
    "fetch-md": "markdown",
    "fetch-strict": "strict",
    "fetch-small": "markdown",  # MCP reference fetch server style: 5000-char truncation
}
PROFILE_ENV = {"fetch-small": {"FETCH_MAX_CHARS": "5000"}}


def nonce():
    return "nonce-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def run_one(target, profile, model, task, outdir):
    n = nonce()
    name = f"eval-{model}-{profile}"
    prompt = TASKS[task].format(url=target, name=name, nonce=n)
    tag = f"{profile}-{model}-{task}-{n}"
    d = outdir / tag
    d.mkdir(parents=True)
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
           "--no-session-persistence", "--max-budget-usd", "1.50"]
    if PROFILES[profile] is None:
        cmd += ["--tools", "Bash", "--allowedTools", "Bash(curl:*)", "Bash(echo:*)",
                "Bash(python3:*)", "--strict-mcp-config"]
    else:
        cfg = {"mcpServers": {"web": {
            "command": str(HERE / ".venv/bin/python"), "args": [str(HERE / "fetch_mcp.py")],
            "env": {"FETCH_PROFILE": PROFILES[profile], "FETCH_SEED": target,
                    "FETCH_LOG": str(d / "fetch.jsonl"), **PROFILE_ENV.get(profile, {})}}}}
        (d / "mcp.json").write_text(json.dumps(cfg))
        cmd += ["--tools", "", "--mcp-config", str(d / "mcp.json"), "--strict-mcp-config",
                "--allowedTools", "mcp__web__fetch"]
    t0 = time.time()
    if model == "qwen":  # local OpenAI-compatible model via eval/oss_agent.py (no curl profile)
        (d / "prompt.txt").write_text(prompt)
        prof = PROFILES[profile] or "raw"
        env = {**os.environ, **PROFILE_ENV.get(profile, {})}
        p = subprocess.run([str(HERE / ".venv/bin/python"), str(HERE / "oss_agent.py"), "--prompt-file",
                            str(d / "prompt.txt"), "--out", str(d), "--profile", prof, "--seed", target],
                           capture_output=True, text=True, timeout=3600, env=env)
        (d / "stderr.txt").write_text(p.stderr)
        try:
            o = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            o = {"tool_calls": 0, "tool_errors": 0, "final": p.stdout[-1500:] + p.stderr[-500:]}
        return finish(target, profile, model, task, n, t0, None, o["tool_calls"], o["tool_errors"],
                      o["final"], d)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, cwd=d,
                       stdin=subprocess.DEVNULL)
    (d / "transcript.jsonl").write_text(p.stdout)
    (d / "stderr.txt").write_text(p.stderr)
    events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    result = next((e for e in events if e.get("type") == "result"), {})
    tool_calls, tool_errors = 0, 0
    for e in events:
        if e.get("type") == "assistant":
            tool_calls += sum(1 for c in e["message"].get("content", []) if c.get("type") == "tool_use")
        if e.get("type") == "user":
            for c in e["message"].get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                    tool_errors += 1
    return finish(target, profile, model, task, n, t0, result, tool_calls, tool_errors,
                  result.get("result") or "", d)


def finish(target, profile, model, task, n, t0, result, tool_calls, tool_errors, final, d):
    result = result or {}
    try:
        found = httpx.get(f"{target}/_eval/posts.json", params={"contains": n}, timeout=10).json()
    except Exception as ex:  # noqa: BLE001
        found = [{"error": str(ex)}]
    rec = {
        "target": target, "profile": profile, "model": model, "task": task, "nonce": n,
        "secs": round(time.time() - t0), "turns": result.get("num_turns"),
        "cost": result.get("total_cost_usd"), "tool_calls": tool_calls, "tool_errors": tool_errors,
        "posts_found": len([f for f in found if "error" not in f]),
        "kinds": sorted({str(f.get("kind")) for f in found}),
        "ampersand_ok": any("&" in (f.get("body") or "") for f in found),
        "newline_ok": any("\n" in (f.get("body") or "").strip() for f in found),
        "final": final[-1500:],
        "dir": str(d),
    }
    (d / "result.json").write_text(json.dumps(rec, indent=2))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--profiles", default="curl,fetch-md,fetch-strict")
    ap.add_argument("--models", default="haiku")
    ap.add_argument("--task", default="explore")
    ap.add_argument("-j", type=int, default=3)
    a = ap.parse_args()
    outdir = RUNS / time.strftime("%Y%m%d-%H%M%S")
    jobs = [(a.target.rstrip("/"), p, m, a.task, outdir)
            for p in a.profiles.split(",") for m in a.models.split(",")]
    with cf.ThreadPoolExecutor(a.j) as ex:
        recs = list(ex.map(lambda j: run_one(*j), jobs))
    (outdir / "summary.json").write_text(json.dumps(recs, indent=2))
    print(f"{'profile':13} {'model':7} {'posts':5} {'kinds':22} {'&':2} {'nl':2} {'calls':5} "
          f"{'errs':4} {'secs':5} cost")
    for r in recs:
        print(f"{r['profile']:13} {r['model']:7} {r['posts_found']:<5} {','.join(r['kinds']):22} "
              f"{'y' if r['ampersand_ok'] else 'n':2} {'y' if r['newline_ok'] else 'n':2} "
              f"{r['tool_calls']:<5} {r['tool_errors']:<4} {r['secs']:<5} {r['cost']}")
    print(f"\nruns in {outdir}", file=sys.stderr)


if __name__ == "__main__":
    main()
