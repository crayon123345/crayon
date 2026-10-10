"""Postgres storage (Neon free tier). One cached connection per thread, auto-reconnect."""
import json
import logging
import threading
import time

import psycopg
from psycopg.rows import dict_row

import cr_config as C

log = logging.getLogger("crayon.db")
_local = threading.local()
_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id BIGINT PRIMARY KEY, name TEXT DEFAULT '', created_at TIMESTAMPTZ DEFAULT now(),
  last_seen TIMESTAMPTZ DEFAULT now(), tz TEXT DEFAULT '', summary TEXT DEFAULT '',
  summary_upto BIGINT DEFAULT 0, settings JSONB DEFAULT '{}'::jsonb);
ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_epoch BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS facts(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  category TEXT DEFAULT 'general', source TEXT DEFAULT 'chat', updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, key));
CREATE TABLE IF NOT EXISTS messages(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  ts TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS messages_user_id_idx ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS notes(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, text TEXT NOT NULL, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS reminders(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, text TEXT NOT NULL,
  due_at TIMESTAMPTZ NOT NULL, status TEXT DEFAULT 'pending', recurrence TEXT DEFAULT '',
  created_at TIMESTAMPTZ DEFAULT now(), sent_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders(status, due_at);
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS attempts INT DEFAULT 0;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'text';
CREATE TABLE IF NOT EXISTS tasks(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, title TEXT NOT NULL,
  goal TEXT DEFAULT '', status TEXT DEFAULT 'active', created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(), last_nudge TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS subtasks(
  id BIGSERIAL PRIMARY KEY, task_id BIGINT NOT NULL, pos INT NOT NULL, title TEXT NOT NULL,
  status TEXT DEFAULT 'todo', result TEXT DEFAULT '', updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS pending_actions(
  id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, action TEXT NOT NULL, args JSONB DEFAULT '{}'::jsonb,
  label TEXT DEFAULT '', status TEXT DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, ts TIMESTAMPTZ DEFAULT now(), event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value JSONB);
"""


def _connect():
    last = None
    for attempt in range(4):
        try:
            return psycopg.connect(C.DATABASE_URL, autocommit=True, connect_timeout=15, row_factory=dict_row)
        except Exception as e:  # Neon may be waking from scale-to-zero
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last


def _conn():
    c = getattr(_local, "c", None)
    if c is None or c.closed:
        _local.c = c = _connect()
    return c


def available():
    return bool(C.DATABASE_URL)


def q(sql, params=(), fetch="all"):
    """Run a statement. fetch: all | one | none."""
    if not C.DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    for attempt in (0, 1):
        try:
            cur = _conn().execute(sql, params)
            if fetch == "none":
                return None
            if fetch == "one":
                return cur.fetchone()
            return cur.fetchall()
        except (psycopg.OperationalError, psycopg.InterfaceError):
            # Never reconnect/retry one statement from inside an atomic transaction.
            # A reconnect uses autocommit and could turn a failed merge into partial writes.
            if getattr(getattr(_local,'c',None),'_num_transactions',0):raise
            _local.c = None
            if attempt:
                raise


def init():
    global _ready
    if _ready or not C.DATABASE_URL:
        return
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        q(stmt, fetch="none")
    _ready = True
    log.info("database ready")


def kv_get(key, default=None):
    r = q("SELECT value FROM kv WHERE key=%s", (key,), "one")
    return r["value"] if r else default


def kv_set(key, value):
    q("INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
      (key, json.dumps(value)), "none")


def audit(user_id, event, detail=""):
    try:
        from cr_safety import redact
        q("INSERT INTO audit(user_id,event,detail) VALUES(%s,%s,%s)", (user_id, event, redact(str(detail))[:500]), "none")
    except Exception:
        pass
