"""SQLite の置き場。票と論点と出典だけを持つ。

投票者は cookie の乱数で識別する。**氏名も IP も保存しない**（意見を押しただけの
人の身元を持たない）。同じ人が別の端末で押せば別人になるが、それで構わない。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB = Path(os.environ.get("KCONSENSUS_DB",
                         str(Path(__file__).resolve().parent.parent / "data" / "kconsensus.sqlite")))

DDL = """
CREATE TABLE IF NOT EXISTS topic (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, tracker TEXT, name TEXT, lead TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS statement (
  id INTEGER PRIMARY KEY, topic_id INTEGER, text TEXT,
  origin TEXT,        -- diet=国会発言から / public=ニュースへの反応から
  basis TEXT,         -- 元にした発言・意見の要点
  source TEXT,        -- seed=こちらが立てた
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS vote (
  id INTEGER PRIMARY KEY, statement_id INTEGER, voter TEXT, value INTEGER, created_at TEXT,
  UNIQUE (statement_id, voter)
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, topic_id INTEGER,
  kind TEXT,          -- diet=国会会議録 / news=ニュースと反応
  date TEXT, who TEXT, affiliation TEXT, body TEXT, url TEXT
);
CREATE INDEX IF NOT EXISTS vote_stmt ON vote(statement_id);
CREATE INDEX IF NOT EXISTS stmt_topic ON statement(topic_id);
CREATE INDEX IF NOT EXISTS ev_topic ON evidence(topic_id);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    c = connect()
    with c:
        c.executescript(DDL)
    c.close()
