from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "outreach.db"


def _req(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {key}. Copy .env.example to .env and fill it in.")
    return val


def _opt(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip() or default


@dataclass(frozen=True)
class Config:
    apify_token: str
    apify_actor: str

    gcp_project: str
    gcp_location: str
    gemini_model: str
    google_credentials: str

    chrome_user_data_dir: str
    chrome_profile_directory: str
    chrome_executable: str

    resume_pdf: Path

    min_send_delay: int
    max_send_delay: int
    daily_send_limit: int
    followup_days: int

    @classmethod
    def load(cls) -> "Config":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        return cls(
            apify_token=_req("APIFY_TOKEN"),
            apify_actor=_req("APIFY_LINKEDIN_ACTOR"),
            gcp_project=_req("GCP_PROJECT_ID"),
            gcp_location=_opt("GCP_LOCATION", "us-central1"),
            gemini_model=_opt("GEMINI_MODEL", "gemini-2.0-flash-001"),
            google_credentials=_opt("GOOGLE_APPLICATION_CREDENTIALS"),
            chrome_user_data_dir=_req("CHROME_USER_DATA_DIR"),
            chrome_profile_directory=_opt("CHROME_PROFILE_DIRECTORY", "Default"),
            chrome_executable=_opt("CHROME_EXECUTABLE"),
            resume_pdf=Path(_req("RESUME_PDF")),
            min_send_delay=int(_opt("MIN_SEND_DELAY_SECONDS", "45")),
            max_send_delay=int(_opt("MAX_SEND_DELAY_SECONDS", "180")),
            daily_send_limit=int(_opt("DAILY_SEND_LIMIT", "20")),
            followup_days=int(_opt("FOLLOWUP_DAYS", "5")),
        )
