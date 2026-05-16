import typer

app = typer.Typer(help="Personal cold outreach agent.")


@app.command()
def ping() -> None:
    """Sanity check: confirms the package is importable and CLI works."""
    from outreach.config import Config

    cfg = Config.load()
    typer.echo(f"OK. actor={cfg.apify_actor} project={cfg.gcp_project} resume={cfg.resume_pdf}")


if __name__ == "__main__":
    app()
