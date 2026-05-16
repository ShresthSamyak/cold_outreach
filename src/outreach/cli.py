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
