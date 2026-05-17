"""Module 0 — Profile discovery via Gemini's built-in google_search tool.

No browser. No LinkedIn login. No Apify. Gemini-2.5-pro literally searches
the live web (via Google) for LinkedIn profile URLs that match the campaign
audience and returns them as JSON.

Quality of results depends on what Google has indexed. For senior people at
well-known firms (the Big 4 / MBB target), Google indexes a lot of LinkedIn
profile snippets. For obscure or new profiles, results will be thinner.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from google import genai
from google.genai import types

from outreach.campaign import Campaign
from outreach.config import RAW_DIR, Config


@dataclass
class SearchQuery:
    keywords: str
    titles: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    location: str = ""
    rationale: str = ""

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


def _client(cfg: Config) -> genai.Client:
    if cfg.gemini_api_key:
        return genai.Client(api_key=cfg.gemini_api_key)
    return genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)


_DISCOVERY_PROMPT = """\
You are finding REAL LinkedIn profiles for a cold outreach campaign.

## Campaign goal
{goal}

## Audience (who to find)
{audience}

## Your job
Use Google Search to find {limit} LinkedIn profile URLs of REAL people who
match the audience. Search the live web. Do not invent or guess URLs.

Constraints:
- Each URL must be of the form `https://www.linkedin.com/in/<slug>` (or
  `https://<lang>.linkedin.com/in/<slug>`). Strip any tracking params.
- Only profiles that EXIST in Google's index — no hallucinated slugs.
- Bias toward people whose role/company match the audience explicitly.
- If the audience names specific companies, distribute results across them.
- Skip company pages, posts, search result pages, etc. Profiles only.

## Output
Return ONLY a JSON object on its own (no markdown fences, no prose) with this
exact schema:

{{
  "search_summary": "<one sentence: what you searched for and why>",
  "candidates": [
    {{
      "url": "https://www.linkedin.com/in/<slug>",
      "name": "<their name if visible in search results, else empty>",
      "headline": "<their LinkedIn headline if visible, else empty>",
      "company": "<their current company if known, else empty>",
      "location": "<city/region if known, else empty>"
    }},
    ...
  ]
}}
"""


_URL_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.IGNORECASE)


def _strip_tracking(url: str) -> str:
    return url.split("?")[0].rstrip("/")


def _extract_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON parse — strip code fences and find the outermost {...}."""
    text = raw.strip()
    if text.startswith("```"):
        # Remove ```json ... ``` fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Try as-is first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}


def discover(
    campaign: Campaign,
    limit: int = 10,
    cfg: Config | None = None,
    *,
    query_override: str | None = None,
    **_ignored: Any,
) -> tuple[SearchQuery, list[Candidate]]:
    """Find LinkedIn profile URLs for the campaign via Gemini google_search."""
    cfg = cfg or Config.load()
    client = _client(cfg)

    audience = query_override or campaign.audience

    prompt = _DISCOVERY_PROMPT.format(
        goal=campaign.goal,
        audience=audience,
        limit=limit,
    )

    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.4,
            max_output_tokens=8192,
        ),
    )

    raw = (response.text or "").strip()

    # Persist raw response for debugging.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    raw_path = RAW_DIR / f"discover-{stamp}-gemini.txt"
    try:
        raw_path.write_text(raw, encoding="utf-8")
    except Exception:
        pass

    data = _extract_json(raw)

    # Build Candidate list from JSON.
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for item in data.get("candidates", []):
        if not isinstance(item, dict):
            continue
        url = _strip_tracking(str(item.get("url", "")).strip())
        if not url or not _URL_RE.match(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append(Candidate(
            url=url,
            name=str(item.get("name", "")).strip(),
            headline=str(item.get("headline", "")).strip(),
            company=str(item.get("company", "")).strip(),
            location=str(item.get("location", "")).strip(),
        ))

    # Fallback: if the JSON was malformed, regex-scrape any LinkedIn URLs from raw text.
    if not candidates:
        for m in _URL_RE.finditer(raw):
            url = _strip_tracking(m.group(0))
            if url in seen:
                continue
            seen.add(url)
            candidates.append(Candidate(url=url))
            if len(candidates) >= limit:
                break

    # Also consider grounding metadata if Gemini exposed it.
    try:
        chunks = response.candidates[0].grounding_metadata.grounding_chunks  # type: ignore[union-attr]
        for ch in chunks or []:
            url = getattr(getattr(ch, "web", None), "uri", "") or ""
            if "linkedin.com/in/" in url.lower():
                clean = _strip_tracking(url)
                if clean not in seen:
                    seen.add(clean)
                    candidates.append(Candidate(url=clean))
            if len(candidates) >= limit:
                break
    except Exception:
        pass

    query = SearchQuery(
        keywords=str(data.get("search_summary", "")).strip() or "google_search via Gemini",
        rationale="Gemini google_search grounding",
    )

    # Persist the final candidate set.
    out = RAW_DIR / f"discover-{stamp}-candidates.json"
    try:
        out.write_text(
            json.dumps({"query": query.to_dict(), "candidates": [c.to_dict() for c in candidates]}, indent=2),
            encoding="utf-8",
        )
        print(f"[discover] {len(candidates)} candidate URL(s)  raw -> {out}")
    except Exception:
        pass

    return query, candidates[:limit]
