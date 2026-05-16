"""Module 0 — Profile discovery.

Takes a campaign's free-text `audience` field, asks Gemini to convert it into
a tight LinkedIn search query, then calls an Apify LinkedIn people-search
actor to get candidate profile URLs.

Pipeline default: campaign in -> URLs out. No hand-curation.

Note on the search actor: the *search* actor is a different actor from the
*profile-detail* actor used in Module 1.
- Module 1 (scrape):  APIFY_LINKEDIN_ACTOR        — input: profile URLs
- Module 0 (discover): APIFY_LINKEDIN_SEARCH_ACTOR — input: keyword query
You set both in `.env`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from apify_client import ApifyClient
from google import genai
from google.genai import types

from outreach.campaign import Campaign
from outreach.config import RAW_DIR, Config


@dataclass
class SearchQuery:
    """A refined search query Gemini generates from the campaign audience."""
    keywords: str
    titles: list[str]
    location: str
    rationale: str

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
    "You convert free-text outreach audience descriptions into LinkedIn search "
    "queries. Output strict JSON only — no preamble, no markdown fences.\n\n"
    "Output schema:\n"
    "{\n"
    '  "keywords": "<single-line keyword string for LinkedIn search>",\n'
    '  "titles": ["<role 1>", "<role 2>", ...],\n'
    '  "location": "<best single location, or empty>",\n'
    '  "rationale": "<one sentence on why these terms>"\n'
    "}\n\n"
    "Guidelines:\n"
    "- Keywords should be 3-8 words, optimized for LinkedIn's search.\n"
    "- Titles should be exact role strings people use on LinkedIn (e.g. 'Founder', 'CTO').\n"
    "- Prefer specificity over breadth. Empty location is fine if audience is global.\n"
)


def refine_query(campaign: Campaign, cfg: Config | None = None) -> SearchQuery:
    """Ask Gemini to convert the campaign's audience description into a search query."""
    cfg = cfg or Config.load()

    if cfg.gemini_api_key:
        client = genai.Client(api_key=cfg.gemini_api_key)
    else:
        client = genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)

    user_prompt = (
        f"Campaign goal: {campaign.goal}\n\n"
        f"Audience description:\n{campaign.audience}\n\n"
        f"Convert to a LinkedIn search query."
    )

    gen_config: dict = {
        "system_instruction": _QUERY_REFINER_SYSTEM,
        "temperature": 0.3,
        "max_output_tokens": 1024,
        "response_mime_type": "application/json",
    }
    if "2.5" in cfg.gemini_model or "3." in cfg.gemini_model:
        gen_config["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(**gen_config),
    )
    raw = (response.text or "").strip()
    if not raw:
        raise RuntimeError(f"Gemini returned empty query. Response: {response!r}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini returned non-JSON query: {raw[:200]}") from e

    return SearchQuery(
        keywords=str(data.get("keywords", "")).strip(),
        titles=[str(t).strip() for t in data.get("titles", []) if str(t).strip()],
        location=str(data.get("location", "")).strip(),
        rationale=str(data.get("rationale", "")).strip(),
    )


def _first(d: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _normalize_candidate(item: dict[str, Any]) -> Candidate | None:
    url = _first(item, "url", "profileUrl", "linkedinUrl", "publicProfileUrl", "link")
    if not url or "linkedin.com/in/" not in url.lower():
        return None
    return Candidate(
        url=url.split("?")[0].rstrip("/"),
        name=_first(item, "fullName", "name", "displayName"),
        headline=_first(item, "headline", "subTitle", "tagline", "title"),
        company=_first(item, "companyName", "company", "currentCompany"),
        location=_first(item, "location", "geoLocation", "city"),
    )


def discover(
    campaign: Campaign,
    limit: int = 20,
    cfg: Config | None = None,
    *,
    query_override: str | None = None,
) -> tuple[SearchQuery, list[Candidate]]:
    """Discover candidate profiles for a campaign. Returns (query, candidates)."""
    cfg = cfg or Config.load()

    search_actor = os.environ.get("APIFY_LINKEDIN_SEARCH_ACTOR", "").strip()
    if not search_actor:
        raise RuntimeError(
            "APIFY_LINKEDIN_SEARCH_ACTOR not set. Pick a LinkedIn people-search "
            "actor on Apify (e.g. apimaestro/linkedin-people-search) and put its "
            "slug in .env."
        )

    if query_override:
        query = SearchQuery(keywords=query_override, titles=[], location="",
                            rationale="user-provided override")
    else:
        query = refine_query(campaign, cfg=cfg)

    # Search-actor input shapes vary. Common keys: `keywords`, `queries`,
    # `searchUrl`, `query`. Default to `keywords` + `limit`. Override via env.
    input_key = os.environ.get("APIFY_SEARCH_INPUT_KEY", "keywords").strip() or "keywords"
    run_input: dict[str, Any] = {
        input_key: query.keywords,
        "maxItems": limit,
        "limit": limit,
    }
    if query.location:
        run_input["location"] = query.location

    client = ApifyClient(cfg.apify_token)
    actor = client.actor(search_actor)
    run = actor.call(run_input=run_input)
    if not run:
        raise RuntimeError(f"Apify search actor {search_actor} returned no run object.")
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError(f"Search run has no defaultDatasetId. Status={run.get('status')}")

    items = list(client.dataset(dataset_id).iterate_items())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    raw_path = RAW_DIR / f"discover-{stamp}-{run.get('id', 'norun')}.json"
    raw_path.write_text(json.dumps(items, indent=2, default=str), encoding="utf-8")

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        c = _normalize_candidate(it)
        if c and c.url not in seen:
            seen.add(c.url)
            candidates.append(c)
        if len(candidates) >= limit:
            break

    print(f"[discover] query: {query.keywords!r}  -> {len(candidates)} candidates (raw: {raw_path})")
    return query, candidates
