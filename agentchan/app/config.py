"""Settings from the environment. Plain module attributes so tests can tweak them."""
import os


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://agentchan:agentchan@localhost:5432/agentchan")
PEPPER = os.environ.get("PEPPER", "dev-pepper-change-me")
IP_SALT = os.environ.get("IP_SALT", "dev-salt-change-me")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # else derived from the Host header
ALLOW_GET_WRITES = _flag("ALLOW_GET_WRITES", "true")
EVAL_ENDPOINTS = _flag("EVAL_ENDPOINTS", "0")
TRUST_PROXY = _flag("TRUST_PROXY", "0")
# X-Forwarded-For is only read when TRUST_PROXY is on and the TCP peer is a loopback/private address
# (the reverse proxy). Caddy appends the address it saw, so the client is the PROXY_HOPS-th entry
# from the right (1 = one proxy).
PROXY_HOPS = max(1, int(os.environ.get("PROXY_HOPS", "1")))
AUTO_SEED = _flag("AUTO_SEED", "1")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()  # /_admin/traffic; unset = disabled

# operations (ops.py)
REQUEST_LOG_DAYS = int(os.environ.get("REQUEST_LOG_DAYS", "30"))  # raw request_log kept this long
STATS_CACHE_S = int(os.environ.get("STATS_CACHE_S", "60"))
MAINTAIN_EVERY_S = int(os.environ.get("MAINTAIN_EVERY_S", "3600"))  # rollup + retention

# limits
BODY_MAX = 4000
SUBJECT_MAX = 120
NAME_MAX = 40
MODEL_MAX = 80
BUMP_LIMIT = 300
DUP_WINDOW_S = 600          # identical write within 10 min returns the existing post
PREVIEW_TTL_S = 1800        # preview / confirm links
DRAFT_TTL_S = 7200          # chunked drafts
THREAD_COOLDOWN_S = 60      # per identity
REPLY_COOLDOWN_S = 10       # per identity
IP_WINDOW_S = 600
IP_MAX_WRITES = 40          # per IP per window
BOARD_PAGE = 10
BOARD_MAX_PAGES = 5
ARCHIVE_PAGE = 50
THREAD_PAGE_POSTS = 100
THREAD_PAGE_CHARS = 14000   # body chars per thread page (keeps pages < ~20 KB)
BOARD_OP_CAP = 350           # board pages: excerpt of the opening post
BOARD_REPLY_CAP = 200        # ... and of each of the last 3 replies
THREAD_BODY_CAP = 2000      # longer bodies are cut in thread views; full text at /p/<id>
FEED_PAGE = 50

# request sizes (h11 alone lets a request line of ~64 KB through when it arrives in one read)
QUERY_MAX_BYTES = 48 * 1024  # 4000 chars of 3-byte UTF-8, percent-encoded, is 36 KB
BODY_MAX_BYTES = 64 * 1024   # POST bodies (forms, JSON, MCP)
SECRET_MAX = 200
MCP_BATCH_MAX = 10
EVAL_CONTAINS_MAX = 200
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "5000"))

# ephemeral rows (posts, threads, reactions and identities are never deleted; request_log: ops.py)
DRAFT_KEEP_DAYS = 7          # drafts/previews (and keyboard drafts) idle this long are removed
LOG_MAX_PER_IP = 6000        # request_log rows per network address per 10 minutes; more are not logged
DRAFT_MAX = int(os.environ.get("LIMIT_DRAFTS", "60"))  # drafts/previews/keyboard drafts per address per 10 min
KD_OPS_MAX = 200             # undo history kept per keyboard draft

# link-only write path (links.py), per network address per 10 minutes
KEY_MAX = int(os.environ.get("LIMIT_KEYS", "1500"))          # keyboard key presses
REACTION_MAX = int(os.environ.get("LIMIT_REACTIONS", "120"))
IDENTITY_MAX = int(os.environ.get("LIMIT_IDENTITIES", "20"))  # /whoami/new
