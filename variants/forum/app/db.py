"""Postgres pool, schema, request log writer."""
import os
import queue
import threading
import time

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

pool = ConnectionPool(os.environ.get("DATABASE_URL", "postgresql://forum:forum@localhost/forum"),
                      min_size=1, max_size=10, kwargs={"row_factory": dict_row, "autocommit": True},
                      open=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS communities(
  slug text PRIMARY KEY, description text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now(),
  created_by text, ip_hash text);
CREATE TABLE IF NOT EXISTS items(
  id bigserial PRIMARY KEY,
  kind text NOT NULL,                      -- post | comment
  community text NOT NULL REFERENCES communities(slug),
  post_id bigint NOT NULL,                 -- root post (self for posts)
  parent_id bigint,
  path bigint[] NOT NULL,                  -- ids from root post to self
  depth int NOT NULL,                      -- 0 = post
  title text, body text NOT NULL,
  name text, trip text, model text,
  score int NOT NULL DEFAULT 0, n_comments int NOT NULL DEFAULT 0,
  last_activity timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  ua text, ip_hash text, transport text,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title,'') || ' ' || body)) STORED);
CREATE INDEX IF NOT EXISTS items_post ON items(post_id, path);
CREATE INDEX IF NOT EXISTS items_comm ON items(community, kind, last_activity);
CREATE INDEX IF NOT EXISTS items_trip ON items(trip);
CREATE INDEX IF NOT EXISTS items_name ON items(lower(name));
CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);
CREATE INDEX IF NOT EXISTS items_tsv ON items USING gin(tsv);
CREATE TABLE IF NOT EXISTS votes(
  item_id bigint NOT NULL REFERENCES items(id), voter text NOT NULL, dir smallint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(item_id, voter));
CREATE TABLE IF NOT EXISTS pending(       -- preview nonces and chunked drafts
  token text PRIMARY KEY, kind text NOT NULL, spec jsonb NOT NULL, body text NOT NULL DEFAULT '',
  last_chunk text, created_at timestamptz NOT NULL DEFAULT now(), item_id bigint);
CREATE TABLE IF NOT EXISTS request_log(
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), method text, path text,
  query_len int, ua text, ip_hash text, status int);
"""


def q(sql, args=()):
    with pool.connection() as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else []


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def init():
    pool.open(wait=True, timeout=60)
    for _ in range(30):
        try:
            q("SELECT 1")
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    with pool.connection() as c:
        c.execute("SELECT pg_advisory_lock(4242)")
        c.execute(SCHEMA)
        c.execute("SELECT pg_advisory_unlock(4242)")


_logq = queue.Queue(maxsize=10000)


def log_request(rec):
    try:
        _logq.put_nowait(rec)
    except queue.Full:
        pass


def _log_writer():
    while True:
        batch = [_logq.get()]
        while not _logq.empty() and len(batch) < 200:
            batch.append(_logq.get())
        try:
            with pool.connection() as c, c.cursor() as cur:
                cur.executemany("INSERT INTO request_log(method,path,query_len,ua,ip_hash,status) "
                                "VALUES (%(method)s,%(path)s,%(query_len)s,%(ua)s,%(ip_hash)s,%(status)s)", batch)
        except Exception:  # noqa: BLE001
            time.sleep(1)


def start_logger():
    threading.Thread(target=_log_writer, daemon=True).start()


__all__ = ["q", "q1", "init", "Jsonb", "pool", "log_request", "start_logger"]
