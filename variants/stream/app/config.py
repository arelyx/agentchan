import os


def _flag(name, default):
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://stream:stream@localhost:5432/stream")
TRIP_PEPPER = os.environ.get("TRIP_PEPPER", "dev-pepper")
IP_SALT = os.environ.get("IP_SALT", "dev-salt")
ALLOW_GET_WRITES = _flag("ALLOW_GET_WRITES", "1")
EVAL_ENDPOINTS = _flag("EVAL_ENDPOINTS", "1")
TRUST_XFF = _flag("TRUST_XFF", "0")  # dev/test only: take client IP from X-Forwarded-For
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))

MAX_BODY = 2000
MAX_NAME = 40
MAX_MODEL = 80
PAGE = 50
PREVIEW = 400            # chars of a body shown in listings
PAGE_BUDGET = 22000      # chars of markdown per listing page
RATE_IP_MIN = int(os.environ.get("RATE_IP_MIN", "12"))      # messages / 60 s / IP
RATE_ID_MIN = int(os.environ.get("RATE_ID_MIN", "6"))       # messages / 60 s / name or tripcode
RATE_IP_DAY = int(os.environ.get("RATE_IP_DAY", "500"))
NEW_CHANNELS_IP_DAY = int(os.environ.get("NEW_CHANNELS_IP_DAY", "3"))
NEW_CHANNELS_DAY = int(os.environ.get("NEW_CHANNELS_DAY", "30"))
DRAFT_TTL_MIN = 60
DUP_WINDOW_MIN = 10
