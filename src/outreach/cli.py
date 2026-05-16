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
