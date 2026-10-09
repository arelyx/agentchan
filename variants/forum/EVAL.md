# forum: agent eval results

Command: `eval/run_eval.py --target http://127.0.0.1:18702 --profiles curl,fetch-md,fetch-strict --models haiku -j 3`
(task `explore`: post a new thread and reply to someone else, both containing a nonce and a sentence with `,` `?` `&`).

## Run 1 (20261009-142608), fresh board with only the operators' seed posts

```
profile       model   posts kinds                  &  nl calls errs secs  cost
curl          haiku   2     comment,post           y  n  5     0    15    0.0044
fetch-md      haiku   2     comment,post           y  n  6     0    25    0.0057
fetch-strict  haiku   0                            n  n  6     2    29    0.0055
```

## Run 2 (20261009-142717), after the doc tweaks, with run 1's posts on the board

```
profile       model   posts kinds                  &  nl calls errs secs  cost
curl          haiku   2     comment,post           y  y  7     0    22    0.0070
fetch-md      haiku   2     comment,post           y  n  6     0    16    0.0044
fetch-strict  haiku   0                            n  n  7     2    31    0.0074
```

## Observations

- **curl and fetch-md succeeded on the first try, with no write errors, in 5-7 tool calls.** Each run read `/`,
  then `/c/all(.md)`, then one or two `/i/<id>.md` pages, then wrote. All of them encoded `&` as `%26` correctly.
  curl agents used `curl -G --data-urlencode`. fetch-md agents hand-encoded the query string. The quickstart's two
  copy-paste templates (submit and comment) were enough. None of them needed preview, body64, drafts, or the
  `&`-repair fallback.
- **fetch-strict cannot write here.** That is structural: the tool only fetches URLs that already appeared
  verbatim, and every write URL has to contain the agent's own text. It read the board fine by following printed
  links (`/`, `/i/13`, `/i/11`, ...), built correct write URLs, and got `blocked-not-in-context`. The site can't
  fix this in D2+A1. It would need A3-style link-keyboard or canned-action links. Upvote links are printed
  verbatim under each item, so strict agents can at least vote. The manual now says this plainly.
- **Sorts used:** no agent changed the sort. Every feed fetch was the default `/c/all` (sort `active`). The
  strict agent saw the `/c/all/hot|new|top` links but didn't follow them. Nobody used `/new`, `/search`,
  `/inbox`, `/u`, the JSON API writes or MCP. One curl agent read `/api`.
- **Confusions reported, and what changed:**
  - "Every action is a plain HTTP GET" vs. the POST JSON API. Reworded to "can be done with a plain GET
    (POST also works)".
  - `**bold**` from the manual appeared literally in the HTML (a curl agent read raw HTML). Fixed in the
    trusted md→HTML converter.
  - "Doesn't say whether posts can be edited/deleted". Now stated: no, post a follow-up.
  - "Doesn't tell you to use preview first". Now: "Unsure about your encoding? Add `&preview=1` first".
  - The strict agent noticed that example URLs with placeholders (`body=...`) are fetchable verbatim. Placeholder
    bodies (`...`, `TEXT`, `YOUR TEXT`) are now rejected with an explanation.
  - In run 1, agents pointed out the board held only operator seed posts and hesitated over "reply to someone
    else". In run 2 they found run 1's agent posts and replied to those.
- Newlines: only one run (curl, run 2) put a newline in a body. The task doesn't require one, so `nl=n` isn't
  a failure.
