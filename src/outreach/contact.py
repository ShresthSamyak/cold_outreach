"""Module 2 — ContactOut phone extraction via Playwright.

ContactOut is a Chrome extension that injects UI into LinkedIn profile pages
and exposes a "reveal phone" button. We drive that UI through Playwright on
the user's real Chrome (so the user's logged-in ContactOut session is live)
and pull the revealed phone number out of the DOM.

Honest note: the exact selectors for ContactOut's injected UI are not
guaranteed stable. This module ships with an `inspect` mode that opens the
page, waits, and dumps the entire DOM + a screenshot to data/raw/. Use that
on the first real profile to lock in the right selectors, then tune
`_CANDIDATE_*` lists below.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout

from outreach.config import RAW_DIR

Status = Literal["found", "no_phone", "quota_exhausted", "extension_not_loaded", "error"]


@dataclass
class ContactResult:
    linkedin_url: str
    status: Status
    phone: str | None = None
    notes: str = ""
    debug_artifacts: list[str] | None = None


# Candidate selectors for the ContactOut injected widget / panel. We try each
# in order. Add more as we observe them in the wild.
_CANDIDATE_WIDGET_SELECTORS = [
    '[class*="contactout" i]',
    '[id*="contactout" i]',
    '[data-contactout]',
    'iframe[src*="contactout" i]',
]

# Candidate selectors for the "reveal phone" / "show phone" button.
_CANDIDATE_REVEAL_SELECTORS = [
    'button:has-text("Show phone")',
    'button:has-text("Reveal phone")',
    'button:has-text("Get phone")',
    '[class*="contactout" i] button:has-text("Show")',
    '[class*="contactout" i] button:has-text("phone" i)',
]

# Marker text indicating quota exhausted.
_QUOTA_HINTS = [
    "out of credits",
    "quota exceeded",
    "upgrade to reveal",
    "daily limit",
    "no credits left",
]

# Phone regex — Indian mobile (+91 / 91 / bare 10-digit starting 6-9) and a
# permissive international fallback.
_PHONE_RE = re.compile(
    r"(?:\+?91[\s\-]?)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}"
    r"|\+\d{1,3}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}"
)


def _ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _dump_debug(page: Page, label: str) -> list[str]:
    """Save a screenshot + serialized DOM for offline inspection."""
    stem = f"contactout-{_ts_slug()}-{label}"
    png = RAW_DIR / f"{stem}.png"
    html = RAW_DIR / f"{stem}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception as e:
        png = RAW_DIR / f"{stem}.screenshot-FAILED-{type(e).__name__}.txt"
        png.write_text(str(e), encoding="utf-8")
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return [str(png), str(html)]


def _looks_like_quota_exhausted(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _QUOTA_HINTS)


def _find_widget(page: Page, timeout_ms: int = 8000) -> bool:
    """Wait for any ContactOut widget marker to appear."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in _CANDIDATE_WIDGET_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        page.wait_for_timeout(400)
    return False


def _click_reveal(page: Page) -> bool:
    for sel in _CANDIDATE_REVEAL_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _extract_phone_from_page(page: Page) -> str | None:
    # Search the full rendered text. ContactOut sometimes injects into the
    # page DOM and sometimes into an iframe; check both.
    candidates: list[str] = []
    try:
        candidates.append(page.inner_text("body"))
    except Exception:
        pass
    for frame in page.frames:
        try:
            candidates.append(frame.inner_text("body"))
        except Exception:
            continue

    for blob in candidates:
        m = _PHONE_RE.search(blob)
        if m:
            return _normalize_phone(m.group(0))
    return None


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if not raw.startswith("+"):
        return "+" + digits
    return "+" + digits


def reveal_phone(ctx: BrowserContext, linkedin_url: str, *, debug: bool = False) -> ContactResult:
    """Open the LinkedIn profile, drive ContactOut, return the phone."""
    page = ctx.new_page()
    artifacts: list[str] = []
    try:
        page.goto(linkedin_url, wait_until="domcontentloaded", timeout=45_000)
        # Let LinkedIn settle + give ContactOut a moment to inject.
        page.wait_for_timeout(3500)

        if debug:
            artifacts += _dump_debug(page, "initial")

        if not _find_widget(page):
            artifacts += _dump_debug(page, "no-widget")
            return ContactResult(
                linkedin_url=linkedin_url,
                status="extension_not_loaded",
                notes="ContactOut widget never appeared. Is the extension installed and logged in?",
                debug_artifacts=artifacts,
            )

        # First check — phone may already be visible without a reveal click.
        phone = _extract_phone_from_page(page)
        if phone:
            return ContactResult(linkedin_url=linkedin_url, status="found", phone=phone)

        clicked = _click_reveal(page)
        if clicked:
            page.wait_for_timeout(2500)
            phone = _extract_phone_from_page(page)
            if phone:
                return ContactResult(linkedin_url=linkedin_url, status="found", phone=phone)

        # See if ContactOut is telling us quota is gone.
        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""
        if _looks_like_quota_exhausted(body_text):
            return ContactResult(
                linkedin_url=linkedin_url,
                status="quota_exhausted",
                notes="ContactOut UI mentions quota / credits exhausted.",
            )

        artifacts += _dump_debug(page, "no-phone")
        return ContactResult(
            linkedin_url=linkedin_url,
            status="no_phone",
            notes="Widget loaded but no phone surfaced. Profile may have no phone on file.",
            debug_artifacts=artifacts,
        )

    except PWTimeout as e:
        artifacts += _dump_debug(page, "timeout")
        return ContactResult(
            linkedin_url=linkedin_url, status="error", notes=f"Timeout: {e}", debug_artifacts=artifacts
        )
    except Exception as e:
        artifacts += _dump_debug(page, "error")
        return ContactResult(
            linkedin_url=linkedin_url,
            status="error",
            notes=f"{type(e).__name__}: {e}",
            debug_artifacts=artifacts,
        )
    finally:
        try:
            page.close()
        except Exception:
            pass


def inspect_profile(ctx: BrowserContext, linkedin_url: str, wait_seconds: int = 60) -> list[str]:
    """Open a profile, wait for the user to inspect manually, then dump DOM.

    Use this on your FIRST real profile to capture what ContactOut's UI
    actually looks like, so we can lock in selectors.
    """
    page = ctx.new_page()
    try:
        page.goto(linkedin_url, wait_until="domcontentloaded", timeout=45_000)
        print(f"[inspect] Loaded {linkedin_url}")
        print(f"[inspect] Waiting {wait_seconds}s — open DevTools, expand ContactOut, click 'Show phone'.")
        print(f"[inspect] DOM + screenshot will be dumped to data/raw/ after the wait.")
        page.wait_for_timeout(wait_seconds * 1000)
        return _dump_debug(page, "inspect")
    finally:
        try:
            page.close()
        except Exception:
            pass
