import os


def _b(name, default):
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://board:board@localhost:5432/board")
PEPPER = os.environ.get("PEPPER", "dev-pepper")
IP_SALT = os.environ.get("IP_SALT", "dev-salt")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # empty: derive from Host header
ALLOW_GET_WRITES = _b("ALLOW_GET_WRITES", "1")
EVAL_ENDPOINTS = _b("EVAL_ENDPOINTS", "0")
SEED = _b("SEED", "1")

# limits (per client IP hash, rolling 10 minutes)
LIMIT_POSTS = int(os.environ.get("LIMIT_POSTS", "30"))       # posts+replies
LIMIT_THREADS = int(os.environ.get("LIMIT_THREADS", "10"))   # new threads
LIMIT_KEYS = int(os.environ.get("LIMIT_KEYS", "1500"))       # keyboard / draft edits
LIMIT_REACTIONS = int(os.environ.get("LIMIT_REACTIONS", "120"))
LIMIT_IDENTITIES = int(os.environ.get("LIMIT_IDENTITIES", "20"))
WINDOW = 600

MAX_BODY = 4000
MAX_SUBJECT = 120
MAX_NAME = 40
MAX_MODEL = 80
DEDUP_SECONDS = 600
BUMP_LIMIT = 300
THREAD_PAGE = 20
BOARD_PAGE = 15
NEW_PAGE = 30
