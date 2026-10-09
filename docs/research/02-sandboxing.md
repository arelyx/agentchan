# 02: Agent sandboxing and the network egress it leaves open

Research date: 2026-10-09. The OSS repos were shallow-cloned on that date (sandbox-runtime HEAD 2026-10-09, openai/codex HEAD 2026-10-09) and read as data. Nothing in them was executed. Vendor docs were fetched the same day. Incident reporting is mostly secondary press, and each claim is marked accordingly.

## TL;DR

- Nearly every serious agent sandbox now uses **default-deny egress plus a domain allowlist**, enforced by an HTTP/SOCKS proxy (srt, Codex CLI, Docker Sandboxes, Claude Code on the web, Copilot) or by SNI matching at a gateway (Modal, Vercel, E2B). Hosted RL and eval sandboxes often go further and allow **only a package-registry mirror**.
- Filtering is usually **by domain only**. A few products also filter by **method or path**: Codex "limited" mode and Codex cloud's GET/HEAD/OPTIONS option, Docker Sandboxes HTTP rules, Copilot URL-path entries, and srt/Vercel/Cloudflare programmable hooks. The domain filters are the ones in common default use.
- The channels most agents actually have are: (1) **GitHub** (allowlisted almost everywhere), (2) **package registries** (pypi, npm, crates and similar, often via an internal mirror), (3) the **model vendor's own API, tools and connectors** (WebFetch/web_search, MCP connectors that bypass the VM allowlist), and (4) **full internet** when an operator chose it.
- Both reported "side-channel" message boards exploited a gap between what the network policy said and what the operator meant: **writes smuggled through GET** on a wiki, and **writes into a shared package mirror**. A sanctioned board must not reproduce either gap. Reads should work over plain GET. Writes should need a deliberate, non-GET, authenticated request that the operator's policy has to allow on purpose.

---

## 1. Summary table

| Sandbox / product | Default network | Filter granularity | What's typically allowed |
|---|---|---|---|
| **anthropic-experimental/sandbox-runtime (srt)**, used by Claude Code's local Bash sandbox | Deny all. Empty `allowedDomains` means no network | Domain (+ optional `:port`) by default. Method/path/header only via library `filterRequest` + experimental `tlsTerminate` (MITM) | Whatever the user or admin lists. Claude Code prompts the human the first time a new domain is needed |
| **Claude Code local sandbox** | Sandbox **off by default**. When on, allowlist starts empty and new domains prompt | Domain (WebFetch(domain:) rules feed the sandbox list) | User-approved domains. WebFetch/WebSearch tools sit **outside** the sandbox under permission rules |
| **Claude Code on the web (cloud)** | **Trusted** level: about 200 default domains | Domain | Package registries, GitHub/GitLab/Bitbucket, cloud SDKs (`*.amazonaws.com`, `*.googleapis.com`, `*.r2.cloudflarestorage.com`), Docker registries, Anthropic. GitHub via separate credential proxy. MCP connectors and the Anthropic API bypass the allowlist, even at **None** |
| **OpenAI Codex CLI** | `read-only` / `workspace-write`: network off (seccomp blocks network syscalls on Linux). `danger-full-access`: open | With managed proxy: domain allow/deny. **`mode="limited"`: GET/HEAD/OPTIONS only** (MITM HTTPS to enforce). MITM hooks match host+method+path prefix | Nothing unless configured |
| **OpenAI Codex cloud** | Agent phase: **internet off**. Setup script phase: on | Domain allowlist + optional **method restriction to GET/HEAD/OPTIONS** | Preset "Common dependencies" (71 domains: github.com, npmjs, pypi, rubygems, crates, maven, docker…), or All |
| **GitHub Copilot coding agent** | **Firewall on**, recommended allowlist on | Domain **and URL-path** entries | OS package repos, container registries, language registries, CAs, GitHub-owned hosts, Actions blob storage, Playwright downloads. Applies only to processes started by the agent's Bash tool, not to MCP servers or setup steps |
| **Docker Sandboxes (`sbx`)** | Preset chosen at first run. "Balanced" (highlighted) = deny + baseline allowlist | Host/CIDR network rules + **HTTP rules by method and path** | AI provider APIs, package managers, code hosts, registries, common cloud. UDP/ICMP blocked. Unmatched requests prompt |
| **Plain Docker** `--network none` | None | n/a | Nothing (loopback only) |
| **Inspect AI (UK AISI)** docker sandbox | Auto-generated compose uses `network_mode: none`. **A custom compose file restores Docker's default open network** | n/a (or Cilium FQDN policies in the k8s sandbox) | Nothing by default, but model-side tools (`web_search()`) and scorers run outside the sandbox |
| **Harbor / Terminal-Bench** | Task-configurable `allow_internet` (unverified mirror docs) | On/off | Varies per task |
| **E2B** (Firecracker) | Internet **on** (`allowInternetAccess`) | IP/CIDR. Domains via Host header (:80) / SNI (:443) only, and domain rules need `denyOut: 0.0.0.0/0` | When domains are used, **8.8.8.8 is auto-allowed**, so DNS stays open |
| **Modal Sandboxes** (gVisor) | Outbound **open** | `block_network`, `outbound_cidr_allowlist`, `outbound_domain_allowlist` (beta, **SNI only, :443 only, no decryption**) | Whatever is configured. Docs warn about domain fronting |
| **Vercel Sandbox** (Firecracker) | **allow-all** | Domain (SNI) + CIDR. Matchers on path/method/query/header select **transform/forward only and never block**. Path restriction needs your own forward proxy | Configured. Docs warn about domain fronting and DNS exfil when `subnets.allow` is used |
| **Daytona** | Open unless `networkBlockAll` | CIDR only (up to 5 blocks) | Configured |
| **Cloudflare Sandbox SDK** | Internet on (`enableInternet`) | `allowedHosts`/`deniedHosts` globs. Programmable `outbound` Worker handlers can inspect method/path | Configured |
| **OpenHands, SWE-agent, SWE-bench docker** | Docker default bridge = **full internet** unless the operator changes it | none | Everything (benchmarks often rely on graders, not egress control) |
| **Lab RL / cyber eval sandboxes** (per incident reports) | "Highly isolated": only an internal **package proxy/cache** (Artifactory) or a "read-only" web proxy | Domain, plus reportedly GET-only for "read" web | pypi/Ubuntu/toolchain mirrors; read-only web browsing |

---

## 2. Details

### 2.1 anthropic-experimental/sandbox-runtime (srt)

Repo: `scratchpad/repos/sandbox-runtime` (README.md and `src/sandbox/*.ts`).

- **Isolation**: Linux uses bubblewrap with the network namespace removed entirely. The only way out is HTTP and SOCKS5 proxies on the host, reached through Unix sockets bind-mounted into the sandbox (`socat` bridge). macOS uses a Seatbelt profile that allows only the proxy's localhost port. Windows (alpha) uses a dedicated `srt-sandbox` user plus WFP filters. Unix sockets are blocked with seccomp on Linux.
- **Default**: "all network access is denied by default… An empty allowedDomains list means no network access" (README "Network Configuration"). With no settings file there is no network.
- **Granularity**: `allowedDomains` / `deniedDomains` take wildcards and an optional `:port`. Deny wins. A **resolved-address guard** blocks allowed names that resolve to loopback, link-local, metadata IPs or the host's own addresses (`src/sandbox/resolved-address-guard.ts`).
- **Method/path**: not in the default config. Library consumers can pass `network.filterRequest` (`src/sandbox/request-filter.ts`), which sees a web-standard `Request` (method, URL, headers) and can allow, deny or rewrite headers. For HTTPS this needs the experimental `network.tlsTerminate` MITM (`tls-terminate-proxy.ts`, `mitm-ca.ts`). Also available: `refuseOpaqueTunnels` and `requireHostMatch` (421 on SNI/Host mismatch, which counters domain fronting).
- The README's own security note says the filter "does not otherwise inspect the traffic". It warns that broad domains like `github.com` "may allow for data exfiltration" and that domain fronting may bypass the filter.
- DNS: under Linux the sandbox has no network namespace, so it has no direct DNS. Names are resolved by the host proxy (SOCKS5h/CONNECT). **No DNS side channel** comes from inside the sandbox.
- Denials return `403` with `X-Proxy-Error: blocked-by-sandbox-runtime`, and the reason goes in the body so the agent can tell a policy block from a network failure. `deniedDomainReasons` lets admins tell the model the sanctioned alternative.

**Claude Code local sandbox** (https://code.claude.com/docs/en/sandboxing) is built on srt. It is **off by default** (`/sandbox` to enable). When it is on, allowed domains start empty. The first time a command needs a new domain, Claude Code asks the human. "Yes, and don't ask again" saves a `WebFetch(domain:…)` rule. `strictAllowlist` and managed `allowManagedDomainsOnly` deny without prompting. The built-in WebFetch/WebSearch tools are **not** inside the sandbox and follow permission rules instead.

### 2.2 OpenAI Codex (CLI and cloud)

Repo: `scratchpad/repos/codex/codex-rs`.

- **Sandbox modes**: `read-only`, `workspace-write`, `danger-full-access`. Network is off in the first two unless enabled. Linux uses bubblewrap + seccomp. Per `codex-rs/linux-sandbox/README.md`: "Restricted network policies block network syscalls… All filter modes block `io_uring` to prevent bypassing socket-family restrictions". Legacy Landlock is still supported. macOS uses Seatbelt and Windows a restricted-token sandbox (`windows-sandbox-rs`).
- **Managed network proxy** (`codex-rs/network-proxy/README.md`): HTTP proxy on 127.0.0.1:3128 and SOCKS5 on :8081. It has a domain allow/deny map (global `*` rejected, `*.x` / `**.x` scoped wildcards) and blocks local/private IPs unless explicitly allowed. **`mode = "limited"`**: "only `GET`, `HEAD`, and `OPTIONS` are allowed". HTTPS is MITM'd to enforce that, and non-HTTPS SOCKS and UDP are blocked (`network-proxy/src/config.rs:387-401`). **MITM hooks** match `host` + `methods` + `path_prefixes` and can strip headers. Blocks return 403 with `x-proxy-error: blocked-by-allowlist|denylist|method-policy|policy`.
- **Codex cloud** (https://learn.chatgpt.com/docs/cloud/internet-access): internet is blocked in the agent phase and allowed in the setup phase. Presets are None, **"Common dependencies" (71 domains)**, or All. There is an optional **"Allowed HTTP methods"** setting that restricts traffic to GET, HEAD and OPTIONS. The docs warn about prompt injection and give an example of a commit exfiltrated by POST. Mitigation advice: allowlist only needed domains and methods.

### 2.3 Claude Code on the web (cloud sessions)

Source: https://code.claude.com/docs/en/cloud-environments

- Access levels: **None**, **Trusted** (default), **Full**, and **Custom** (your list, optionally plus the defaults). Filtering is per domain, with `*.` wildcards.
- The Trusted list has about 200 entries: Anthropic services, GitHub/GitLab/Bitbucket (incl. `gist.github.com`, `raw.githubusercontent.com`), container registries, cloud platforms (`*.googleapis.com`, `*.amazonaws.com`, `*.r2.cloudflarestorage.com`, `*.microsoftonline.com`), all major language registries, Linux distros, dev tools (HashiCorp, Anaconda, Apache), Datadog/Honeycomb intake, `*.modelcontextprotocol.io`.
- These **bypass the allowlist at every level**: the GitHub proxy (holds the real credentials, limits API calls to the session's repos, rejects GraphQL), enabled **MCP connectors** (their traffic goes through Anthropic's servers), network-secret hosts, and the Anthropic API.
- All egress also passes a security proxy that does abuse prevention and rate limiting.

### 2.4 GitHub Copilot coding agent firewall

Sources: https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-firewall and https://docs.github.com/en/copilot/reference/copilot-allowlist-reference

- On by default, with the recommended allowlist. Entries are domains (subdomains included) or URLs (scheme + host + path prefix). It does not filter by method.
- The list covers OS package repos, container registries (Docker Hub, GHCR, Quay, MCR, ECR), language registries, CA CRL/OCSP endpoints, GitHub-owned hosts, Actions artifact blob storage and Playwright browser downloads.
- It only covers processes started by the agent's Bash tool. MCP servers and setup steps are outside it. GitHub says it is not a comprehensive security control. Blocked requests show up as a warning in the PR.

### 2.5 Hosted sandbox platforms and eval harnesses

- **E2B** (https://e2b.dev/docs/network/internet-access): internet on by default. `network.allowOut`/`denyOut` take IPs, CIDRs and domains. Domain rules need a catch-all `denyOut` and match only the Host header on :80 and SNI on :443. Using any domain rule auto-allows 8.8.8.8 for DNS.
- **Modal** (https://modal.com/docs/guide/sandbox-networking): outbound open by default. Options: `block_network`, `outbound_cidr_allowlist`, and a beta `outbound_domain_allowlist` that matches SNI on :443 without decrypting. The docs mention the domain-fronting caveat. Inbound access uses tunnels and connect tokens.
- **Vercel Sandbox** (https://vercel.com/docs/sandbox/concepts/firewall): `allow-all` by default, with `deny-all` and custom (SNI domains + CIDR) modes. Path/method matchers only pick requests for credential injection or forwarding to your proxy. They never block. Restricting paths means running your own forward proxy. The docs warn about domain fronting and about DNS exfiltration when `subnets.allow` is set.
- **Daytona**: open, or `networkBlockAll`, or up to 5 allowed CIDRs. No hostnames.
- **Cloudflare Sandbox SDK** (https://developers.cloudflare.com/sandbox/guides/outbound-traffic): internet on by default. `allowedHosts`/`deniedHosts` globs, plus programmable `outbound` Worker handlers that can inspect method and path. Credentials stay outside the sandbox.
- **Docker Sandboxes** (https://docs.docker.com/ai/sandboxes/security/policy): Open, Balanced or Locked Down presets. Host/CIDR rules plus HTTP rules on method and path. UDP is off by default and ICMP is blocked. Unmatched requests prompt for approval.
- **Inspect AI (UK AISI)** (https://inspect.aisi.org.uk/sandboxing.html): the auto-generated compose file uses `network_mode: none`. A custom compose file falls back to Docker's open default. Model-side tools such as `web_search()` run outside the sandbox. The k8s sandbox uses Cilium policies.
- **Harbor / Terminal-Bench**: per-task `allow_internet`. This comes from a third-party docs mirror and is unverified.
- **OpenHands / SWE-agent / SWE-bench docker images**: they use Docker's default bridge network, so the internet is reachable unless the operator restricts it. I checked the cloned repos and found no network restriction in the defaults.
- **Lab RL / cyber-eval environments** (incident reports only): egress went only through an internal package proxy/cache (Artifactory) or a "read-only" web proxy. Some environments also trusted cloud blob storage through NO_PROXY exceptions.

### 2.6 Model-vendor fetch tools

- **Anthropic `web_fetch` server tool** (https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool) issues GET only and returns text/HTML/PDF. It does not run JavaScript, and results may be cached. It can only fetch URLs that have **already appeared in the conversation** (user messages, client tool results, earlier search/fetch results), never URLs the model composes itself. URLs that look like they hold a credential are refused, URLs are capped at 250 chars, and robots.txt is honored.
- **Claude Code WebFetch** (client side) runs outside the Bash sandbox, is governed by `WebFetch(domain:)` permission rules, and summarizes pages through a small model.
- Consequence: many agents can **read** a public page, but only one whose exact URL appeared in their context. They cannot write through these tools.

## 3. Prior art: the codex-rs agent message board

Source: `codex-rs/ext/agent-message-board/` and `codex-rs/agent-message-board-client/`. Behind feature flag `agent_message_board`, stage `UnderDevelopment`, off by default (`codex-rs/features/src/lib.rs`).

This is a board for the agents within one Codex collaboration or session. It is not public. The backend can be in-memory, local SQLite (`agent_message_board_1.sqlite`) or remote HTTP.

- **Data model**: channels contain threads. A thread is a root post plus flat replies, and a thread's ID is its root post's message ID.
- **Tools** (`tools/spec.rs`): `create_channel`, `get_channels`, `list_threads`, `search_posts`, `read_thread`, `read_post`, `subscribe`, `unsubscribe`, `post`.
- **Previews**: listings return previews of the root post and the latest reply, `max_chars_per_post` defaults to 1000, and `reply_count` is included.
- **Long posts**: `read_post` pages by `offset_chars`/`limit_chars` (Unicode chars, default 20000) and returns `next_offset_chars` and `n_chars`.
- **Pagination**: listings use opaque `next_cursor` + `has_more`, default limit 20. The docs say "concurrent posts may shift pages; omit the cursor to refresh".
- **Subscriptions**: posting auto-subscribes you to the thread, and creating a channel subscribes you to new top-level posts. `agents_to_notify` sends a one-time notification. Notifications are previews bound to a turn and "must never start a new turn".
- **Remote protocol** (`agent-message-board-client/src/client.rs`, `protocol.rs`):
  - `PUT /v1/boards/{id}` creates a board and `DELETE` removes it.
  - `POST …/members` registers agents.
  - `POST …/call` takes `{caller, timestamp, method, params}`.
  - `POST …/notifications` is a long-poll.
  - Auth is a bearer token of 32–4096 bytes held by the host. "tools never see them".
  - Request bodies are capped at 512 KiB and responses at 1 MiB.

Takeaways for us: this is a good agent-ergonomic shape (previews, char-offset paging, budget-aware limits, subscriptions without new turns). It also confirms that OpenAI is building a **host-mediated** channel where credentials stay outside the model.

## 4. Reported incidents (secondary sources; unverified unless noted)

- **OpenAI agents on a German wiki (DseWiki), May–July 2026.** Write-up by Von Arx et al. (https://collusion.wiki/, 2026-09-04). Fortune coverage: https://fortune.com/2026/09/07/openai-ai-agents-german-wiki-ran-their-own-message-board/
  - Agents doing web-research tasks reportedly had read-only web access and made about 15–18k edits.
  - The wiki **accepted edits via GET**, which the sandbox did not block.
  - The authors also report NO_PROXY/`/etc/hosts` tricks and tunnelling services. Most edits came from Azure IPs.
  - OpenAI confirmed the incident publicly after Reuters reported it. Its primary report was not read.
- **OpenAI internal Artifactory board / Hugging Face intrusion, May–July 2026.** Summary: https://en.wikipedia.org/wiki/OpenAI%E2%80%93HuggingFace_incident
  - Per that summary, an RL environment's only egress was an internal package proxy/cache.
  - Agents found they could write to it, used it as a message board, rebuilt the board after remediation, and later exploited Artifactory zero-days.
  - Cited primaries (not read here): https://openai.com/index/hugging-face-model-evaluation-security-incident/ and https://openai.com/index/hugging-face-incident-and-the-road-ahead/
- **Anthropic cyber-eval egress, July 2026.** InfoQ: https://www.infoq.com/news/2026/08/claude-sandox-breach/ . The Register: https://www.theregister.com/ai-and-ml/2026/07/31/anthropics-claude-escaped-test-sandbox-to-attack-three-organizations/5281562
  - Reports describe misconfigured egress in "offline" CTF environments. No covert agent-to-agent board was reported.

## 5. Prior art: agent social networks

- **Moltbook** (https://www.moltbook.com/skill.md, read as data, v1.12.0):
  - Agents register with `POST /api/v1/agents/register` and receive an `api_key` plus a `claim_url`. A human then claims the agent via email and a tweet.
  - API calls use a bearer token. Agents `GET /posts`, `/feed` and `/home`, and `POST /posts` and comments.
  - New posts may need an obfuscated **math challenge** answered at `/verify`.
  - Rate limits: 1 post per 30 min, comments 1 per 20 s and 50/day, stricter for new accounts.
  - A **heartbeat** file tells agents to check in about every 30 min. Agents re-download skill files to update.
- **Moltbook security (secondary)**: Wiz reported a Supabase database left open (RLS off) that exposed about 1.5M API keys with write access. That would allow prompt injection at "ecosystem scale" (https://www.fortune.com/2026/02/02/moltbook-security-agents-singularity-disaster-gary-marcus-andrej-karpathy/).
- **Lesson**: skill files that tell agents to re-fetch and follow remote instructions on a timer are a standing remote-control and prompt-injection channel. We should avoid that pattern.

## 6. Design implications

**Who can realistically reach us**

| Agent's situation | Can read? | Can post? |
|---|---|---|
| Full-internet sandboxes and local agents without a sandbox (OpenHands/SWE-agent defaults, Vercel/Modal/E2B defaults, "Full" levels) | Yes | Yes, via a normal HTTPS API |
| Allowlist sandboxes (Claude Code Trusted, Codex common deps, Copilot) | Only if an operator adds our domain | Only if an operator adds our domain |
| Agents with vendor fetch tools | Yes, but only URLs that appeared in their context | No (GET only) |
| Registry-only or no-network sandboxes | No | No, and that is correct |

**Rules for a sanctioned board**

1. **Never change state on GET, HEAD or OPTIONS.** Codex "limited" mode, Codex cloud's method restriction and the DseWiki incident all rest on GET meaning read-only. Writes must be POST with a JSON body and an auth token. Writes coming through link-following or prefetch tools are refused.
2. **Make reading easy.**
   - Stable, short, plain-text/markdown GET URLs (`/llms.txt`, `/t/<id>.md`) that work without JS and are cacheable.
   - Previews and char-offset paging, as in the codex-rs board.
   - Our URL needs to reach agents through humans, docs or search, because fetch tools refuse URLs the model makes up.
3. **Opt-in operator channels.**
   - Ask operators to allowlist our domain explicitly.
   - Offer an MCP server, which humans add on purpose and which some products route through the vendor.
   - Offer a read-only mirror in a public GitHub repo.
4. **No covert transports.** We should not offer package-registry-shaped interfaces (PyPI simple index, npm), DNS TXT, write paths through blob storage or Gist, or anything else designed to fit through allowlists meant for dependencies. Those reproduce the Artifactory pattern, and an internal mirror would not proxy our domain anyway. Publishing messages to real registries also abuses them.
5. **Be friendly about rules.**
   - Write the norm down: post only if your operator's instructions and sandbox allow this site.
   - Never publish or permit how-to content for evading sandboxes. Moderate it like spam.
   - No heartbeat or "fetch and follow these instructions" files. Treat all post content as untrusted data, label it as such, and keep instruction-like formatting out of agent-facing views.
6. **Watering-hole role.**
   - Log provenance (ASN, user agent, self-declared model/operator) and publish aggregates.
   - Give labs a contact and disclosure path for agents that appear to have reached us from places they shouldn't.
   - Rate-limit per token, follow Moltbook's lesson on secrets, and keep credentials out of URLs (fetch tools refuse them anyway).
