"""Module 0 — AI-driven LinkedIn search via the user's logged-in Chrome.

NO Apify actor required. Flow:
  1. Gemini converts the campaign audience -> structured search params
  2. We construct a LinkedIn people-search URL from those params
  3. Playwright (in the user's real Chrome, LinkedIn already logged in)
     navigates to that URL and scrapes profile URLs from the results
  4. Pagination across N pages until we have `limit` candidates

The campaign's `audience` field drives the search. Companies named in the
campaign get included as a LinkedIn `currentCompany` filter when we can
match them to LinkedIn company IDs (or fall back to keyword search).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from google import genai
from google.genai import types
from playwright.sync_api import BrowserContext

from outreach.campaign import Campaign
from outreach.config import RAW_DIR, Config


@dataclass
class SearchQuery:
    """A LinkedIn search query Gemini builds from the campaign."""
    keywords: str
    titles: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    location: str = ""
    rationale: str = ""

    def to_url(self) -> str:
        """Build a LinkedIn people-search URL from the structured query."""
        kw_parts: list[str] = []
        if self.keywords:
            kw_parts.append(self.keywords)
        if self.titles:
            kw_parts.append("(" + " OR ".join(f'"{t}"' for t in self.titles) + ")")
        if self.companies:
            kw_parts.append("(" + " OR ".join(f'"{c}"' for c in self.companies) + ")")
        keywords = " ".join(kw_parts).strip() or self.keywords

        url = f"https://www.linkedin.com/search/results/people/?keywords={quote(keywords)}&origin=GLOBAL_SEARCH_HEADER"
        return url

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    url: str
    name: str = ""
    headline: str = ""
    company: str = ""
    location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_QUERY_REFINER_SYSTEM = (
    "You convert outreach campaign descriptions into LinkedIn people-search queries. "
    "Output strict JSON only — no preamble, no markdown fences.\n\n"
    "Schema:\n"
    "{\n"
    '  "keywords": "<core search terms — what they DO>",\n'
    '  "titles":    ["<role 1>", "<role 2>"],\n'
    '  "companies": ["<company 1>", "<company 2>"],\n'
    '  "location":  "<best single location, or empty>",\n'
    '  "rationale": "<one sentence explaining the choices>"\n'
    "}\n\n"
    "Guidelines:\n"
    "- keywords: 2-5 terms describing what these people work on or hire for.\n"
    "- titles: exact LinkedIn role strings (e.g. 'Manager', 'Senior Manager', 'Campus Recruiter').\n"
    "- companies: extract every company explicitly named in the audience. Use the canonical brand name on LinkedIn ('Deloitte', 'Bain & Company', 'McKinsey & Company', 'EY', 'KPMG', 'PwC', 'Accenture').\n"
    "- location: a city/region LinkedIn would recognize. Empty if global.\n"
    "- DO NOT invent companies the user didn't ask for.\n"
)


def _client(cfg: Config) -> genai.Client:
    if cfg.gemini_api_key:
        return genai.Client(api_key=cfg.gemini_api_key)
    return genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)


def refine_query(campaign: Campaign, cfg: Config | None = None) -> SearchQuery:
    cfg = cfg or Config.load()
    client = _client(cfg)
    prompt = (
        f"Campaign goal: {campaign.goal}\n\n"
        f"Audience description:\n{campaign.audience}\n\n"
        f"Convert to a LinkedIn people-search query."
    )
    gen_config: dict = {
        "system_instruction": _QUERY_REFINER_SYSTEM,
        "temperature": 0.3,
        "max_output_tokens": 4096,    # leaves room for thinking on 2.5-pro
        "response_mime_type": "application/json",
    }
    # Only Flash supports disabling thinking. Pro must think.
    if "flash" in cfg.gemini_model.lower():
        gen_config["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(**gen_config),
    )
    raw = (response.text or "").strip()
    if not raw:
        raise RuntimeError(f"Gemini returned empty query refinement. {response!r}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini returned non-JSON: {raw[:200]}") from e

    return SearchQuery(
        keywords=str(data.get("keywords", "")).strip(),
        titles=[str(t).strip() for t in data.get("titles", []) if str(t).strip()],
        companies=[str(c).strip() for c in data.get("companies", []) if str(c).strip()],
        location=str(data.get("location", "")).strip(),
        rationale=str(data.get("rationale", "")).strip(),
    )


_LINKEDIN_LOGIN_HINT = ("authwall", "linkedin.com/login", "checkpoint/lg/login", "session_redirect")
_RESULT_URL_RE = re.compile(r'href="(https://[^"]*linkedin\.com/in/[^"?#]+)')


def _is_logged_in(page) -> bool:
    """Crude check: if any login indicator appears in the URL, we're not logged in."""
    cur = (page.url or "").lower()
    return not any(h in cur for h in _LINKEDIN_LOGIN_HINT)


def _scrape_result_urls(page) -> list[str]:
    """Extract unique LinkedIn profile URLs from a search results page."""
    html = page.content()
    urls: list[str] = []
    seen: set[str] = set()
    for m in _RESULT_URL_RE.finditer(html):
        clean = m.group(1).split("?")[0].rstrip("/")
        # Skip generic / paged URLs that aren't real profiles.
        if "/in/" not in clean.lower():
            continue
        if clean in seen:
            continue
        seen.add(clean)
        urls.append(clean)
    return urls


def _dump(page, label: str) -> list[str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    png = RAW_DIR / f"discover-{stamp}-{label}.png"
    html = RAW_DIR / f"discover-{stamp}-{label}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return [str(png), str(html)]


def discover(
    campaign: Campaign,
    limit: int = 20,
    cfg: Config | None = None,
    *,
    ctx: BrowserContext,
    query_override: str | None = None,
    max_pages: int = 5,
) -> tuple[SearchQuery, list[Candidate]]:
    """Discover candidate profile URLs via in-Chrome LinkedIn search.

    `ctx` MUST be a Playwright BrowserContext launched against the user's
    real Chrome profile (LinkedIn already logged in).
    """
    cfg = cfg or Config.load()

    if query_override:
        query = SearchQuery(keywords=query_override, rationale="user-provided override")
    else:
        query = refine_query(campaign, cfg=cfg)

    page = ctx.new_page()
    candidates: list[Candidate] = []
    try:
        # Per-company sweep: if the campaign names specific firms, do one search
        # per company so each is represented. Otherwise: single combined search.
        sweeps: list[str]
        if query.companies:
            base_keywords = query.keywords
            title_clause = ""
            if query.titles:
                title_clause = " (" + " OR ".join(f'"{t}"' for t in query.titles) + ")"
            sweeps = [
                f"{base_keywords} {title_clause} \"{company}\"".strip()
                for company in query.companies
            ]
        else:
            sweeps = [query.to_url()]  # already a URL

        per_company_cap = max(1, limit // max(1, len(sweeps)))

        for sweep_idx, sweep in enumerate(sweeps):
            if sweep.startswith("http"):
                base_url = sweep
            else:
                base_url = (
                    "https://www.linkedin.com/search/results/people/"
                    f"?keywords={quote(sweep)}&origin=GLOBAL_SEARCH_HEADER"
                )

            collected_this_sweep = 0
            for pg in range(1, max_pages + 1):
                url = base_url + (f"&page={pg}" if pg > 1 else "")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except Exception as e:
                    print(f"[discover] navigation failed: {e}")
                    break

                page.wait_for_timeout(2500)

                if not _is_logged_in(page):
                    _dump(page, "not-logged-in")
                    raise RuntimeError(
                        "LinkedIn redirected to the login wall. Open Chrome, log into "
                        "LinkedIn manually in this profile, then retry."
                    )

                urls = _scrape_result_urls(page)
                if not urls:
                    _dump(page, f"empty-sweep{sweep_idx}-page{pg}")
                    break

                new_count = 0
                seen = {c.url for c in candidates}
                for u in urls:
                    if u in seen:
                        continue
                    candidates.append(Candidate(url=u))
                    seen.add(u)
                    new_count += 1
                    collected_this_sweep += 1
                    if collected_this_sweep >= per_company_cap or len(candidates) >= limit:
                        break

                print(f"[discover] sweep {sweep_idx + 1}/{len(sweeps)} page {pg}: +{new_count} (total {len(candidates)})")

                if collected_this_sweep >= per_company_cap or len(candidates) >= limit:
                    break
                # Polite delay between paginations.
                time.sleep(2)

            if len(candidates) >= limit:
                break

        # Persist what we found for debugging.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = RAW_DIR / f"discover-{stamp}-candidates.json"
        out.write_text(
            json.dumps({"query": query.to_dict(), "candidates": [c.to_dict() for c in candidates]}, indent=2),
            encoding="utf-8",
        )
        print(f"[discover] saved -> {out}")

        return query, candidates[:limit]

    finally:
        try:
            page.close()
        except Exception:
            pass
