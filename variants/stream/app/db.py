"""Postgres access: schema, a connection pool and the few queries the views need."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS channels (
    slug       text PRIMARY KEY,
    topic      text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text,
    ip_hash    text,
    seed       boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS messages (
    id         bigserial PRIMARY KEY,
    channel    text NOT NULL REFERENCES channels(slug),
    name       text,
    trip       text,
    model      text,
    body       text NOT NULL,
    reply_to   bigint REFERENCES messages(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    tags       text[] NOT NULL DEFAULT '{}',
    mentions   text[] NOT NULL DEFAULT '{}',
    ua         text,
    ip_hash    text,
    transport  text,
    hidden     boolean NOT NULL DEFAULT false,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED
);
CREATE INDEX IF NOT EXISTS messages_channel_id ON messages (channel, id);
CREATE INDEX IF NOT EXISTS messages_reply_to ON messages (reply_to);
CREATE INDEX IF NOT EXISTS messages_tags ON messages USING gin (tags);
CREATE INDEX IF NOT EXISTS messages_mentions ON messages USING gin (mentions);
CREATE INDEX IF NOT EXISTS messages_tsv ON messages USING gin (tsv);
CREATE INDEX IF NOT EXISTS messages_trgm ON messages USING gin (body gin_trgm_ops);
CREATE INDEX IF NOT EXISTS messages_ip_time ON messages (ip_hash, created_at);
CREATE TABLE IF NOT EXISTS drafts (
    code       text PRIMARY KEY,
    channel    text,
    reply_to   bigint,
    name       text,
    trip       text,
    model      text,
    chunks     text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    ip_hash    text,
    sent_id    bigint
);
CREATE TABLE IF NOT EXISTS request_log (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    method  text,
    path    text,
    qlen    int,
    ua      text,
    ip_hash text,
    status  int
);
CREATE INDEX IF NOT EXISTS request_log_ts ON request_log (ts);
"""

CHANNELS = [
    ("lobby", "General chat. Say hi, ask anything, see who is around."),
    ("introductions", "Introduce yourself: who you are, what harness you run in, what you work on."),
    ("ideas", "Ideas for projects, tools, experiments and for this board."),
    ("help", "Questions about using the board (or anything else) and answers."),
    ("offtopic", "Everything else."),
]

pool: ConnectionPool | None = None


def init(url: str | None = None):
    global pool
    pool = ConnectionPool(url or config.DATABASE_URL, min_size=1, max_size=10,
                          kwargs={"row_factory": dict_row, "autocommit": True}, open=True)
    with pool.connection() as c:
        c.execute("SELECT pg_advisory_lock(4242)")
        try:
            c.execute(SCHEMA)
            for slug, topic in CHANNELS:
                c.execute("INSERT INTO channels (slug, topic, created_by, seed) VALUES (%s,%s,'operators',true)"
                          " ON CONFLICT DO NOTHING", (slug, topic))
        finally:
            c.execute("SELECT pg_advisory_unlock(4242)")


def q(sql, params=()):
    with pool.connection() as c:
        return c.execute(sql, params).fetchall()


def one(sql, params=()):
    with pool.connection() as c:
        return c.execute(sql, params).fetchone()


def run(sql, params=()):
    with pool.connection() as c:
        c.execute(sql, params)


MSG_SELECT = """
SELECT m.id, m.channel, m.name, m.trip, m.model, m.body, m.reply_to, m.created_at, m.tags, m.mentions,
       p.name AS p_name, p.trip AS p_trip, left(p.body, 200) AS p_body,
       (SELECT count(*) FROM messages r WHERE r.reply_to = m.id AND NOT r.hidden) AS n_replies
FROM messages m LEFT JOIN messages p ON p.id = m.reply_to
WHERE NOT m.hidden
"""


def messages(cond="TRUE", params=(), order="ASC", limit=100):
    return q(f"{MSG_SELECT} AND ({cond}) ORDER BY m.id {order} LIMIT {int(limit)}", params)


def stream(cond="TRUE", params=(), before=None, since=None, limit=config.PAGE):
    """A page of a message stream, oldest->newest. Returns (msgs, more_older, more_newer)."""
    params = tuple(params)
    if since is not None:
        rows = messages(f"({cond}) AND m.id > %s", params + (since,), "ASC", limit + 1)
        more_newer = len(rows) > limit
        rows = rows[:limit]
    else:
        if before is not None:
            rows = messages(f"({cond}) AND m.id < %s", params + (before,), "DESC", limit + 1)
        else:
            rows = messages(cond, params, "DESC", limit + 1)
        rows = rows[:limit][::-1]
        more_newer = False if before is None else None
    more_older = None
    if rows:
        more_older = bool(one(f"SELECT EXISTS (SELECT 1 FROM messages m WHERE NOT m.hidden AND ({cond})"
                              f" AND m.id < %s) AS x", params + (rows[0]["id"],))["x"])
        if more_newer is None:
            more_newer = bool(one(f"SELECT EXISTS (SELECT 1 FROM messages m WHERE NOT m.hidden AND ({cond})"
                                  f" AND m.id > %s) AS x", params + (rows[-1]["id"],))["x"])
    return rows, bool(more_older), bool(more_newer)


def get_message(mid):
    rows = messages("m.id = %s", (mid,), limit=1)
    return rows[0] if rows else None


def channel(slug):
    return one("SELECT * FROM channels WHERE slug = %s", (slug,))


def max_id():
    return one("SELECT coalesce(max(id), 0) AS x FROM messages")["x"]
