"""End-to-end orchestrator. One Chrome session does the whole job:

  Module 0  Discover    -> LinkedIn search in Chrome -> profile URLs
  Module 1  Scrape      -> open each URL, read profile DOM
  Module 2  Contact     -> ContactOut on the same page, get phone
  Module 3  Message     -> Gemini (reading the resume PDF) drafts message
  Module 4  Send        -> WhatsApp Web in the same Chrome session
  Module 5  Log         -> SQLite

No Apify actors required. The user's logged-in LinkedIn + WhatsApp Web +
ContactOut extension do all the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import typer

from outreach import db
from outreach.browser import session
from outreach.campaign import Campaign, load_campaign
from outreach.config import Config
from outreach.contact import reveal_phone
from outreach.discover import discover as discover_candidates
from outreach.message import generate_message
from outreach.scraper import scrape_profile
from outreach.sender import human_send_delay, send_whatsapp


@dataclass
class RunStats:
    discovered: int = 0
    scraped: int = 0
    contacts_found: int = 0
    contacts_missing: int = 0
    messages_drafted: int = 0
    sent: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"discovered={self.discovered}  scraped={self.scraped}  "
            f"contacts={self.contacts_found}/{self.scraped} (missing={self.contacts_missing})  "
            f"drafted={self.messages_drafted}  sent={self.sent}  dry_run={self.dry_run}  "
            f"skipped={self.skipped}  failed={self.failed}"
        )


def _confirm(prompt: str) -> str:
    while True:
        ans = typer.prompt(prompt, default="s").strip().lower()
        if ans in {"s", "send", "y", "yes"}:
            return "send"
        if ans in {"k", "skip", "n", "no"}:
            return "skip"
        if ans in {"q", "quit", "abort"}:
            return "quit"
        typer.echo("  Answer with 's' (send), 'k' (skip), or 'q' (quit)")


def run_pipeline(
    urls: Iterable[str] | None,
    campaign_name: str,
    *,
    auto_send: bool = False,
    real_send: bool = False,
    discover_limit: int = 20,
    query_override: str | None = None,
    cfg: Config | None = None,
) -> RunStats:
    cfg = cfg or Config.load()
    campaign: Campaign = load_campaign(campaign_name)
    db.init_db()
    stats = RunStats()

    url_list: list[str] = [u.strip() for u in (urls or []) if u and u.strip()]

    # ---- Module 0: discovery via Gemini google_search (no browser needed) ----
    if not url_list:
        typer.echo(f"\n[0/4] Gemini searching the web for candidate LinkedIn profiles...")
        query, candidates = discover_candidates(
            campaign, limit=discover_limit, cfg=cfg, query_override=query_override
        )
        typer.echo(f"  {query.keywords}")
        typer.echo(f"  found {len(candidates)} candidate profile URL(s)")
        url_list = [c.url for c in candidates]
        stats.discovered = len(url_list)
        if not url_list:
            typer.echo("Discovery returned 0 candidates. Refine campaign audience.")
            return stats

    typer.echo(f"\n[opening headless Chrome sandbox for ContactOut...]")
    with session(cfg) as ctx:

        # Daily cap check (real sends only).
        if real_send:
            already = db.sends_today_count()
            remaining = cfg.daily_send_limit - already
            if remaining <= 0:
                typer.echo(f"Daily send cap reached ({already}/{cfg.daily_send_limit}).")
                return stats
            typer.echo(f"  daily send cap: {already}/{cfg.daily_send_limit} used, {remaining} left today.")

        # ---- Per-profile loop: Modules 1-5 ----
        for i, url in enumerate(url_list, start=1):
            typer.echo(f"\n--- Profile {i}/{len(url_list)}: {url} ---")

            # Module 1 — scrape profile from LinkedIn DOM
            typer.echo("  [scrape] reading LinkedIn profile DOM...")
            try:
                profile = scrape_profile(ctx, url)
                stats.scraped += 1
            except Exception as e:
                stats.failed += 1
                typer.echo(f"  [scrape] FAILED: {type(e).__name__}: {e}")
                continue
            typer.echo(f"  [scrape] {profile.name or '?'}  |  {profile.role or '?'}  @  {profile.company or '?'}")

            pid = db.upsert_profile(
                linkedin_url=profile.url, name=profile.name, role=profile.role,
                company=profile.company, location=profile.location, about=profile.about,
                raw=profile.raw,
            )

            # Module 2 — ContactOut
            typer.echo("  [contact] revealing phone via ContactOut...")
            contact = reveal_phone(ctx, profile.url)
            cid = db.insert_contact(
                profile_id=pid, phone=contact.phone,
                status=contact.status, notes=contact.notes,
            )
            if contact.status != "found" or not contact.phone:
                stats.contacts_missing += 1
                typer.echo(f"  [contact] no phone: {contact.status} — {contact.notes}")
                stats.skipped += 1
                continue
            stats.contacts_found += 1
            typer.echo(f"  [contact] {contact.phone}")

            # Module 3 — Gemini message
            typer.echo("  [message] drafting via Gemini (reading your resume)...")
            try:
                message = generate_message(profile, campaign, cfg=cfg)
                stats.messages_drafted += 1
            except Exception as e:
                stats.failed += 1
                typer.echo(f"  [message] FAILED: {type(e).__name__}: {e}")
                continue

            typer.echo("\n  [preview]")
            for line in message.splitlines():
                typer.echo(f"    {line}")
            typer.echo("")

            # Module 4 — confirm + send
            if not auto_send:
                action = _confirm("  Send? [s]end/[k]skip/[q]uit")
                if action == "quit":
                    typer.echo("  Aborting on user request.")
                    break
                if action == "skip":
                    stats.skipped += 1
                    db.insert_send(
                        profile_id=pid, contact_id=cid, campaign=campaign.name,
                        phone=contact.phone, message=message, status="dry_run",
                        notes="user skipped",
                    )
                    continue

            attachment = cfg.resume_pdf if campaign.attach_resume else None
            typer.echo(f"  [send] {'REAL' if real_send else 'DRY-RUN'}...")
            result = send_whatsapp(
                contact.phone, message, attachment,
                dry_run=not real_send, cfg=cfg,
            )
            db.insert_send(
                profile_id=pid, contact_id=cid, campaign=campaign.name,
                phone=result.phone, message=message, status=result.status,
                notes=result.notes,
                followup_days=cfg.followup_days if real_send else None,
            )
            if result.status == "sent":
                stats.sent += 1
                typer.echo(f"  [send] SENT — follow-up in {cfg.followup_days} days")
                if i < len(url_list):
                    human_send_delay(cfg)
            elif result.status == "dry_run":
                stats.dry_run += 1
                typer.echo("  [send] DRY-RUN complete (Send NOT clicked).")
            else:
                stats.failed += 1
                typer.echo(f"  [send] {result.status.upper()}: {result.notes}")

    typer.echo(f"\n=== DONE ===\n{stats.summary()}")
    return stats
