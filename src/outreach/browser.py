"""Shared Playwright Chrome session.

Both Module 2 (ContactOut extraction) and Module 4 (WhatsApp Web) drive the
user's real Chrome — same user-data-dir, same extensions, same logged-in
sessions. Chrome only allows one process per user-data-dir, so both modules
must share a single browser context within a run.

Usage:

    from outreach.browser import session

    with session() as ctx:
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/in/someone/")
        ...

The context manager handles startup, profile-lock detection, and clean
shutdown.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Error as PWError, sync_playwright

from outreach.config import Config


class ProfileLockedError(RuntimeError):
    """Chrome user-data-dir is in use by another Chrome process."""


def _is_lock_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in ("singletonlock", "profile", "user data directory is already in use"))


@contextmanager
def session(cfg: Config | None = None, headless: bool = False) -> Iterator[BrowserContext]:
    """Launch (or reuse) a persistent Chrome context against the user's real profile."""
    cfg = cfg or Config.load()

    user_data_dir = Path(cfg.chrome_user_data_dir)
    if not user_data_dir.exists():
        raise RuntimeError(f"CHROME_USER_DATA_DIR does not exist: {user_data_dir}")

    launch_kwargs: dict = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "args": [
            f"--profile-directory={cfg.chrome_profile_directory}",
            # Keep extensions enabled; ContactOut must be present.
            "--disable-blink-features=AutomationControlled",
        ],
        "viewport": {"width": 1366, "height": 820},
        "ignore_default_args": ["--enable-automation"],
    }
    if cfg.chrome_executable and os.path.exists(cfg.chrome_executable):
        launch_kwargs["executable_path"] = cfg.chrome_executable
    else:
        # Fall back to Playwright's installed Chrome channel.
        launch_kwargs["channel"] = "chrome"

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        except PWError as e:
            if _is_lock_error(e):
                raise ProfileLockedError(
                    "Chrome is already running with this profile. "
                    "Close all Chrome windows and try again."
                ) from e
            raise

        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass
