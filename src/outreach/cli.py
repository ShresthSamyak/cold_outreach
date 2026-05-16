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
    urls: Optional[list[str]] = typer.Argument(None, help="LinkedIn profile URLs"),
    from_file: Optional[Path] = typer.Option(None, "--from-file", "-f", help="File with one URL per line"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write normalized profiles as JSON"),
) -> None:
    """Run Module 1: scrape LinkedIn profiles via Apify."""
    from outreach.scraper import scrape_profiles

    all_urls: list[str] = list(urls or [])
    if from_file:
        all_urls += [line.strip() for line in from_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    if not all_urls:
        typer.echo("Provide at least one URL (as args or via --from-file).", err=True)
        raise typer.Exit(code=2)

    profiles = scrape_profiles(all_urls)

    table = Table(title=f"Scraped {len(profiles)} profile(s)")
    for col in ("name", "role", "company", "location", "url"):
        table.add_column(col, overflow="fold")
    for p in profiles:
        table.add_row(p.name or "-", p.role or "-", p.company or "-", p.location or "-", p.url)
    console.print(table)

    if out:
        out.write_text(json.dumps([p.to_dict() for p in profiles], indent=2), encoding="utf-8")
        typer.echo(f"Wrote {out}")


if __name__ == "__main__":
    app()
