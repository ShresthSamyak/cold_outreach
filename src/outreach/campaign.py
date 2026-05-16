"""Campaign loader. Each campaign is a YAML file under `campaigns/`.

Adding a new outreach goal (e.g. VC fundraising) is just dropping a new file
in that directory — no code changes needed. The fields here feed directly
into the Gemini system prompt in Module 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from outreach.config import ROOT

CAMPAIGNS_DIR = ROOT / "campaigns"


@dataclass(frozen=True)
class Campaign:
    name: str
    goal: str
    audience: str
    sender_bio: str
    ask: str
    tone: str
    attach_resume: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any], filename_stem: str) -> "Campaign":
        required = ("name", "goal", "audience", "sender_bio", "ask", "tone")
        missing = [k for k in required if not str(data.get(k, "")).strip()]
        if missing:
            raise ValueError(f"Campaign '{filename_stem}' is missing required fields: {missing}")
        if data["name"] != filename_stem:
            raise ValueError(
                f"Campaign name '{data['name']}' must match filename '{filename_stem}.yaml'."
            )
        return cls(
            name=str(data["name"]).strip(),
            goal=str(data["goal"]).strip(),
            audience=str(data["audience"]).strip(),
            sender_bio=str(data["sender_bio"]).strip(),
            ask=str(data["ask"]).strip(),
            tone=str(data["tone"]).strip(),
            attach_resume=bool(data.get("attach_resume", True)),
        )


def list_campaigns(directory: Path = CAMPAIGNS_DIR) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def load_campaign(name: str, directory: Path = CAMPAIGNS_DIR) -> Campaign:
    path = directory / f"{name}.yaml"
    if not path.exists():
        available = list_campaigns(directory)
        raise FileNotFoundError(
            f"Campaign '{name}' not found at {path}. Available: {available or '(none)'}"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Campaign file {path} must be a YAML mapping.")
    return Campaign.from_dict(data, path.stem)
