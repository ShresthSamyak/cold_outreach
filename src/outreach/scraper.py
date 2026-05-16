"""Module 1 — Apify LinkedIn scraper.

Calls a configurable Apify actor with a list of LinkedIn profile URLs (or a
search query) and returns normalized `Profile` records. Raw actor output is
also saved to `data/raw/<run_id>.json` for debugging when the normalizer
misses a field.

Designed to be tolerant of the common field-name variations across popular
LinkedIn profile actors. If your actor uses an input key other than
`profileUrls` / `urls`, set APIFY_ACTOR_INPUT_KEY in `.env`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from apify_client import ApifyClient

from outreach.config import RAW_DIR, Config


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


def _first(d: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return default


def _current_position(d: dict[str, Any]) -> tuple[str, str]:
    """Pull (role, company) from whatever shape the actor returned."""
    role = _first(d, "jobTitle", "title", "position", "currentPosition")
    company = _first(d, "companyName", "company", "currentCompany", "organization")
    if role and company:
        return role, company

    for key in ("experience", "experiences", "positions", "workExperience"):
        items = d.get(key)
        if isinstance(items, list) and items:
            top = items[0] if isinstance(items[0], dict) else {}
            role = role or _first(top, "title", "jobTitle", "position")
            company = company or _first(top, "companyName", "company", "organization", "name")
            if role or company:
                break
    return role, company


def _recent_activity(d: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("activities", "posts", "recentActivity", "updates"):
        items = d.get(key)
        if isinstance(items, list):
            for it in items[:5]:
                if isinstance(it, dict):
                    text = _first(it, "text", "content", "title", "summary")
                    if text:
                        out.append(text)
                elif isinstance(it, str) and it.strip():
                    out.append(it.strip())
            if out:
                break
    return out


def normalize(item: dict[str, Any]) -> Profile:
    url = _first(item, "url", "profileUrl", "linkedinUrl", "publicProfileUrl", "input")
    name = _first(item, "fullName", "name", "displayName") or " ".join(
        x for x in [_first(item, "firstName"), _first(item, "lastName")] if x
    ).strip()
    headline = _first(item, "headline", "subTitle", "tagline")
    role, company = _current_position(item)
    location = _first(item, "location", "geoLocation", "addressWithCountry", "city")
    about = _first(item, "about", "summary", "description", "bio")

    return Profile(
        url=url,
        name=name,
        headline=headline,
        role=role,
        company=company,
        location=location,
        about=about,
        recent_activity=_recent_activity(item),
        raw=item,
    )


def _save_raw(items: list[dict[str, Any]], run_id: str) -> Path:
    path = RAW_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{run_id}.json"
    path.write_text(json.dumps(items, indent=2, default=str), encoding="utf-8")
    return path


def scrape_profiles(urls: Iterable[str], cfg: Config | None = None) -> list[Profile]:
    """Run the configured Apify actor against `urls` and return normalized profiles."""
    cfg = cfg or Config.load()
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        return []

    input_key = os.environ.get("APIFY_ACTOR_INPUT_KEY", "profileUrls").strip() or "profileUrls"
    run_input: dict[str, Any] = {input_key: urls}

    client = ApifyClient(cfg.apify_token)
    actor = client.actor(cfg.apify_actor)
    run = actor.call(run_input=run_input)
    if not run:
        raise RuntimeError(f"Apify actor {cfg.apify_actor} did not return a run object.")

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError(f"Apify run has no defaultDatasetId. Status={run.get('status')}")

    items = list(client.dataset(dataset_id).iterate_items())
    saved = _save_raw(items, run.get("id", "norun"))
    profiles = [normalize(it) for it in items]
    print(f"[scraper] {len(profiles)} profile(s) from actor {cfg.apify_actor}; raw -> {saved}")
    return profiles
