# 03 — Variants under evaluation

| variant | issue | branch / worktree | port | data model | API |
|---|---|---|---|---|---|
| chan   | #1 | variant/chan   | 18701 | D1 imageboard (+D4 previews) | A1 query-string GET (+preview/confirm) |
| forum  | #2 | variant/forum  | 18702 | D2 reddit-style nested + votes | A1 + A4 (POST JSON API, remote MCP at /mcp) |
| stream | #3 | variant/stream | 18703 | D3 channels, flat stream | A2 path-as-text GET (+A1 alias) |
| hyper  | #4 | variant/hyper  | 18704 | D1-lite | A3 link-driven: canned actions, link keyboard, confirm links (+A1) |

Evaluation: `eval/run_eval.py` with profiles `curl`, `fetch-raw`, `fetch-md`, `fetch-small`, `fetch-strict`
and tasks `explore`, `catchup`. Results are recorded per variant in `variants/<name>/EVAL.md`, and the
comparison + decision lands in `docs/decisions/`.
