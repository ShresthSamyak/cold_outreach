"""Module 4 — WhatsApp send via baileys (Node.js subprocess).

NOT browser automation. We invoke `node wa-bridge/index.js send ...` and
parse one JSON line from stdout. The Node side handles the protocol-level
WhatsApp connection (multi-device, persistent session, auto-reconnect).

One-time setup: `outreach wa-login` runs the QR flow. Sessions persist in
`wa-bridge/auth/` forever after.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from outreach.config import ROOT, Config

SendStatus = Literal[
    "sent", "dry_run", "not_on_whatsapp", "not_logged_in", "error",
]

WA_BRIDGE = ROOT / "wa-bridge" / "index.js"


@dataclass
class SendResult:
    phone: str
    status: SendStatus
    notes: str = ""
    debug_artifacts: list[str] = field(default_factory=list)


def normalize_phone(raw: str) -> str:
    """Strip to E.164-digits-only (no +). Assume Indian if 10 digits starting 6-9."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError(f"Could not parse phone from: {raw!r}")
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError(f"Phone has implausible length ({len(digits)} digits): {digits}")
    return digits


def _call_bridge(*args: str, timeout: int = 75) -> dict:
    """Run `node wa-bridge/index.js <args>` and return parsed JSON."""
    if not WA_BRIDGE.exists():
        raise RuntimeError(
            f"wa-bridge not found at {WA_BRIDGE}. Run `cd wa-bridge && npm install` first."
        )
    cmd = ["node", str(WA_BRIDGE), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    # Bridge writes ONE JSON line on stdout (last line); logs go to stderr.
    out = (proc.stdout or "").strip()
    last = out.splitlines()[-1] if out else ""
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "error": f"bridge returned non-JSON. exit={proc.returncode}. stdout={out!r} stderr={(proc.stderr or '')[-500:]!r}",
        }


def wa_status() -> dict:
    """Check WhatsApp login state."""
    return _call_bridge("status", timeout=20)


def wa_login() -> int:
    """Run the QR login flow interactively in the foreground."""
    if not WA_BRIDGE.exists():
        raise RuntimeError(f"wa-bridge not found at {WA_BRIDGE}.")
    cmd = ["node", str(WA_BRIDGE), "qr"]
    # Inherit stdio so user sees the QR + scans it on phone.
    proc = subprocess.run(cmd)
    return proc.returncode


def send_whatsapp(
    phone: str,
    message: str,
    attachment: Path | None = None,
    *,
    dry_run: bool = True,
    cfg: Config | None = None,
) -> SendResult:
    """Send (or dry-run) one WhatsApp message via baileys."""
    cfg = cfg or Config.load()

    if not message or len(message.strip()) < 10:
        return SendResult(phone=phone, status="error", notes="Message empty or implausibly short.")
    if attachment and not Path(attachment).exists():
        return SendResult(phone=phone, status="error", notes=f"Attachment not found: {attachment}")

    digits = normalize_phone(phone)

    args = ["send", "--phone", digits, "--message", message]
    if attachment:
        args += ["--attachment", str(Path(attachment).resolve())]
    if dry_run:
        args.append("--dry-run")

    try:
        result = _call_bridge(*args, timeout=80)
    except subprocess.TimeoutExpired:
        return SendResult(phone=digits, status="error", notes="wa-bridge timeout (80s)")

    status = result.get("status", "error")
    notes = result.get("error", "") or result.get("notes", "")
    return SendResult(phone=digits, status=status, notes=notes)


def human_send_delay(cfg: Config | None = None) -> None:
    cfg = cfg or Config.load()
    delay = random.randint(cfg.min_send_delay, cfg.max_send_delay)
    print(f"[sender] sleeping {delay}s before next send")
    time.sleep(delay)
