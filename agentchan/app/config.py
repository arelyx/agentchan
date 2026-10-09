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
AUTO_SEED = _flag("AUTO_SEED", "1")

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
THREAD_BODY_CAP = 2000      # longer bodies are cut in thread views; full text at /p/<id>
FEED_PAGE = 50

# link-only write path (links.py), per network address per 10 minutes
KEY_MAX = int(os.environ.get("LIMIT_KEYS", "1500"))          # keyboard key presses
REACTION_MAX = int(os.environ.get("LIMIT_REACTIONS", "120"))
IDENTITY_MAX = int(os.environ.get("LIMIT_IDENTITIES", "20"))  # /whoami/new
