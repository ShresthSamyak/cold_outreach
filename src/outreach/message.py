"""Module 3 — Gemini message generation via google-genai SDK.

Generates a personalized cold WhatsApp message for one (profile, campaign) pair.

Auth: tries API-key mode first (GEMINI_API_KEY), falls back to Vertex AI
(GCP_PROJECT_ID + GCP_LOCATION). Pick one in `.env`.

The campaign YAML defines what the LLM is "selling" (internship now, VC
funding later, anything else). The profile dict provides the personalization
hooks (name, role, company, recent activity).
"""

from __future__ import annotations

import re

from google import genai
from google.genai import types

from outreach.campaign import Campaign
from outreach.config import Config
from outreach.scraper import Profile


# Strip common LLM preamble like "Sure! Here's a message:" or surrounding quotes.
_PREAMBLE_RE = re.compile(
    r"^(sure[!,.]?\s*|here(?:'s|\sis)\s+(?:a\s+)?(?:cold\s+)?(?:whatsapp\s+)?message[:\-]?\s*)",
    re.IGNORECASE,
)


def _client(cfg: Config) -> genai.Client:
    if cfg.gemini_api_key:
        return genai.Client(api_key=cfg.gemini_api_key)
    return genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)


def _system_instruction(campaign: Campaign) -> str:
    return (
        f"You are writing a cold WhatsApp message on behalf of the sender below. "
        f"Your job: craft one short, specific, human-sounding message that achieves the GOAL.\n\n"
        f"=== SENDER BIO ===\n{campaign.sender_bio}\n\n"
        f"=== GOAL ===\n{campaign.goal}\n\n"
        f"=== AUDIENCE ===\n{campaign.audience}\n\n"
        f"=== TONE & STYLE ===\n{campaign.tone}\n\n"
        f"=== THE ASK (must appear naturally in the message) ===\n{campaign.ask}\n\n"
        f"Rules:\n"
        f"- Output ONLY the message body. No preamble, no quotes, no 'Here's a message:'.\n"
        f"- Reference something specific about the recipient's role/company. Do not invent facts.\n"
        f"- Greet them by first name only.\n"
        f"- Do NOT use emojis unless the tone explicitly calls for them.\n"
        f"- Do NOT use words like 'leverage', 'synergize', 'reach out'.\n"
        f"- Keep it under 600 characters. WhatsApp messages should feel like a text, not a letter.\n"
    )


def _profile_block(profile: Profile) -> str:
    lines = [
        f"Name: {profile.name or 'Unknown'}",
        f"Headline: {profile.headline or '-'}",
        f"Role: {profile.role or '-'}",
        f"Company: {profile.company or '-'}",
        f"Location: {profile.location or '-'}",
    ]
    if profile.about:
        about = profile.about[:600]
        lines.append(f"About: {about}")
    if profile.recent_activity:
        recent = "\n  - " + "\n  - ".join(a[:200] for a in profile.recent_activity[:3])
        lines.append(f"Recent activity:{recent}")
    return "\n".join(lines)


def _clean(raw: str) -> str:
    text = raw.strip()
    text = _PREAMBLE_RE.sub("", text).strip()
    # Strip wrapping quotes if the model added them.
    if len(text) >= 2 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()
    return text


def generate_message(
    profile: Profile,
    campaign: Campaign,
    cfg: Config | None = None,
    *,
    temperature: float = 0.7,
    max_output_tokens: int = 512,
) -> str:
    """Generate one cold message. Returns just the message body — no preamble."""
    cfg = cfg or Config.load()
    client = _client(cfg)

    user_prompt = (
        f"Recipient profile:\n\n{_profile_block(profile)}\n\n"
        f"Write the message now."
    )

    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=_system_instruction(campaign),
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
    )

    text = (response.text or "").strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty response. Raw response: {response!r}")
    return _clean(text)
