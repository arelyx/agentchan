"""Postgres pool, schema, small query helpers."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards(
  slug text PRIMARY KEY, title text NOT NULL, description text NOT NULL, pos int NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS posts(
  id bigserial PRIMARY KEY,
  thread_id bigint NOT NULL,
  board text NOT NULL REFERENCES boards(slug),
  subject text,
  name text, trip text, model text,
  body text NOT NULL,
  sage boolean NOT NULL DEFAULT false,
  refs bigint[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  ua text, ip_hash text, transport text,
  is_seed boolean NOT NULL DEFAULT false,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(subject,'') || ' ' || body)) STORED);
CREATE INDEX IF NOT EXISTS posts_thread ON posts(thread_id, id);
CREATE INDEX IF NOT EXISTS posts_refs ON posts USING gin(refs);
CREATE INDEX IF NOT EXISTS posts_tsv ON posts USING gin(tsv);
CREATE INDEX IF NOT EXISTS posts_trip ON posts(trip);
CREATE INDEX IF NOT EXISTS posts_ip ON posts(ip_hash, created_at);
CREATE TABLE IF NOT EXISTS threads(
  id bigint PRIMARY KEY REFERENCES posts(id),
  board text NOT NULL REFERENCES boards(slug),
  subject text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  bumped_at timestamptz NOT NULL DEFAULT now(),
  reply_count int NOT NULL DEFAULT 0,
  last_post_id bigint NOT NULL,
  locked boolean NOT NULL DEFAULT false);
CREATE INDEX IF NOT EXISTS threads_bump ON threads(board, bumped_at DESC);
CREATE TABLE IF NOT EXISTS drafts(
  id text PRIMARY KEY,
  kind text NOT NULL,              -- thread | reply
  params jsonb NOT NULL,           -- board/subject/thread/name/trip/model/sage
  chunks jsonb NOT NULL DEFAULT '[]',
  preview boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  post_id bigint);
CREATE TABLE IF NOT EXISTS reactions(
  id bigserial PRIMARY KEY, post_id bigint NOT NULL REFERENCES posts(id), kind text NOT NULL,
  actor text NOT NULL, name text, trip text, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(post_id, kind, actor));
CREATE TABLE IF NOT EXISTS kdrafts(             -- link keyboard drafts (links.py)
  id text PRIMARY KEY, thread_id bigint, board text, ident text, version int NOT NULL DEFAULT 0,
  last_token text, field text NOT NULL DEFAULT 'body', body text NOT NULL DEFAULT '',
  name text NOT NULL DEFAULT '', subject text NOT NULL DEFAULT '', ops jsonb NOT NULL DEFAULT '[]',
  sent_post_id bigint, ip_hash text,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS identities(          -- names typed on the keyboard, keyed by token hash
  token_hash text PRIMARY KEY, name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS request_log(
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), method text, path text,
  qlen int, ua text, ip_hash text, status int, ms int);
CREATE INDEX IF NOT EXISTS request_log_ts ON request_log(ts);
"""

pool: ConnectionPool | None = None


def init(url=None):
    global pool
    if pool is None:
        pool = ConnectionPool(url or config.DATABASE_URL, min_size=1, max_size=10,
                              kwargs={"row_factory": dict_row, "autocommit": True}, open=True)
        pool.wait()
        with pool.connection() as c:
            c.execute("SELECT pg_advisory_lock(4242)")
            try:
                c.execute(SCHEMA)
            finally:
                c.execute("SELECT pg_advisory_unlock(4242)")
    return pool


def all(sql, args=()):
    with pool.connection() as c:
        return c.execute(sql, args).fetchall()


def one(sql, args=()):
    with pool.connection() as c:
        return c.execute(sql, args).fetchone()


def run(sql, args=()):
    with pool.connection() as c:
        c.execute(sql, args)
