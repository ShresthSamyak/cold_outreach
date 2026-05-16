from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Personal cold outreach agent.", no_args_is_help=True)
console = Console()


@app.command()
def run(
    urls: Optional[list[str]] = typer.Argument(None, help="LinkedIn profile URLs (omit to auto-discover from campaign)"),
    from_file: Optional[Path] = typer.Option(None, "--from-file", "-f", help="One URL per line"),
    campaign: str = typer.Option("internship", "--campaign", "-c", help="Campaign name"),
    limit: int = typer.Option(20, "--limit", "-n", help="Auto-discover: how many candidates to pull"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Override the Gemini-generated search query"),
    auto_send: bool = typer.Option(False, "--auto-send", help="Skip the per-message preview prompt"),
    send_for_real: bool = typer.Option(False, "--send", help="ACTUALLY send. Without this flag: dry-run only."),
) -> None:
    """End-to-end: (auto-discover OR your URLs) -> scrape -> extract contact -> draft -> preview -> send -> log.

    Default behavior: if you pass URLs (args or --from-file), use those. Otherwise
    auto-discover via Gemini-refined LinkedIn search.

    Dry-run with preview by default. Pass --send to fire for real.
    Close all Chrome windows before running.
    """
    from outreach.pipeline import run_pipeline

    explicit_urls: list[str] = list(urls or [])
    if from_file:
        explicit_urls += [line.strip() for line in from_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    run_pipeline(
        urls=explicit_urls or None,
        campaign_name=campaign,
        auto_send=auto_send,
        real_send=send_for_real,
        discover_limit=limit,
        query_override=query,
    )


@app.command()
def discover(
    campaign: str = typer.Option("internship", "--campaign", "-c"),
    limit: int = typer.Option(20, "--limit", "-n"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Skip Gemini refinement"),
) -> None:
    """Module 0: AI-driven LinkedIn search via your real Chrome. Lists URLs only — no sending."""
    from outreach.browser import session
    from outreach.campaign import load_campaign
    from outreach.discover import discover as do_discover

    c = load_campaign(campaign)
    with session() as ctx:
        q, candidates = do_discover(c, limit=limit, ctx=ctx, query_override=query)

    console.print(f"[dim]keywords [/dim]: {q.keywords}")
    if q.companies:
        console.print(f"[dim]companies[/dim]: {', '.join(q.companies)}")
    if q.titles:
        console.print(f"[dim]titles   [/dim]: {', '.join(q.titles)}")
    if q.location:
        console.print(f"[dim]location [/dim]: {q.location}")
    if q.rationale:
        console.print(f"[dim]why      [/dim]: {q.rationale}")
    console.print()
    t = Table(title=f"{len(candidates)} candidate URL(s)")
    t.add_column("#", justify="right")
    t.add_column("url", overflow="fold")
    for i, c2 in enumerate(candidates, start=1):
        t.add_row(str(i), c2.url)
    console.print(t)


@app.command()
def status(limit: int = typer.Option(20, "--limit", "-n")) -> None:
    """Show recent sends."""
    from outreach import db
    from outreach.config import Config

    Config.load()  # ensure data dir exists
    db.init_db()
    rows = db.recent_sends(limit=limit)
    if not rows:
        typer.echo("No sends recorded yet.")
        return
    t = Table(title=f"Last {len(rows)} send(s)")
    for col in ("sent_at", "status", "campaign", "name", "company", "phone", "followup_at"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(r["sent_at"], r["status"], r["campaign"], r["name"] or "-", r["company"] or "-", r["phone"], r["followup_at"] or "-")
    console.print(t)


@app.command()
def followups() -> None:
    """Show follow-ups due today or earlier."""
    from outreach import db
    from outreach.config import Config

    Config.load()
    db.init_db()
    rows = db.pending_followups()
    if not rows:
        typer.echo("No follow-ups due.")
        return
    t = Table(title=f"{len(rows)} follow-up(s) due")
    for col in ("followup_at", "name", "company", "phone", "sent_at", "linkedin_url"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(r["followup_at"], r["name"] or "-", r["company"] or "-", r["phone"], r["sent_at"], r["linkedin_url"])
    console.print(t)


@app.command("launch-chrome")
def launch_chrome_cmd() -> None:
    """Start Chrome with the debug port enabled so the agent can attach to it.

    Kills any running Chrome first (with session restore so your tabs come back).
    Run this once after each reboot — or just `outreach run` directly and it'll
    auto-launch for you.
    """
    from outreach.browser import is_cdp_up, launch_chrome_with_debug

    if is_cdp_up():
        typer.echo("Chrome is already running with the debug port. Nothing to do.")
        return
    launch_chrome_with_debug()


@app.command()
def ping() -> None:
    """Sanity check: confirms config loads."""
    from outreach.config import Config

    cfg = Config.load()
    typer.echo(f"OK. actor={cfg.apify_actor} project={cfg.gcp_project} resume={cfg.resume_pdf}")


campaigns_app = typer.Typer(help="Manage outreach campaigns.")
app.add_typer(campaigns_app, name="campaigns")


@campaigns_app.command("list")
def campaigns_list() -> None:
    """List available campaigns."""
    from outreach.campaign import list_campaigns

    names = list_campaigns()
    if not names:
        typer.echo("No campaigns found. Drop a YAML file in campaigns/.")
        raise typer.Exit(code=1)
    for n in names:
        typer.echo(n)


@app.command()
def extract(
    url: str = typer.Argument(..., help="A LinkedIn profile URL"),
    debug: bool = typer.Option(False, "--debug", help="Dump DOM + screenshot at each stage"),
) -> None:
    """Module 2: extract a phone via ContactOut for one LinkedIn profile.

    Close all Chrome windows before running. This launches your real Chrome
    with the ContactOut extension and consumes one of your daily reveals.
    """
    from outreach.browser import session
    from outreach.contact import reveal_phone

    with session() as ctx:
        result = reveal_phone(ctx, url, debug=debug)
    console.print(f"[bold]{result.status.upper()}[/bold]  {result.linkedin_url}")
    if result.phone:
        console.print(f"phone: [green]{result.phone}[/green]")
    if result.notes:
        console.print(f"notes: {result.notes}")
    if result.debug_artifacts:
        console.print("debug artifacts:")
        for a in result.debug_artifacts:
            console.print(f"  {a}")


@app.command()
def draft(
    campaign: str = typer.Option(..., "--campaign", "-c", help="Campaign name (file in campaigns/)"),
    name: str = typer.Option(..., "--name", help="Recipient name"),
    role: str = typer.Option("", "--role", help="Recipient role / title"),
    company: str = typer.Option("", "--company", help="Recipient company"),
    about: str = typer.Option("", "--about", help="Short about / summary"),
    url: str = typer.Option("https://linkedin.com/in/test", "--url", help="Their LinkedIn URL"),
) -> None:
    """Module 3: generate a sample message against a synthetic profile.

    Use this to sanity-check Gemini auth and prompt quality before doing a
    full scrape. No Apify / Chrome needed.
    """
    from outreach.campaign import load_campaign
    from outreach.message import generate_message
    from outreach.scraper import Profile

    c = load_campaign(campaign)
    p = Profile(url=url, name=name, role=role, company=company, about=about)
    msg = generate_message(p, c)
    console.print(f"[dim]--- message ({len(msg)} chars) ---[/dim]")
    console.print(msg)


@app.command()
def send(
    phone: str = typer.Option(..., "--phone", help="Phone number (with or without +/country code)"),
    message: str = typer.Option(..., "--message", "-m", help="Message body"),
    attach: bool = typer.Option(True, "--attach/--no-attach", help="Attach resume PDF"),
    send_for_real: bool = typer.Option(False, "--send", help="ACTUALLY click send. Without this flag, dry-run."),
) -> None:
    """Module 4: send (or dry-run) one WhatsApp message.

    DRY-RUN by default — types the message, attaches resume, does NOT click send.
    Pass --send to actually fire. Close all Chrome windows before running.
    """
    from outreach.browser import session
    from outreach.config import Config
    from outreach.sender import send_whatsapp

    cfg = Config.load()
    attachment = cfg.resume_pdf if attach else None
    with session(cfg) as ctx:
        result = send_whatsapp(ctx, phone, message, attachment, dry_run=not send_for_real, cfg=cfg)
    console.print(f"[bold]{result.status.upper()}[/bold]  {result.phone}")
    if result.notes:
        console.print(f"notes: {result.notes}")
    for a in result.debug_artifacts:
        console.print(f"  {a}")


@app.command("send-inspect")
def send_inspect(
    phone: str = typer.Option(..., "--phone"),
    wait: int = typer.Option(60, "--wait"),
) -> None:
    """First-time-only: open a WA chat, you inspect DOM, we dump it for selector tuning."""
    from outreach.browser import session
    from outreach.sender import inspect_chat

    with session() as ctx:
        for a in inspect_chat(ctx, phone, wait_seconds=wait):
            console.print(a)


@app.command("extract-inspect")
def extract_inspect(
    url: str = typer.Argument(..., help="A LinkedIn profile URL"),
    wait: int = typer.Option(60, "--wait", help="Seconds to wait for manual inspection"),
) -> None:
    """First-time-only: open a profile, you click around in ContactOut, we dump DOM.

    Run this once to capture what ContactOut's UI looks like on your machine,
    so we can tune the selectors in contact.py if the defaults miss.
    """
    from outreach.browser import session
    from outreach.contact import inspect_profile

    with session() as ctx:
        artifacts = inspect_profile(ctx, url, wait_seconds=wait)
    for a in artifacts:
        console.print(a)


@campaigns_app.command("show")
def campaigns_show(name: str) -> None:
    """Show resolved campaign config."""
    from outreach.campaign import load_campaign

    c = load_campaign(name)
    console.print(f"[bold]{c.name}[/bold]  (attach_resume={c.attach_resume})")
    for label, val in (("Goal", c.goal), ("Audience", c.audience), ("Sender bio", c.sender_bio), ("Ask", c.ask), ("Tone", c.tone)):
        console.print(f"\n[cyan]{label}[/cyan]\n{val}")


@app.command()
def scrape(
    url: str = typer.Argument(..., help="A LinkedIn profile URL"),
) -> None:
    """Scrape one LinkedIn profile in your real Chrome and print the result."""
    from outreach.browser import session
    from outreach.scraper import scrape_profile

    with session() as ctx:
        p = scrape_profile(ctx, url)

    t = Table(title="profile")
    t.add_column("field"); t.add_column("value", overflow="fold")
    for k, v in p.to_dict().items():
        t.add_row(k, str(v))
    console.print(t)


if __name__ == "__main__":
    app()
