"""Chrome session via CDP attach — Browser-Use style.

We never launch our own browser context that competes with the user's Chrome.
Instead, Chrome runs ONCE with `--remote-debugging-port=9222` (auto-started
on first run, or via `outreach launch-chrome`), and Playwright attaches over
CDP. The user keeps using Chrome normally; we just open background tabs.

After the one-time launch, every subsequent `outreach run` just attaches —
no closing Chrome, no profile-lock errors.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

from outreach.config import Config

CDP_PORT = 9222
CDP_VERSION_URL = f"http://localhost:{CDP_PORT}/json/version"


def is_cdp_up(timeout: float = 1.0) -> bool:
    """Return True if Chrome's remote-debugging port is responding."""
    try:
        with urllib.request.urlopen(CDP_VERSION_URL, timeout=timeout):
            return True
    except Exception:
        return False


def _kill_chrome_processes() -> int:
    """Force-kill any running chrome.exe processes (Windows). Returns count killed."""
    if os.name != "nt":
        # On non-Windows, leave to the user. We don't run there anyway.
        return 0
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
        # taskkill returns 0 if it killed anything, 128 if no matching process.
        if "SUCCESS" in (result.stdout or ""):
            return result.stdout.count("SUCCESS")
        return 0
    except Exception:
        return 0


def launch_chrome_with_debug(cfg: Config | None = None, wait_seconds: int = 15) -> None:
    """Spawn Chrome with the debug port enabled. Blocks until the port is up."""
    cfg = cfg or Config.load()

    if is_cdp_up():
        print(f"[browser] CDP already up on :{CDP_PORT} — nothing to do.")
        return

    # Any running Chrome WITHOUT the debug port will swallow our launch
    # ("Opening in existing browser session"). Kill them first.
    killed = _kill_chrome_processes()
    if killed:
        print(f"[browser] terminated {killed} existing chrome.exe process(es)")
        time.sleep(1.5)  # let Windows release file handles

    chrome_exe = cfg.chrome_executable
    if not chrome_exe or not os.path.exists(chrome_exe):
        raise RuntimeError(
            f"CHROME_EXECUTABLE not found at {chrome_exe!r}. "
            f"Set the correct path in .env."
        )

    args = [
        chrome_exe,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={cfg.chrome_user_data_dir}",
        f"--profile-directory={cfg.chrome_profile_directory}",
        "--restore-last-session",  # bring his tabs back
    ]
    print(f"[browser] launching Chrome with --remote-debugging-port={CDP_PORT}...")
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so Chrome outlives us.
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(args, creationflags=flags, close_fds=True)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_cdp_up():
            print("[browser] Chrome is up. Your tabs will restore. Agent attaching via CDP.")
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome didn't open the debug port within {wait_seconds}s. "
        f"Try `uv run outreach launch-chrome` manually."
    )


@contextmanager
def session(cfg: Config | None = None) -> Iterator[BrowserContext]:
    """Attach to Chrome via CDP. Auto-launches Chrome if not already running with the port."""
    cfg = cfg or Config.load()

    if not is_cdp_up():
        launch_chrome_with_debug(cfg)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
        # Use the default context — has the user's cookies, sessions, extensions.
        if not browser.contexts:
            ctx = browser.new_context()
        else:
            ctx = browser.contexts[0]
        try:
            yield ctx
        finally:
            try:
                browser.close()  # disconnect CDP, Chrome process keeps running
            except Exception:
                pass
