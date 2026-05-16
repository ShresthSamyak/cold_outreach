"""Dedicated Chrome sandbox for the agent.

OpenClaude / Computer-Use style: the agent has its OWN Chrome profile,
completely separate from the user's main browser. We never kill chrome.exe,
never attach to the user's session, never cause profile-lock errors.

Setup is a one-time wizard (`outreach setup`):
  1. We launch a Chrome window pointing at the sandbox profile.
  2. User installs ContactOut, logs into LinkedIn, scans WhatsApp Web QR.
  3. User confirms. Sessions persist in the sandbox profile forever.

Run is invisible: `outreach run` auto-launches the sandbox Chrome (or
attaches if already running), opens its tabs, does its work.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

from outreach.config import Config

CDP_PORT = 9223  # avoid clashing with the user's own Chrome if they use 9222
CDP_VERSION_URL = f"http://localhost:{CDP_PORT}/json/version"


def is_cdp_up(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(CDP_VERSION_URL, timeout=timeout):
            return True
    except Exception:
        return False


def _sandbox_dir(cfg: Config) -> Path:
    return Path(cfg.chrome_user_data_dir)


def _remove_singleton_locks(sandbox: Path) -> None:
    """Stale lock files from a previous crashed run block re-launch on Windows."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = sandbox / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def _chrome_args(cfg: Config, *, headless: bool = False) -> list[str]:
    sandbox = _sandbox_dir(cfg)
    args = [
        cfg.chrome_executable,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={sandbox}",
        f"--profile-directory={cfg.chrome_profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    if headless:
        args.append("--headless=new")
    return args


def launch_sandbox(cfg: Config | None = None, *, headless: bool = False, wait_seconds: int = 60) -> None:
    """Launch the dedicated sandbox Chrome (or no-op if already up)."""
    cfg = cfg or Config.load()

    if is_cdp_up():
        return

    sandbox = _sandbox_dir(cfg)
    sandbox.mkdir(parents=True, exist_ok=True)
    _remove_singleton_locks(sandbox)

    if not cfg.chrome_executable or not os.path.exists(cfg.chrome_executable):
        raise RuntimeError(f"CHROME_EXECUTABLE not found: {cfg.chrome_executable!r}. Set it in .env.")

    args = _chrome_args(cfg, headless=headless)
    print(f"[browser] launching sandbox Chrome at {sandbox} (port {CDP_PORT})...")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(args, creationflags=flags, close_fds=True)

    deadline = time.time() + wait_seconds
    last_print = 0.0
    while time.time() < deadline:
        if is_cdp_up():
            print(f"[browser] sandbox Chrome ready on :{CDP_PORT}")
            return
        if time.time() - last_print >= 5:
            remaining = int(deadline - time.time())
            print(f"[browser] waiting for sandbox Chrome ({remaining}s left)...")
            last_print = time.time()
        time.sleep(0.5)
    raise RuntimeError(
        f"Sandbox Chrome didn't open the debug port within {wait_seconds}s.\n"
        f"Try running `uv run outreach setup` to set it up interactively."
    )


@contextmanager
def session(cfg: Config | None = None, *, headless: bool = False) -> Iterator[BrowserContext]:
    """Attach to the sandbox Chrome via CDP. Launches it if not running."""
    cfg = cfg or Config.load()
    launch_sandbox(cfg, headless=headless)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
        if not browser.contexts:
            ctx = browser.new_context()
        else:
            ctx = browser.contexts[0]
        try:
            yield ctx
        finally:
            try:
                browser.close()  # disconnect CDP only; Chrome stays running
            except Exception:
                pass


def setup_wizard(cfg: Config | None = None) -> None:
    """Interactive one-time setup: launch sandbox Chrome and walk through login steps."""
    cfg = cfg or Config.load()
    print("=" * 70)
    print("OUTREACH SETUP — one-time configuration of your dedicated automation Chrome")
    print("=" * 70)
    print(f"\nSandbox profile path: {_sandbox_dir(cfg)}\n")
    print("Launching the sandbox Chrome window now. Your main Chrome stays untouched.")
    print("(The agent will reuse this sandbox forever after — never touches your main browser.)\n")

    launch_sandbox(cfg, headless=False)
    time.sleep(2)

    # Open the three setup pages in tabs so the user just walks through them.
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        for url, label in (
            ("https://chromewebstore.google.com/detail/contactout-find-any-email/jjdemeiffadmmjhkbbpglgnlgeafomjo",
             "ContactOut extension"),
            ("https://www.linkedin.com/login", "LinkedIn login"),
            ("https://web.whatsapp.com/", "WhatsApp Web (scan QR)"),
        ):
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                print(f"  [tab opened] {label}: {url}")
            except Exception as e:
                print(f"  [warn] couldn't open {label}: {e}")
        browser.close()

    print("\nSteps to complete in the sandbox Chrome window now:")
    print("  1. Install the ContactOut extension (Add to Chrome).")
    print("  2. Log into LinkedIn (your normal credentials).")
    print("  3. Log into ContactOut (the extension's popup).")
    print("  4. Scan the WhatsApp Web QR code with your phone.")
    print("\nWhen all three are done, press Enter here to finish setup.")
    try:
        input(">>> ")
    except EOFError:
        pass
    print("\nSetup complete. From now on, `outreach run` will use this sandbox silently.")
