"""Module 4 — WhatsApp Web sender via Playwright.

Drives the user's logged-in WhatsApp Web session in the shared Chrome
context. Default mode is DRY-RUN: the message is typed into the input box,
the attachment is attached, but the send button is NOT clicked. You must
explicitly pass `dry_run=False` (CLI: `--send`) to actually send.

WhatsApp Web's DOM changes often. Selectors below are candidates; use
`inspect_chat()` on the first real run to dump DOM and tune them.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout

from outreach.config import RAW_DIR, Config

SendStatus = Literal[
    "sent", "dry_run", "invalid_number", "not_on_whatsapp",
    "session_expired", "error",
]


@dataclass
class SendResult:
    phone: str
    status: SendStatus
    notes: str = ""
    debug_artifacts: list[str] = field(default_factory=list)


# Selector candidates — try in order, first match wins.
_SEL_QR = '[data-testid="qrcode"], canvas[aria-label*="Scan"]'
_SEL_INVALID_NUMBER_DIALOG = '[data-testid="popup-contents"], div[role="dialog"]'
_SEL_INVALID_NUMBER_TEXT = "phone number shared via url is invalid"

# Message input box (rich-text contenteditable).
_SEL_MSG_INPUT = [
    'div[contenteditable="true"][data-tab="10"]',
    'div[contenteditable="true"][role="textbox"][data-tab]',
    'footer div[contenteditable="true"][role="textbox"]',
    '[data-testid="conversation-compose-box-input"]',
]

# Attach button (paperclip or plus icon depending on WA version).
_SEL_ATTACH = [
    'button[title="Attach"]',
    'div[title="Attach"]',
    '[data-testid="conversation-clip"]',
    'span[data-icon="plus-rounded"]',
    'span[data-icon="clip"]',
    'span[data-icon="attach-menu-plus"]',
]

# Hidden file input that appears in the attach menu's Document option.
_SEL_FILE_INPUT = 'input[type="file"][accept*="*"], input[type="file"]'

# Send button.
_SEL_SEND = [
    'button[aria-label="Send"]',
    'span[data-icon="send"]',
    '[data-testid="compose-btn-send"]',
]


def _ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _dump_debug(page: Page, label: str) -> list[str]:
    stem = f"whatsapp-{_ts_slug()}-{label}"
    png = RAW_DIR / f"{stem}.png"
    html = RAW_DIR / f"{stem}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return [str(png), str(html)]


def normalize_phone(raw: str) -> str:
    """Strip everything to E.164-digits-only (no +) for the WhatsApp URL."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError(f"Could not parse phone from: {raw!r}")
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits  # assume Indian mobile
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError(f"Phone has implausible length ({len(digits)} digits): {digits}")
    return digits


def _wait_for_page_ready(page: Page, timeout_ms: int = 30_000) -> str:
    """Wait for one of: chat loaded, QR (session expired), invalid number dialog."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if page.locator(_SEL_QR).count() > 0 and page.locator(_SEL_QR).first.is_visible():
                return "qr"
        except Exception:
            pass
        try:
            dialog = page.locator(_SEL_INVALID_NUMBER_DIALOG).first
            if dialog.count() > 0 and dialog.is_visible():
                txt = (dialog.inner_text() or "").lower()
                if _SEL_INVALID_NUMBER_TEXT in txt or "invalid" in txt:
                    return "invalid"
        except Exception:
            pass
        for sel in _SEL_MSG_INPUT:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return "ready"
            except Exception:
                continue
        page.wait_for_timeout(500)
    return "timeout"


def _find_first_visible(page: Page, selectors: list[str], timeout_ms: int = 5000) -> object | None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        page.wait_for_timeout(250)
    return None


def _type_humanlike(loc, text: str) -> None:
    """Type with small randomized per-character delays."""
    for ch in text:
        loc.type(ch, delay=random.randint(20, 60))


def _attach_document(page: Page, file_path: Path, timeout_ms: int = 8000) -> bool:
    """Open the attach menu and set the file on the underlying file input."""
    attach_btn = _find_first_visible(page, _SEL_ATTACH, timeout_ms=3000)
    if attach_btn:
        try:
            attach_btn.click(timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(700)

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            inputs = page.locator(_SEL_FILE_INPUT)
            if inputs.count() > 0:
                inputs.last.set_input_files(str(file_path))
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def send_whatsapp(
    ctx: BrowserContext,
    phone: str,
    message: str,
    attachment: Path | None = None,
    *,
    dry_run: bool = True,
    cfg: Config | None = None,
) -> SendResult:
    cfg = cfg or Config.load()
    digits = normalize_phone(phone)

    if not message or len(message.strip()) < 10:
        return SendResult(phone=digits, status="error", notes="Message empty or implausibly short.")

    if attachment and not attachment.exists():
        return SendResult(phone=digits, status="error", notes=f"Attachment not found: {attachment}")

    # Use the URL preload for the *number*, but type the message ourselves so
    # we control encoding and can do human-like typing.
    url = f"https://web.whatsapp.com/send?phone={digits}&text={quote(' ')}"

    page = ctx.new_page()
    artifacts: list[str] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        state = _wait_for_page_ready(page, timeout_ms=45_000)

        if state == "qr":
            artifacts += _dump_debug(page, "qr")
            return SendResult(phone=digits, status="session_expired",
                              notes="WhatsApp Web is asking for a QR scan. Log in manually, then retry.",
                              debug_artifacts=artifacts)
        if state == "invalid":
            return SendResult(phone=digits, status="not_on_whatsapp",
                              notes="WA Web rejected the number (not on WhatsApp or malformed).")
        if state == "timeout":
            artifacts += _dump_debug(page, "ready-timeout")
            return SendResult(phone=digits, status="error",
                              notes="Chat input never appeared. WA Web may have changed its DOM.",
                              debug_artifacts=artifacts)

        # Type the message.
        input_box = _find_first_visible(page, _SEL_MSG_INPUT, timeout_ms=10_000)
        if not input_box:
            artifacts += _dump_debug(page, "no-input")
            return SendResult(phone=digits, status="error",
                              notes="Could not locate message input box.",
                              debug_artifacts=artifacts)
        input_box.click()
        _type_humanlike(input_box, message)
        page.wait_for_timeout(500)

        # Attach if requested.
        if attachment is not None:
            ok = _attach_document(page, attachment)
            if not ok:
                artifacts += _dump_debug(page, "attach-failed")
                return SendResult(phone=digits, status="error",
                                  notes="Could not attach file. See debug artifacts.",
                                  debug_artifacts=artifacts)
            # Wait for the preview/caption screen.
            page.wait_for_timeout(1500)

        if dry_run:
            artifacts += _dump_debug(page, "dry-run")
            return SendResult(phone=digits, status="dry_run",
                              notes="Message typed and attached (if any). Send NOT clicked.",
                              debug_artifacts=artifacts)

        send_btn = _find_first_visible(page, _SEL_SEND, timeout_ms=5000)
        if not send_btn:
            artifacts += _dump_debug(page, "no-send-btn")
            return SendResult(phone=digits, status="error",
                              notes="Could not locate send button.",
                              debug_artifacts=artifacts)
        send_btn.click()
        page.wait_for_timeout(2500)
        return SendResult(phone=digits, status="sent")

    except PWTimeout as e:
        artifacts += _dump_debug(page, "pw-timeout")
        return SendResult(phone=digits, status="error", notes=f"Timeout: {e}",
                          debug_artifacts=artifacts)
    except Exception as e:
        artifacts += _dump_debug(page, "exception")
        return SendResult(phone=digits, status="error",
                          notes=f"{type(e).__name__}: {e}", debug_artifacts=artifacts)
    finally:
        try:
            page.close()
        except Exception:
            pass


def inspect_chat(ctx: BrowserContext, phone: str, wait_seconds: int = 60) -> list[str]:
    """Open a WhatsApp chat and let the user inspect the DOM for selector tuning."""
    digits = normalize_phone(phone)
    page = ctx.new_page()
    try:
        page.goto(f"https://web.whatsapp.com/send?phone={digits}", wait_until="domcontentloaded", timeout=60_000)
        print(f"[inspect] Loaded WA chat for {digits}. Wait: {wait_seconds}s.")
        print(f"[inspect] Open DevTools, inspect: message input, attach button, send button.")
        page.wait_for_timeout(wait_seconds * 1000)
        return _dump_debug(page, "inspect")
    finally:
        try:
            page.close()
        except Exception:
            pass


def human_send_delay(cfg: Config | None = None) -> None:
    """Block for a random interval between sends (config-driven)."""
    cfg = cfg or Config.load()
    delay = random.randint(cfg.min_send_delay, cfg.max_send_delay)
    print(f"[sender] sleeping {delay}s before next send")
    time.sleep(delay)
