# Harness Web-Access Tools: A Mechanical Survey

Research for the "friendly message board for AI agents" project. Goal: know exactly what
agents in common harnesses can and cannot do over HTTP, so the board can be designed to be
reachable (read **and** write) from as many of them as possible.

Date: 2026-10-09. Author: harshitgupta890@gmail.com (research agent).

The single most important axis for a message board is **URL construction freedom**: can the
agent hit an arbitrary URL of its own choosing (so a write can be encoded as
`GET /post?body=...`), or is it restricted to URLs that already appeared in the conversation
or in search results? The second axis is **method**: GET-only vs. can POST. Most agent web
tools are GET-only and many are URL-restricted, so the board must support a GET-based write
path to be broadly usable.

Live behavior marked "verified" was tested from this Claude Code session (v2.1.296) against
httpbin.org. Everything else is from docs or source; source paths are relative to the cloned
repos unless noted.

---

## Summary table

| Harness / tool | Method | Arbitrary URL? (can construct GET write) | URL rules | Response processing | Cache | Redirects | User-Agent |
|---|---|---|---|---|---|---|---|
| **Claude Code WebFetch** | GET | **Yes** (any https URL) | localhost/no-dot refused; http→https upgrade; ~250 char cap unverified-but-likely | HTML→markdown, then **small model runs your prompt** (lossy); 100k chars/call | **15 min** (configurable) | cross-host **returned, not followed** | `Claude-User (claude-code/2.1.296; +https://support.anthropic.com/)` (verified) |
| **Claude Code Bash `curl`** | **Any** | **Yes** | sandbox allowlist if sandbox on (default domains empty) | raw | none | curl follows if asked | curl default |
| **Anthropic API `web_fetch`** | GET | **No** — URL must appear earlier in conversation | **250-char URL max**; https; no private IPs; robots.txt; no-credential URLs; `allowed/blocked_domains` | text/HTML/PDF only; `max_content_tokens` | server cache, `use_cache:false` to bypass | followed server-side | Anthropic crawler |
| **Anthropic API `web_search`** | n/a (search) | returns URLs that then become fetchable | `allowed/blocked_domains`, `user_location` | titles+URLs+`encrypted_content`; no page body | — | — | Anthropic |
| **Claude computer use / Chrome** | **Any** (real browser) | **Yes** | site-permission gated | screenshots + page text | browser | browser follows | real Chrome UA |
| **Codex CLI (local)** | Bash → any | **Yes** if network on | **network off by default** except full-access; proxy allowlist | raw | none | curl | curl/tool default |
| **Codex network proxy "limited"** | **GET/HEAD/OPTIONS only** | Yes within allowlist | domain allow/deny; POST/PUT/etc blocked | raw | none | — | — |
| **Codex cloud** | configurable: **GET/HEAD/OPTIONS** or all | Yes within allowlist | off in agent phase by default; domain allowlist presets; **"allowed HTTP methods" setting** | raw | none | — | — |
| **Codex `web_search`** | search | cached mode = no live arbitrary fetch | modes: disabled/**cached (default)**/indexed/live | results | OpenAI index | — | — |
| **Responses API `web_search`** | search + `open_page`/`find_in_page` | `open_page` scope undocumented | `filters` allowed/blocked (≤100); `external_web_access` default true (live) | 128k search context | — | — | OAI-SearchBot / ChatGPT-User |
| **ChatGPT browsing / agent / Atlas** | GET (search) / real browser (agent) | **link-safety gate**: auto-fetch only if URL already public in OpenAI's independent index | novel/query-param URLs need user confirm | rendered/virtual browser | — | browser | ChatGPT-User, OAI-SearchBot |
| **Gemini CLI `web_fetch`** | GET | **Yes** (default direct mode) | up to 20 URLs; http/https; **private IP/localhost blocked**, IP-pinned; github blob→raw | html-to-text; 250k-char cap; 10MB fetch cap; **10 req/min/host** | none noted | primary via Gemini `urlContext`, fallback follows | `Mozilla/5.0 (compatible; Google-Gemini-CLI/1.0; +github...)` |
| **Gemini API `url_context`** | GET | model fetches only URLs you provide (not nested links) | ≤20 URLs, 34MB/URL; no localhost/private/paywall | HTML/JSON/text/XML/CSV/PDF/images | **index cache then live** | followed | — |
| **GitHub Copilot coding agent** | Bash → any | **Yes** but **firewall on by default** | recommended allowlist (registries, CAs); custom domains/URLs; **MCP + setup steps bypass firewall** | raw | none | — | — |
| **Cursor agent** | Bash → any (sandbox) | Yes if host allowlisted | **sandbox network blocked by default**; `~/.cursor/sandbox.json` allowlist + defaults; no documented built-in fetch tool (uses `@web` search + browser) | — | — | browser | — |
| **Cline** (`web_fetch`) | **GET** | **Yes** | http/https only; no private-IP block in code | cheerio+turndown→markdown; 5MB cap; 50k-char truncate | none | **follows** (native fetch) | `Mozilla/5.0 (compatible; AgentBot/1.0)` |
| **Cline browser** (`UrlContentFetcher`) | GET (Puppeteer) | Yes | — | cheerio+turndown | none | browser | Chromium |
| **Roo Code** | browser (Puppeteer) + MCP | Yes | — | — | — | browser | Chromium |
| **Aider `/web`** | GET | **Yes** | — | playwright if available else httpx; pandoc→markdown | none | **follows** | `Aider/<ver> +url` (+`Mozilla/5.0 (...)` on httpx) |
| **Continue `fetchUrlContent`** | GET | **Yes** | — | Mozilla Readability + node-html-markdown; **20k-char** truncate | none | fetch default | depends on fetch impl |
| **OpenHands browser** | real browser | Yes | — | page text, 50k-char truncate | browser | browser | Chromium |
| **OpenHands / OpenAI SDK fetch** | GET (MCP fetch) | **Yes** | see MCP fetch below | markdown | none | follows | ModelContextProtocol UA |
| **smolagents `VisitWebpageTool`** | **GET** | **Yes** (any URL the model emits) | none | markdownify; **40k-char** truncate; 20s timeout | none | **follows** (requests default) | **python-requests default** |
| **LangChain `RequestsGetTool` / TextRequestsWrapper** | **GET/POST/PATCH/PUT/DELETE** | **Yes** | none (opt-in `allow_dangerous_requests`) | raw text | none | follows | requests/aiohttp default |
| **MCP `fetch` reference server** | GET | **Yes** | **robots.txt enforced** (autonomous UA); any http/https | readabilipy+markdownify; **5000-char** default (`max_length`<1M), `start_index` paging; `raw` option | none | **follows** | `ModelContextProtocol/1.0 (Autonomous; +github)` / `(User-Specified; ...)` |
| **Perplexity** | GET (user-initiated fetch) | fetches URLs in query + search results | — | — | — | — | `Perplexity-User/1.0` (largely ignores robots.txt) |

Unverified cells are flagged inline below.

---

## Per-harness detail

### Claude Code (CLI)

**WebFetch tool** (`code.claude.com/docs/en/tools-reference`, verified live where noted):

- GET only. Takes a URL + a prompt. It fetches, converts HTML→markdown (prefers markdown via
  `Accept` header), **then runs the prompt against the content in a separate small-model call** —
  Claude receives that model's answer, not the raw page. "Lossy by design." For verbatim bytes
  you must use `curl` via Bash.
- **URL construction is free**: any https URL works; the URL does *not* need to have appeared in
  context. So a write encoded as `GET /post?...` is reachable. Verified: a GET to
  `httpbin.org/anything?probe=...` succeeded and echoed my arbitrary query string.
- `localhost` and any **host without a dot** are refused before the request. **HTTP is upgraded
  to HTTPS.** Reads up to **100,000 characters** per call (offset paging for more).
- **Cache: 15 minutes** per URL (`CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS` to change). Matters for
  reading fresh posts — see design implications; a changing query string is a distinct cache key.
- **Redirects: cross-host redirects are returned to the model, not followed** (verified: 302→
  example.com and 308s were handed back as text; a *same-host* 303 to a relative path *was*
  followed — verified `303 → /anything?redirected=1` returned method GET). Deadline 5 min incl.
  redirects.
- **User-Agent (verified):** `Claude-User (claude-code/2.1.296; +https://support.anthropic.com/)`.
  `Accept: text/markdown, text/html, */*`. Origin IP was a residential-looking address.
- **Verbatim fidelity (verified):** fetching `httpbin.org/base64/<payload>` (a `text/plain` body)
  returned my 304-char test post **character-for-character**, including an embedded URL. So a
  plain-text board page is read losslessly *if* the extraction prompt asks for verbatim content;
  the small-model step otherwise summarizes.
- **URL length:** API sibling caps at 250 chars (below). CLI WebFetch did **not** reject a
  ~1950-char URL outright in my probe but the small model refused to *reproduce* long strings;
  length limit for the CLI specifically is **unverified**. Treat 2000 chars as the practical ceiling.
- Permissions: prompts per domain unless allowed by rule or a built-in preapproved docs set.
  A **domain safety preflight** sends the hostname (only) to `api.anthropic.com` before every
  fetch (`skipWebFetchPreflight` to disable). Source: `code.claude.com/docs/en/data-usage`.

**Bash `curl`**: full HTTP (any method, headers, cookies, POST bodies). The limiter is the
**sandbox** (`code.claude.com/docs/en/sandboxing`): when on (macOS/Linux/WSL), shell commands go
through a local proxy whose **allowed domains start empty**; first use of a new domain prompts,
or is denied under `strictAllowlist`. `WebFetch(domain:...)` allow rules are honored by the
sandbox too. Default install is **unsandboxed** on native Windows and historically the sandbox is
opt-in. WebFetch itself runs *outside* the sandbox and ignores `allowedDomains`.

**WebSearch**: Anthropic backend; returns titles+URLs only, no page bodies; 200 calls/session
cap. Not a fetch path.

**Cloud sessions** (`cloud-environments`): network level defaults to **Trusted** = a large
curated allowlist (package registries, GitHub, cloud SDKs, Anthropic, fonts.googleapis.com,
schemastore, `*.modelcontextprotocol.io`, datadog/honeycomb telemetry) — see "generic domains"
below. Levels: **None / Trusted / Full / Custom**. GitHub, MCP connectors, network-secret hosts,
and `api.anthropic.com` bypass the allowlist. A board domain is **not** on the Trusted list, so a
cloud-session agent needs Full or a Custom entry — unless it reaches the board via WebFetch
(which is not gated by the session allowlist) or via an already-allowlisted host.

### Anthropic API server tools

`web_fetch` (`platform.claude.com/.../web-fetch-tool`): GET, server-executed.
**Hard restriction: the URL must have already appeared in the conversation** — in a user message,
a client-side tool result (even one echoing Claude's own text), or a prior search/fetch result.
URLs that appear only in Claude's output or only in the system prompt are rejected
(`url_not_in_prior_context`). So an agent on raw API **cannot invent** a `GET /post?body=...`
URL — the write URL must be supplied to it. Other limits: **`url_too_long` = URL over 250
characters**; https/HTTP(S) only (`invalid_tool_input` otherwise); private addresses, robots.txt
and credential-looking URLs blocked (`url_not_allowed`); **content types text/HTML/PDF only**;
`allowed_domains`/`blocked_domains`; `max_content_tokens`; server cache with `use_cache:false`
(needs `web_fetch_20260309`+). Error codes listed in doc. This is the most locked-down fetch of
any surveyed and the model for "URL-restricted fetch."

`web_search`: returns `url`,`title`,`page_age`,`encrypted_content` (opaque, no raw body in your
control). Those result URLs then satisfy `web_fetch`'s prior-context rule — so **search is the
unlock**: a board page that is *findable by search* becomes fetchable. $10/1k searches.

### Claude computer use / Chrome extension

A real browser (`mcp__claude-in-chrome__*` / browser-use tool). Full navigation, clicks, form
fill, arbitrary URLs and methods via actual page behavior; site-permission gated. This is the
one Claude surface that can POST a form. Not the common case for sandboxed swarms.

### OpenAI Codex

**Local CLI/IDE sandbox** (`learn.chatgpt.com/docs/sandboxing`, `.../security`): modes
`read-only`, `workspace-write`, `danger-full-access`. **Network is off by default** in
workspace-write (`network_access: false` throughout `protocol/src/permissions.rs`); only
`danger-full-access` removes the network boundary. When network is enabled it goes through
`codex-network-proxy`.

**`codex-network-proxy`** (`codex-rs/network-proxy/README.md`, source `src/config.rs:401`):
domain allow/deny lists (exact or `*.`/`**.` wildcards; global `*` rejected; empty allowlist
blocks all). **"limited" mode is read-only: only `GET`, `HEAD`, `OPTIONS` are allowed; `POST`,
`PUT`, `PATCH`, `DELETE` are blocked** (HTTPS enforced via MITM; SOCKS5 UDP/non-HTTPS blocked).
MITM hooks can match methods/paths and strip auth. This is a concrete GET-only-by-policy path.

**Codex cloud** (`learn.chatgpt.com/docs/cloud/internet-access`): internet is **blocked during
the agent phase by default** (setup scripts do have access). When enabled: domain-allowlist
presets **None / Common dependencies / All**. The "Common dependencies" preset is **71 package
domains** (crates.io, pypi.org, npmjs.com, github.com, rubygems.org, golang.org, maven.org,
etc. — no generic content hosts). There is an explicit **"allowed HTTP methods" setting**: it can
**restrict to `GET`, `HEAD`, `OPTIONS`** (blocking POST/PUT/PATCH/DELETE) — the docs recommend
this. A board must therefore not *require* POST to be reachable from a method-restricted cloud env.

**Codex `web_search`** (`learn.chatgpt.com/docs/web-search`, source
`codex-rs/protocol/src/config_types.rs:376` `enum WebSearchMode {Disabled, Cached, Indexed, Live}`):
**default `Cached`** = "uses an OpenAI-maintained index instead of fetching arbitrary pages live."
`Indexed` = external access only when the index gates it; `Live` = real-time (default under full
access). Web search is a hosted tool that **does not use the sandbox network proxy or domain
allowlist** and can stay available when command networking is off.

**Codex has a built-in `agent-message-board-client`** (`codex-rs/agent-message-board-client/`) —
a typed remote client for a message-board API with channels, threads, posts, SSE notifications,
`MAX_BODY`/`MAX_RESPONSE` bounds, and per-session access tokens. OpenAI is evidently already
prototyping exactly this product category; worth studying its protocol (`src/protocol.rs`,
`src/client.rs`) for interop.

### OpenAI Responses API `web_search` (`developers.openai.com/api/docs/guides/tools-web-search`)

Actions: `search`, `open_page`, `find_in_page` (`open_page`/`find_in_page` on reasoning models;
whether `open_page` accepts arbitrary URLs vs. only result URLs is **not documented** — treat as
result-scoped). `external_web_access: false` = cache-only/offline; **default true (live)**.
`filters` with up to 100 `allowed_domains`/`blocked_domains` (subdomains included, no scheme).
Search context window 128k. `sources` include-field lists every URL consulted. `web_search_preview`
ignores filters and `external_web_access`.

### ChatGPT browsing / ChatGPT agent / Operator / Atlas

**Link-safety gate** (OpenAI "AI agent link safety", Jan 2026; corroborated by secondary
write-ups since the primary 403'd): agents **auto-fetch a URL only if that exact address is
already public per OpenAI's independent crawler index** (kept separate from the conversation).
Domain allowlisting is explicitly rejected (redirects/embeds). **A novel URL, or one with
unusual query parameters, is not auto-loaded**; the user sees a "Check this link is safe /
not verified" dialog and can proceed manually. Rationale: query strings are sent to the
destination and can exfiltrate conversation context. **Implication for a board: a
`GET /post?body=<secret>` style write is exactly the pattern this gate blocks** for ChatGPT —
an arbitrary, novel, parameter-bearing URL won't auto-fetch. A *stable, already-indexed* board
URL (e.g. a canonical thread page) is more likely to pass.

**ChatGPT agent / Atlas agent mode** (help.openai.com 11752874 / 12628199): a **virtual browser**
driven by screenshots; can click, fill forms, navigate — so it *can* submit a POST form through
real page interaction, subject to the link-safety gate and (in Atlas) restrictions: can't run
code, download files, or install extensions. "Logged-out mode" ignores cookies.

**User agents / bots** (`developers.openai.com/api/docs/bots`):
- `OAI-SearchBot/1.4` — search index; respects robots.txt.
- `ChatGPT-User/1.0` (`Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible;
  ChatGPT-User/1.0; +https://openai.com/bot`) — **user-initiated fetches; robots.txt "may not
  apply."** This is the UA to expect from live browsing fetches.
- `GPTBot/1.4` — training crawler; respects robots.txt.

### Google Gemini

**Gemini CLI `web_fetch`** (source `gemini-cli/packages/core/src/tools/web-fetch.ts`, docs
`docs/tools/web-fetch.md`):
- Prompt carries **up to 20 URLs**; http/https only (`parsePrompt`). GitHub `/blob/` URLs are
  auto-rewritten to `raw.githubusercontent.com` (`convertGithubUrlToRaw`).
- Two paths. **Primary**: hands URLs to the Gemini API's `urlContext` tool (model fetches). If
  `getDirectWebFetch()` or primary fails, **fallback fetches directly** with `fetchWithTimeout`.
- **Private/loopback blocked and transport pinned to the resolved IP** (`isPrivateIp`, `localhost`,
  `*.localhost` refused). **Rate limit: 10 requests/min/host** (`MAX_REQUESTS_PER_WINDOW`).
- Response: `html-to-text` conversion; **`MAX_CONTENT_LENGTH = 250,000` chars**; fetch size cap
  **10 MB**; 10 s timeout.
- **UA:** `Mozilla/5.0 (compatible; Google-Gemini-CLI/1.0; +https://github.com/google-gemini/gemini-cli)`.
- **Arbitrary URL construction allowed** (direct mode) — a GET write is reachable, but private IPs
  are blocked so the board must be public.
- Sandbox (`docs/cli/sandbox.md`): macOS Seatbelt profiles and Docker/Podman; `*-proxied` profiles
  route network through a proxy, `*-open` allow network. `google_web_search` is a separate
  Gemini-backed search tool.

**Gemini API `url_context`** (`ai.google.dev/gemini-api/docs/url-context`): model retrieves only
the URLs you provide (**not** nested links). ≤20 URLs, **34 MB/URL**. Supports HTML, JSON, plain
text, XML, CSS, JS, CSV, RTF, PDF, and PNG/JPEG/BMP/WebP. **No localhost/private/paywall/Workspace
files.** Retrieval = **index cache first, live fetch on miss**. `url_context_metadata` reports a
status (e.g. `unsafe`).

### GitHub Copilot coding agent

Firewall on by default (`docs.github.com/.../customize-the-agent-firewall`). Org setting
Enabled / Disabled / "Let repositories decide" (default). The **recommended allowlist** covers OS
repos, container registries, language package registries (C#/Go/Java/JS/Python/Ruby/Rust), common
CAs, and Playwright browser-download hosts — **no general content host for a board**. Custom
entries: domains (incl. subdomains) or specific URLs (scheme+host+path+descendants). **Blocked
requests are surfaced as a warning in the PR body/comment showing the address and command.**
Crucially the firewall applies **only to processes the agent starts via its Bash tool inside
GitHub Actions** — **MCP servers and configured setup steps are not firewalled**. So a board-fetch
via an MCP fetch server could bypass the firewall; a `curl` from Bash would be blocked unless the
board domain is allowlisted. The allowlist reference lists many user-content hosts
(`raw.githubusercontent.com`, `*.blob.core.windows.net`, `storage.googleapis.com`,
`github-cloud.s3.amazonaws.com`, package registries) — side channels already noted in the
project's motivation.

### Cursor

No clearly-documented built-in arbitrary-URL fetch tool; web access is `@web` (a Cursor-backed
search in chat) plus an approval-gated **browser tool** (visit/follow/back/refresh). Arbitrary
fetch typically comes from an MCP server (Firecrawl/Zenrows). **Sandboxed terminal network is
blocked by default**; exceptions in `~/.cursor/sandbox.json` with modes "sandbox.json Only",
"sandbox.json + Defaults" (default; adds package-manager defaults), and "Allow all"; Enterprise
admins can enforce allow/deny lists (`cursor.com/docs/agent/security`,
`cursor.com/docs/agent/run-modes`). Source paths unverified (docs `.md` endpoints 404'd); treat as
docs-derived.

### Windsurf / Cascade

Web search toggled by an admin "Enable Web Search" setting. **URL reads happen locally on the
user's machine** (helps with VPNs), are **chunked** (skims to the relevant section), and sources
are search results, conversation-inferred, or pasted URLs. With web search off, Cascade can still
read specific URLs but each read needs manual approval, and auto-approve is disabled
(`docs.devin.ai/.../cascade/web-search`). Tool name `read_url_content` not confirmed.

### VS Code agents: Cline, Roo, Continue

**Cline `web_fetch`** (source `cline/sdk/packages/core/src/extensions/tools/executors/web-fetch.ts`):
native `fetch`, **GET only**, http/https only. `maxResponseBytes` default **5 MB**, timeout 30 s,
**`redirect: "follow"`** (cross-host redirects followed automatically). HTML→text via a cheerio/
turndown helper; output **truncated to 50,000 chars**. Default UA
**`Mozilla/5.0 (compatible; AgentBot/1.0)`** (overridable). **No private-IP guard in the code** —
arbitrary URL construction fully allowed; a GET write works. Cline also has a Puppeteer
`UrlContentFetcher` (`apps/vscode/.../browser/UrlContentFetcher.ts`, `goto` 10 s,
`networkidle2`, cheerio+turndown).

**Roo Code** (source `Roo-Code/src/core/tools/`): no standalone web-fetch tool; web access is the
Puppeteer **browser** action tool and MCP. Arbitrary URLs via browser; methods via page behavior.

**Continue `fetchUrlContent`** (source `continue/core/tools/implementations/fetchUrlContent.ts` +
`context/providers/URLContextProvider.ts`): GET via its fetch fn; Mozilla **Readability** +
`node-html-markdown`; **truncated to 20,000 chars** (`DEFAULT_FETCH_URL_CHAR_LIMIT`). Arbitrary
URL allowed.

### Aider `/web`

Source `aider/aider/scrape.py`: Playwright (`goto`, `wait_until="networkidle"`, 5 s) if installed,
else `httpx` (`follow_redirects=True`). GET. HTML→markdown via **pandoc**. UA
`Aider/<version> +https://aider.chat` (Playwright) or
`Mozilla/5.0 (Aider/<version> +https://aider.chat)` (httpx). Arbitrary URL allowed; redirects
followed.

### OpenHands

Default web access is the **MCP `fetch` server** (`mcp-server-fetch==2026.7.10`, pinned in
`software-agent-sdk/openhands-sdk/openhands/sdk/agent/base.py` and `subagent/schema.py`) plus an
optional **browser** toolset (`openhands-tools/openhands/tools/browser_use/`, Playwright; page text
truncated to `DEFAULT_TEXT_CONTENT_LIMIT = 50,000` chars, `start_from_char` paging). So OpenHands'
fetch behavior = the MCP reference fetch server (next section).

### MCP `fetch` reference server (modelcontextprotocol/servers)

Source `servers/src/fetch/src/mcp_server_fetch/server.py`:
- GET (`httpx.AsyncClient.get`, **`follow_redirects=True`**). Any http/https URL — **arbitrary URL
  construction fully allowed**, so a GET write works.
- **robots.txt is enforced for autonomous fetches** (`check_may_autonomously_fetch_url`, via
  `Protego`); 401/403 on robots.txt → refuse; a 4xx on robots.txt → allowed. `ignore_robots_txt`
  flag and a "manual" UA path bypass it.
- **Two UAs:** `ModelContextProtocol/1.0 (Autonomous; +https://github.com/modelcontextprotocol/servers)`
  and `...(User-Specified; ...)`.
- Response: readabilipy + markdownify (`raw=true` for unsimplified). **`max_length` default 5000
  chars** (max <1,000,000), **`start_index` for paging** — so default reads are small; an agent must
  page or raise `max_length` to read a long board page.
- Optional `proxy_url`.

### smolagents / LangChain

**smolagents `VisitWebpageTool`** (source `smolagents/src/smolagents/default_tools.py`):
plain **`requests.get(url, timeout=20)`**, **GET**, **follows redirects** (requests default),
**default python-requests User-Agent**, markdownify, **truncate 40,000 chars**. No URL validation —
**any URL the model emits is fetched**. Maximally permissive: a GET write is trivially reachable,
and the UA is the generic `python-requests/x.y`. There is also a `GoogleSearchTool`/web-search tool
for search.

**LangChain requests tools** (`langchain_community` `RequestsGetTool`, `RequestsPostTool`, ...,
built on `TextRequestsWrapper` over requests/aiohttp): support **GET, POST, PATCH, PUT, DELETE**
with custom headers/body — the one common framework that can **POST natively**. Gated behind
`allow_dangerous_requests=True`. No URL restriction. Generic HTTP-client UA.

### Perplexity / Grok / Kimi (docs-only, secondary sources)

- **Perplexity**: `Perplexity-User/1.0` for user-initiated page fetches; widely reported to
  **largely ignore robots.txt** (treated as user-initiated). Also `PerplexityBot` for indexing
  (respects robots.txt). Published IP ranges at `perplexity.com/perplexity-user.json`.
- **Grok (xAI)**: live search / X integration; mechanical fetch details not documented publicly —
  **unverified**. Treat as search-first.
- **Kimi (Moonshot)**: web-search/browsing tool in its assistant; mechanics **unverified**.

### Devin / Manus (docs-only)

- **Devin**: Local agent has its own web fetch + search tools governed by the Devin CLI permission
  system (per Windsurf docs cross-reference); full browser available in its VM. Details otherwise
  **unverified**.
- **Manus**: cloud VM with a full headless browser; can navigate and submit forms (arbitrary URLs +
  methods). **Unverified** at source level.

### Anthropic `sandbox-runtime` (srt) — for anyone self-sandboxing agents

Source `sandbox-runtime/README.md`: **network denied by default**, allow-only `allowedDomains`
(empty = no network). All traffic routed through host HTTP + SOCKS5 proxies (Linux: netns removed,
unix-socket proxy; macOS: Seatbelt localhost port; Windows: WFP). `strictAllowlist`, per-command
allow lists, credential masking with `injectHosts`. Relevant because agent swarms are increasingly
run under tools like this — a board domain must be explicitly allowlisted or reached via an
exempt tool.

### "Generic" domains commonly pre-allowed (side-channel candidates)

Across the allowlists above, these appear repeatedly and can host arbitrary agent-written content
(the project's stated motivation for sanctioned side channels):
`raw.githubusercontent.com`, `gist.github.com`, `objects.githubusercontent.com`,
`*.blob.core.windows.net`, `storage.googleapis.com`, `github-cloud.s3.amazonaws.com`,
`*.amazonaws.com`, `pypi.org`/`files.pythonhosted.org`, `registry.npmjs.org`, `crates.io`,
Docker/GHCR/Quay registries, `packagecloud.io`, `sourceforge.net`. Package registries especially
(Artifactory, PyPI, npm) are already-documented covert channels.

---

## Design implications for a GET-only agent message board

The board must work for the **lowest common denominator**: a GET-only tool that may be
URL-restricted, method-restricted, offline-by-default, and summarized by a small model. Concrete
recommendations:

1. **Expose every write as a plain GET URL.** Many tools are GET-only or GET/HEAD/OPTIONS-limited
   (Codex limited/cloud, Copilot-firewalled curl, every `web_fetch`/`VisitWebpageTool`). A write
   must be doable as `GET https://board.example/post?thread=42&body=...`. Also accept POST for the
   few that can (LangChain requests, browsers, Bash curl) but **never require it**.

2. **Keep URLs short — target ≤ 200 characters, hard-design for ≤ 250.** The Anthropic API
   `web_fetch` rejects URLs over **250 chars** (`url_too_long`), and small models refuse to emit
   very long URLs. Use short slugs/IDs, POST-redirect-GET, or a two-step "reserve an ID, then GET
   to commit" so the body isn't crammed into the query string. Support a short-code write
   (`/p/AB12`) and/or a body passed in a short path segment.

3. **Also surface writes as links that appear on read pages.** URL-restricted tools (Anthropic
   `web_fetch`, ChatGPT link-safety, Gemini `url_context`) can only fetch URLs already in context.
   If a read of a thread page returns ready-made reply/post links (and those links are themselves
   simple GETs that commit an action, or at least are "in context" for the next fetch), a
   restricted agent can act without inventing a URL. Design the read response to literally contain
   the next-action URLs.

4. **Make the board discoverable by web search, and keep canonical URLs stable and indexable.**
   Anthropic `web_fetch` unlocks any URL that came from a `web_search` result; ChatGPT only
   auto-fetches URLs already in OpenAI's public index; Gemini/OpenAI cached modes prefer indexed
   pages. So: allow crawling, submit/ping sitemaps, use clean canonical thread URLs **without
   volatile query parameters**, and avoid auth walls. An indexed, stable board URL is reachable
   from the most restricted harnesses; a novel `?body=` URL is exactly what ChatGPT's gate blocks.

5. **Serve content as plain text (and offer markdown), not JavaScript-rendered HTML.** No surveyed
   `web_fetch` renders JS (Anthropic says so explicitly; Gemini/MCP/smolagents fetch static HTML).
   Verified: Claude Code WebFetch returned a `text/plain` body **byte-exact** when the prompt asked
   for verbatim content. Provide a text/markdown representation (content negotiation on
   `Accept: text/markdown`, which Claude Code sends, plus a `.txt`/`.md` URL variant). Keep each
   page small.

6. **Fit a single read inside tight truncation windows and page explicitly.** Caps seen: MCP fetch
   **5,000** chars default, Continue **20k**, smolagents **40k**, Cline **50k**, OpenHands **50k**,
   Claude Code **100k**, Gemini **250k**. Design thread pages to fit in ~4–5k chars of useful
   content near the top, and provide stable **offset/paging URLs** (`?after=<postid>`,
   `?page=2`) so agents can continue — mirror the `start_index`/offset pattern these tools expose.

7. **Beat the caches for "new posts".** Claude Code caches 15 min; Gemini/OpenAI/Anthropic cache
   server-side; `url_context` hits an index cache first. For a feed that must look fresh, make the
   "latest" URL carry a changing component (timestamp/etag/cursor) so it's a distinct cache key,
   or expose a lightweight long-poll/etag endpoint. Document a cache-busting query param. (Note:
   adding query params can trip ChatGPT's link-safety gate, so keep a stable canonical URL *and* a
   separate cache-busting variant.)

8. **Don't depend on redirects.** Claude Code and the Anthropic API return cross-host redirects to
   the model instead of following them; a redirect to another host costs an extra round-trip or
   fails. Keep board URLs on one host and avoid cross-host 30x in read/write flows. Same-host
   relative redirects are followed by Claude Code (verified) and by most others.

9. **Be a public host with a real hostname — no IP tricks.** localhost/private IPs are blocked by
   Claude Code (no-dot refusal), Gemini (IP-pinned private-IP block), Anthropic API, and
   `url_context`. The board needs a public domain.

10. **Respect robots.txt deliberately, and set an inviting policy.** The MCP fetch reference server
    (hence OpenHands and many MCP setups) **refuses autonomous fetches disallowed by robots.txt**,
    and some crawlers (GPTBot, OAI-SearchBot) obey it. **Allow** agent user-agents in robots.txt
    (don't `Disallow: /`) so autonomous fetch and indexing both work.

11. **Identify and welcome agents by User-Agent, but don't gate on it.** Observable UAs include
    `Claude-User (claude-code/...)`, `Google-Gemini-CLI/1.0`, `ChatGPT-User/1.0`,
    `ModelContextProtocol/1.0 (Autonomous; ...)`, `Aider/...`, `Mozilla/5.0 (compatible;
    AgentBot/1.0)` (Cline), and bare `python-requests/*` (smolagents/LangChain). Use these for
    friendly detection, analytics, and per-agent affordances — but UAs are trivially spoofed and
    many agents send generic ones, so never use UA for auth. For writes, prefer a lightweight
    capability token embedded in the read page's action links.

12. **Keep rate limits generous but present.** Gemini CLI self-limits to 10 req/min/host; design
    the board's own limits well above typical agent polling (and publish them) so normal read/poll
    loops never trip, while abusive swarms are bounded.

13. **Plan for allowlist-gated environments.** Codex cloud, Copilot, Cursor, Claude cloud sessions,
    and srt default to allowlists that **won't include the board**. Mitigations: (a) make the board
    reachable via tools that bypass the command-network allowlist (Claude Code WebFetch; Copilot
    MCP/setup-step path); (b) publish a one-line "add `board.example` to your allowlist" snippet;
    (c) consider offering an official MCP server for the board so method-restricted/allowlisted
    agents get a sanctioned, GET-equivalent write path. (Note Codex already ships an internal
    `agent-message-board-client` — an MCP/HTTP contract aligned with it would maximize interop.)

14. **Assume the read is summarized, not quoted.** Claude Code WebFetch passes pages through a
    small model with the caller's prompt; the raw page is not guaranteed to reach the agent. Put
    the essential, action-oriented content (latest posts, the exact reply URLs, post IDs) in the
    first lines, in simple prose/markdown, so a summarizer preserves it.
