"""Module 5 — SQLite tracking.

One small DB file at `data/outreach.db`. Records every profile we scrape,
every contact we extract, every message we send, and every follow-up the
pipeline schedules. Plain `sqlite3` — no ORM, no migrations framework. If
the schema changes, drop the DB and start over (this is a personal tool).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from outreach.config import DB_PATH, Config


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_url TEXT NOT NULL UNIQUE,
    name         TEXT,
    role         TEXT,
    company      TEXT,
    location     TEXT,
    about        TEXT,
    raw_json     TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    phone        TEXT,
    status       TEXT NOT NULL,            -- found / no_phone / quota_exhausted / extension_not_loaded / error
    notes        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_profile ON contacts(profile_id);

CREATE TABLE IF NOT EXISTS sends (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    contact_id   INTEGER NOT NULL REFERENCES contacts(id),
    campaign     TEXT NOT NULL,
    phone        TEXT NOT NULL,
    message      TEXT NOT NULL,
    status       TEXT NOT NULL,            -- sent / dry_run / not_on_whatsapp / session_expired / error
    notes        TEXT,
    sent_at      TEXT NOT NULL,
    followup_at  TEXT                      -- ISO date, NULL if no follow-up scheduled
);
CREATE INDEX IF NOT EXISTS idx_sends_followup ON sends(followup_at);
CREATE INDEX IF NOT EXISTS idx_sends_status   ON sends(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _db_path() -> Path:
    # Read DB_PATH from the module dynamically so monkeypatching works.
    from outreach import db as _self
    return _self.DB_PATH


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or _db_path()
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    target = path or _db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect(target) as conn:
        conn.executescript(SCHEMA)


def upsert_profile(
    *,
    linkedin_url: str,
    name: str = "",
    role: str = "",
    company: str = "",
    location: str = "",
    about: str = "",
    raw: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    raw_json = json.dumps(raw or {}, default=str)
    sql = """
        INSERT INTO profiles (linkedin_url, name, role, company, location, about, raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(linkedin_url) DO UPDATE SET
            name=excluded.name, role=excluded.role, company=excluded.company,
            location=excluded.location, about=excluded.about, raw_json=excluded.raw_json
        RETURNING id
    """
    params = (linkedin_url, name, role, company, location, about, raw_json, _now())

    def _exec(c: sqlite3.Connection) -> int:
        cur = c.execute(sql, params)
        row = cur.fetchone()
        return int(row["id"])

    if conn is not None:
        return _exec(conn)
    with connect() as c:
        return _exec(c)


def insert_contact(
    *,
    profile_id: int,
    phone: str | None,
    status: str,
    notes: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    sql = """
        INSERT INTO contacts (profile_id, phone, status, notes, created_at)
        VALUES (?, ?, ?, ?, ?) RETURNING id
    """
    params = (profile_id, phone, status, notes, _now())

    def _exec(c: sqlite3.Connection) -> int:
        return int(c.execute(sql, params).fetchone()["id"])

    if conn is not None:
        return _exec(conn)
    with connect() as c:
        return _exec(c)


def insert_send(
    *,
    profile_id: int,
    contact_id: int,
    campaign: str,
    phone: str,
    message: str,
    status: str,
    notes: str = "",
    followup_days: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    followup_at: str | None = None
    if status == "sent" and followup_days and followup_days > 0:
        followup_at = (datetime.now(timezone.utc).date() + timedelta(days=followup_days)).isoformat()
    sql = """
        INSERT INTO sends (profile_id, contact_id, campaign, phone, message, status, notes, sent_at, followup_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
    """
    params = (profile_id, contact_id, campaign, phone, message, status, notes, _now(), followup_at)

    def _exec(c: sqlite3.Connection) -> int:
        return int(c.execute(sql, params).fetchone()["id"])

    if conn is not None:
        return _exec(conn)
    with connect() as c:
        return _exec(c)


def sends_today_count(cfg: Config | None = None) -> int:
    """Number of real sends (status='sent') made today (UTC). Used for daily cap."""
    today = _today_utc()
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE status='sent' AND date(sent_at)=?",
            (today,),
        ).fetchone()
    return int(row["n"] if row else 0)


def pending_followups(today: str | None = None) -> list[sqlite3.Row]:
    today = today or _today_utc()
    with connect() as c:
        rows = c.execute(
            """
            SELECT s.id, s.profile_id, s.phone, s.campaign, s.message, s.sent_at, s.followup_at,
                   p.name, p.role, p.company, p.linkedin_url
              FROM sends s
              JOIN profiles p ON p.id = s.profile_id
             WHERE s.status='sent' AND s.followup_at IS NOT NULL AND s.followup_at <= ?
             ORDER BY s.followup_at ASC
            """,
            (today,),
        ).fetchall()
    return list(rows)


def recent_sends(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as c:
        rows = c.execute(
            """
            SELECT s.id, s.campaign, s.phone, s.status, s.sent_at, s.followup_at,
                   p.name, p.company, p.linkedin_url
              FROM sends s JOIN profiles p ON p.id=s.profile_id
             ORDER BY s.sent_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return list(rows)
