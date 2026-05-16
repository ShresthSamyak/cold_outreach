"""Module 1 — LinkedIn profile scrape via the user's logged-in Chrome.

NO Apify actor required. Scrapes name, headline, current role, current
company, location, and about-blurb directly from the LinkedIn profile DOM.

The same Chrome visit is later used by Module 2 (ContactOut) to reveal the
phone, so we combine them in `scrape_and_reveal()` to avoid a second
LinkedIn page load (which would burn a second rate-limit token).

LinkedIn DOM changes often. Selectors are candidates; on first failure we
dump the raw HTML to data/raw/ so we can tune.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import BrowserContext, Page

from outreach.config import RAW_DIR


@dataclass
class Profile:
    url: str
    name: str = ""
    headline: str = ""
    role: str = ""
    company: str = ""
    location: str = ""
    about: str = ""
    recent_activity: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


# Multiple selector candidates per field, tried in order.
_SEL_NAME = [
    "h1.text-heading-xlarge",
    "h1.inline.t-24",
    "main h1",
]
_SEL_HEADLINE = [
    "div.text-body-medium.break-words",
    "div.pv-text-details__left-panel div.text-body-medium",
    ".ph5 .text-body-medium",
]
_SEL_LOCATION = [
    "span.text-body-small.inline.t-black--light.break-words",
    ".pv-text-details__left-panel span.text-body-small",
]
_SEL_ABOUT = [
    'section[data-section="summary"] .inline-show-more-text',
    'div#about ~ * .inline-show-more-text',
    'section:has(div#about) .display-flex .inline-show-more-text',
]
_SEL_EXPERIENCE_SECTION = [
    'section:has(div#experience)',
    'section[data-section="experience"]',
]


def _first_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=1500) or "").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


def _current_position(page: Page) -> tuple[str, str]:
    """Try to pull (role, company) from the first experience entry."""
    for sec_sel in _SEL_EXPERIENCE_SECTION:
        try:
            section = page.locator(sec_sel).first
            if section.count() == 0:
                continue
            text = (section.inner_text(timeout=2000) or "").strip()
            # The first non-trivial lines after "Experience" header are usually:
            # role
            # company · employment-type
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue
            # Drop the "Experience" header
            if lines[0].lower().startswith("experience"):
                lines = lines[1:]
            if len(lines) >= 2:
                role = lines[0]
                company_line = lines[1]
                # "Company · Full-time" -> "Company"
                company = re.split(r"\s+·\s+", company_line, maxsplit=1)[0].strip()
                return role, company
            if lines:
                return lines[0], ""
        except Exception:
            continue
    return "", ""


def _dump(page: Page, slug: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = RAW_DIR / f"profile-{stamp}-{slug}.html"
    try:
        path.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return str(path)


def scrape_profile(ctx: BrowserContext, linkedin_url: str) -> Profile:
    """Open a LinkedIn profile URL in the existing Chrome context and scrape it."""
    page = ctx.new_page()
    try:
        page.goto(linkedin_url, wait_until="domcontentloaded", timeout=45_000)
        # LinkedIn lazy-loads sections — give it a moment.
        page.wait_for_timeout(3000)
        try:
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)
        except Exception:
            pass

        if "linkedin.com/login" in page.url or "authwall" in page.url:
            dump = _dump(page, "authwall")
            raise RuntimeError(
                f"LinkedIn redirected to authwall while scraping {linkedin_url}. "
                f"Log in manually in this Chrome profile and retry. Dump: {dump}"
            )

        name = _first_text(page, _SEL_NAME)
        headline = _first_text(page, _SEL_HEADLINE)
        location = _first_text(page, _SEL_LOCATION)
        about = _first_text(page, _SEL_ABOUT)
        role, company = _current_position(page)

        # Heuristic fallback: company often appears in headline as "Title @ Company"
        if not company and headline and "@" in headline:
            company = headline.split("@", 1)[1].strip()
        if not role and headline:
            role = headline.split("@", 1)[0].strip(" -|·")

        profile = Profile(
            url=linkedin_url.split("?")[0].rstrip("/"),
            name=name,
            headline=headline,
            role=role,
            company=company,
            location=location,
            about=about[:2000],
        )
        if not name:
            profile.raw["debug_html"] = _dump(page, "no-name")
        return profile

    finally:
        try:
            page.close()
        except Exception:
            pass
