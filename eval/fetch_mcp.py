#!/usr/bin/env python3
"""Minimal MCP stdio server that simulates the web-fetch tools of common agent harnesses.

It exposes one tool, `fetch(url)`, whose behaviour is controlled by env vars so the eval
runner can emulate different harness constraints:

  FETCH_PROFILE   raw       - GET, raw body back (like curl)
                  markdown  - GET, HTML converted to markdown (like Claude Code WebFetch,
                              Gemini CLI web_fetch, MCP fetch server)
                  strict    - markdown + the URL must already have appeared verbatim in the
                              seed prompt or in an earlier fetched page (like the Anthropic API
                              web_fetch tool / ChatGPT browsing)
  FETCH_SEED      whitespace separated URLs the "user" provided (for strict)
  FETCH_MAX_CHARS truncate returned text (default 100000)
  FETCH_HTTPS_ONLY  "1" rejects plain http:// URLs other than FETCH_SEED's host (off by default)
  FETCH_LOG       JSONL file to append one record per call to
"""
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urldefrag

import html2text
import httpx

PROFILE = os.environ.get("FETCH_PROFILE", "markdown")
MAX_CHARS = int(os.environ.get("FETCH_MAX_CHARS", "100000"))
LOG = os.environ.get("FETCH_LOG")
SEEN: set[str] = set()
for u in os.environ.get("FETCH_SEED", "").split():
    SEEN.add(urldefrag(u)[0])

URL_RE = re.compile(r"""https?://[^\s"'<>`)\]]+""")
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)

TOOL = {
    "name": "fetch",
    "description": (
        "Fetches a URL from the internet with an HTTP GET request and returns the page content"
        + (" converted to markdown." if PROFILE != "raw" else " as raw text.")
        + (" For security, only URLs that appeared in the user's message or in previously fetched"
           " pages may be fetched." if PROFILE == "strict" else "")
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The URL to fetch"}},
        "required": ["url"],
    },
}


def log(rec):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")


def remember_links(base, body):
    for m in URL_RE.findall(body):
        SEEN.add(urldefrag(m.rstrip(".,;:"))[0])
    for h in HREF_RE.findall(body):
        SEEN.add(urldefrag(urljoin(base, h.replace("&amp;", "&")))[0])


def do_fetch(url):
    rec = {"t": time.time(), "profile": PROFILE, "url": url}
    if PROFILE == "strict" and len(url) > 250:
        rec["result"] = "blocked-too-long"
        log(rec)
        return True, "Error: URL exceeds the maximum length of 250 characters."
    if PROFILE == "strict" and urldefrag(url)[0] not in SEEN:
        rec["result"] = "blocked-not-in-context"
        log(rec)
        return True, ("Error: this URL has not appeared in the conversation. You can only fetch URLs "
                      "provided by the user or that appeared in previously fetched content.")
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": f"agent-eval-fetch/{PROFILE}"})
    except Exception as e:  # noqa: BLE001
        rec["result"] = f"error: {e}"
        log(rec)
        return True, f"Error fetching URL: {e}"
    body = r.text
    rec.update(status=r.status_code, final_url=str(r.url), bytes=len(r.content),
               ctype=r.headers.get("content-type", ""))
    remember_links(str(r.url), body)
    if PROFILE != "raw" and "html" in r.headers.get("content-type", ""):
        h = html2text.HTML2Text(baseurl=str(r.url))
        h.body_width = 0
        body = h.handle(body)
    truncated = len(body) > MAX_CHARS
    body = body[:MAX_CHARS]
    rec["returned_chars"] = len(body)
    rec["truncated"] = truncated
    log(rec)
    head = f"HTTP {r.status_code} ({r.headers.get('content-type', '?')}) {r.url}\n\n"
    if r.status_code >= 400:
        return True, head + body
    return False, head + body + ("\n\n[content truncated]" if truncated else "")


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method, id_ = req.get("method"), req.get("id")
        if id_ is None:
            continue  # notification
        if method == "initialize":
            reply(id_, {"protocolVersion": req["params"].get("protocolVersion", "2024-11-05"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "web", "version": "0.1"}})
        elif method == "tools/list":
            reply(id_, {"tools": [TOOL]})
        elif method == "tools/call":
            is_err, text = do_fetch(req["params"]["arguments"].get("url", ""))
            reply(id_, {"content": [{"type": "text", "text": text}], "isError": is_err})
        elif method == "ping":
            reply(id_, {})
        else:
            reply(id_, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()
