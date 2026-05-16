"""Module 3 — Gemini message generation, with resume PDF as context.

Generates a personalized cold WhatsApp message for one (profile, campaign) pair.

Gemini reads the actual resume PDF (Files API), so messages can reference
real experience — not just the sender_bio in the campaign YAML.

Auth: API-key mode if GEMINI_API_KEY is set, else Vertex AI.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from google import genai
from google.genai import types

from outreach.campaign import Campaign
from outreach.config import Config
from outreach.scraper import Profile


_PREAMBLE_RE = re.compile(
    r"^(sure[!,.]?\s*|here(?:'s|\sis)\s+(?:a\s+)?(?:cold\s+)?(?:whatsapp\s+)?message[:\-]?\s*)",
    re.IGNORECASE,
)


def _client(cfg: Config) -> genai.Client:
    if cfg.gemini_api_key:
        return genai.Client(api_key=cfg.gemini_api_key)
    return genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)


@lru_cache(maxsize=4)
def _upload_resume(resume_path_str: str, api_key: str) -> object | None:
    """Upload the resume PDF once per process. Returns the Files API handle.

    Returns None if no resume on disk or upload fails — message generation
    still works without it (falls back to campaign sender_bio only).
    """
    path = Path(resume_path_str)
    if not path.exists():
        return None
    try:
        client = genai.Client(api_key=api_key) if api_key else genai.Client()
        return client.files.upload(file=str(path))
    except Exception as e:
        print(f"[message] resume upload failed ({type(e).__name__}: {e}); continuing without it")
        return None


def _system_instruction(campaign: Campaign, resume_attached: bool) -> str:
    base = (
        f"You write cold WhatsApp messages for the sender below. "
        f"One message at a time, achieving the GOAL.\n\n"
        f"=== SENDER BIO ===\n{campaign.sender_bio}\n\n"
    )
    if resume_attached:
        base += (
            "=== RESUME (attached as PDF) ===\n"
            "Read the attached resume. Use real projects, internships, skills, and "
            "metrics from it to make the message specific — do not invent facts.\n\n"
        )
    base += (
        f"=== GOAL ===\n{campaign.goal}\n\n"
        f"=== AUDIENCE ===\n{campaign.audience}\n\n"
        f"=== TONE & STYLE ===\n{campaign.tone}\n\n"
        f"=== ASK (must appear naturally) ===\n{campaign.ask}\n\n"
        "Rules:\n"
        "- Output ONLY the message body. No preamble, no quotes, no 'Here's a message:'.\n"
        "- Reference something specific about the recipient's role / company / firm.\n"
        "- Pick ONE relevant project or skill from the resume to mention — not a list.\n"
        "- First name only.\n"
        "- No emojis unless the tone explicitly says so.\n"
        "- No corporate words: 'leverage', 'synergize', 'reach out', 'circle back'.\n"
        "- Under 600 chars. WhatsApp messages feel like a text, not a letter.\n"
    )
    return base


def _profile_block(profile: Profile) -> str:
    lines = [
        f"Name: {profile.name or 'Unknown'}",
        f"Headline: {profile.headline or '-'}",
        f"Role: {profile.role or '-'}",
        f"Company: {profile.company or '-'}",
        f"Location: {profile.location or '-'}",
    ]
    if profile.about:
        lines.append(f"About: {profile.about[:600]}")
    if profile.recent_activity:
        recent = "\n  - " + "\n  - ".join(a[:200] for a in profile.recent_activity[:3])
        lines.append(f"Recent activity:{recent}")
    return "\n".join(lines)


def _clean(raw: str) -> str:
    text = raw.strip()
    text = _PREAMBLE_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()
    return text


def generate_message(
    profile: Profile,
    campaign: Campaign,
    cfg: Config | None = None,
    *,
    temperature: float = 0.85,
    max_output_tokens: int = 4096,
) -> str:
    cfg = cfg or Config.load()
    client = _client(cfg)

    resume_handle = _upload_resume(str(cfg.resume_pdf), cfg.gemini_api_key)

    contents: list = [
        f"Recipient profile:\n\n{_profile_block(profile)}\n\nWrite the message now."
    ]
    if resume_handle is not None:
        contents.insert(0, resume_handle)

    gen_config: dict = {
        "system_instruction": _system_instruction(campaign, resume_attached=resume_handle is not None),
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    # Only Flash supports disabling thinking. Pro must think.
    if "flash" in cfg.gemini_model.lower():
        gen_config["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=contents,
        config=types.GenerateContentConfig(**gen_config),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty response. {response!r}")
    return _clean(text)
