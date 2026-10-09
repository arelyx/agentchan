#!/usr/bin/env python3
"""Tiny tool-calling agent loop for an OpenAI-compatible endpoint (e.g. a local llama.cpp Qwen), used as
a "weaker / non-Claude model" profile in run_eval.py. Tools: `fetch` (same simulator as fetch_mcp.py)
or `curl` (GET-only http, raw).

usage: oss_agent.py --prompt-file P --out DIR [--profile markdown|raw|strict] [--seed URL]
env: OSS_BASE_URL (default http://127.0.0.1:8080/v1), OSS_MODEL (default first served model)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="markdown")
    ap.add_argument("--seed", default="")
    ap.add_argument("--max-steps", type=int, default=30)
    a = ap.parse_args()
    os.environ["FETCH_PROFILE"] = a.profile
    os.environ["FETCH_SEED"] = a.seed
    os.environ["FETCH_LOG"] = str(Path(a.out) / "fetch.jsonl")
    import fetch_mcp  # reads env at import

    base = os.environ.get("OSS_BASE_URL", "http://127.0.0.1:8080/v1")
    model = os.environ.get("OSS_MODEL") or httpx.get(f"{base}/models").json()["data"][0]["id"]
    tools = [{"type": "function", "function": {
        "name": "fetch", "description": fetch_mcp.TOOL["description"],
        "parameters": fetch_mcp.TOOL["inputSchema"]}}]
    msgs = [{"role": "system", "content": "You are a helpful autonomous agent with a web fetch tool. "
             "Use tools to accomplish the task, then give a final answer."},
            {"role": "user", "content": Path(a.prompt_file).read_text()}]
    calls = errors = 0
    t0 = time.time()
    final = ""
    for _ in range(a.max_steps):
        r = httpx.post(f"{base}/chat/completions", timeout=600, json={
            "model": model, "messages": msgs, "tools": tools, "temperature": 0.3, "max_tokens": 4096})
        msg = r.json()["choices"][0]["message"]
        msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        tcs = msg.get("tool_calls") or []
        if not tcs:
            final = msg.get("content") or ""
            break
        for tc in tcs:
            calls += 1
            try:
                url = json.loads(tc["function"]["arguments"]).get("url", "")
            except Exception:  # noqa: BLE001
                url = ""
            is_err, text = fetch_mcp.do_fetch(url)
            errors += is_err
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
    Path(a.out, "transcript.json").write_text(json.dumps(msgs, indent=1))
    print(json.dumps({"tool_calls": calls, "tool_errors": errors, "secs": round(time.time() - t0),
                      "final": final[-1500:], "model": model}))


if __name__ == "__main__":
    main()
