"""Tests for the tracking DB. Uses an isolated temp DB for each test."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from outreach import db as dbmod


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db(db_path)
    return db_path


def test_init_creates_tables(tmp_db: Path) -> None:
    with dbmod.connect() as c:
        tables = {row["name"] for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"profiles", "contacts", "sends"} <= tables


def test_upsert_profile_returns_same_id_for_same_url(tmp_db: Path) -> None:
    a = dbmod.upsert_profile(linkedin_url="https://linkedin.com/in/x", name="Old")
    b = dbmod.upsert_profile(linkedin_url="https://linkedin.com/in/x", name="New")
    assert a == b
    with dbmod.connect() as c:
        row = c.execute("SELECT name FROM profiles WHERE id=?", (a,)).fetchone()
    assert row["name"] == "New"


def test_insert_send_with_followup(tmp_db: Path) -> None:
    pid = dbmod.upsert_profile(linkedin_url="https://linkedin.com/in/x", name="X")
    cid = dbmod.insert_contact(profile_id=pid, phone="+919999999999", status="found")
    sid = dbmod.insert_send(
        profile_id=pid, contact_id=cid, campaign="internship",
        phone="+919999999999", message="hi this is a long enough message",
        status="sent", followup_days=5,
    )
    with dbmod.connect() as c:
        row = c.execute("SELECT followup_at FROM sends WHERE id=?", (sid,)).fetchone()
    expected = (datetime.now(timezone.utc).date() + timedelta(days=5)).isoformat()
    assert row["followup_at"] == expected


def test_dry_run_send_does_not_schedule_followup(tmp_db: Path) -> None:
    pid = dbmod.upsert_profile(linkedin_url="https://linkedin.com/in/x")
    cid = dbmod.insert_contact(profile_id=pid, phone="+91", status="found")
    sid = dbmod.insert_send(
        profile_id=pid, contact_id=cid, campaign="internship",
        phone="+91", message="x" * 30, status="dry_run", followup_days=5,
    )
    with dbmod.connect() as c:
        row = c.execute("SELECT followup_at FROM sends WHERE id=?", (sid,)).fetchone()
    assert row["followup_at"] is None


def test_sends_today_count(tmp_db: Path) -> None:
    pid = dbmod.upsert_profile(linkedin_url="https://linkedin.com/in/x")
    cid = dbmod.insert_contact(profile_id=pid, phone="+91", status="found")
    assert dbmod.sends_today_count() == 0
    for _ in range(3):
        dbmod.insert_send(
            profile_id=pid, contact_id=cid, campaign="c",
            phone="+91", message="x" * 30, status="sent",
        )
    # dry_run does not count
    dbmod.insert_send(
        profile_id=pid, contact_id=cid, campaign="c",
        phone="+91", message="x" * 30, status="dry_run",
    )
    assert dbmod.sends_today_count() == 3


def test_pending_followups(tmp_db: Path) -> None:
    pid = dbmod.upsert_profile(linkedin_url="https://linkedin.com/in/x", name="X")
    cid = dbmod.insert_contact(profile_id=pid, phone="+91", status="found")
    # one followup due yesterday, one due in the future
    sid_due = dbmod.insert_send(
        profile_id=pid, contact_id=cid, campaign="c",
        phone="+91", message="x" * 30, status="sent", followup_days=0,
    )
    with dbmod.connect() as c:
        c.execute(
            "UPDATE sends SET followup_at=? WHERE id=?",
            ((date.today() - timedelta(days=1)).isoformat(), sid_due),
        )
    dbmod.insert_send(
        profile_id=pid, contact_id=cid, campaign="c",
        phone="+91", message="x" * 30, status="sent", followup_days=30,
    )
    due = dbmod.pending_followups()
    assert len(due) == 1
    assert due[0]["id"] == sid_due
