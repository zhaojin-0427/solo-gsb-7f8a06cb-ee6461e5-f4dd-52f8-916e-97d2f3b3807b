"""SQLite 持久层：每节点一个独立库文件（挂在各自持久卷上）。

写操作一律走 BEGIN IMMEDIATE 事务，把并发重试序列化，
保证“同一决议、每方一份正确文件”的收敛性。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/node.db")

# A/B 共用一份表结构（部分列只在一侧使用）：
#   A 侧：token_a/token_b、a_file、b_registered、决议（b_sha256 + resolution_created_at）、b_file
#   B 侧：token_b、b_file、决议（a_sha256 + resolution_created_at）、a_file、receipt_acked、drill_fired
SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id                     TEXT PRIMARY KEY,
    token_a                TEXT NOT NULL DEFAULT '',
    token_b                TEXT NOT NULL DEFAULT '',
    status                 TEXT NOT NULL,
    a_sha256               TEXT,
    a_file                 BLOB,
    a_filename             TEXT,
    a_submitted_at         TEXT,
    b_sha256               TEXT,
    b_file                 BLOB,
    b_filename             TEXT,
    b_submitted_at         TEXT,
    resolution_created_at  TEXT,
    b_registered           INTEGER NOT NULL DEFAULT 0,
    receipt_acked          INTEGER NOT NULL DEFAULT 0,
    drill_fired            INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx():
    """写事务：BEGIN IMMEDIATE 保证同一时刻只有一个写者。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_exchange(ex_id: str):
    conn = connect()
    try:
        return conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
    finally:
        conn.close()


def list_ids(where: str) -> list[str]:
    """where 只允许传入代码内部常量。"""
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id FROM exchanges WHERE {where} ORDER BY created_at, id"
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()
