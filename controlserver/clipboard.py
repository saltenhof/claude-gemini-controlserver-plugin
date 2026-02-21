"""Clipboard-based response extraction with two-level locking.

The OS clipboard is a shared resource — system-wide, not per-process.
Two levels of protection:

1. asyncio.Lock — fast, intra-process (Gemini Slot 0 vs Slot 1 vs Slot 2)
2. File Lock    — cross-process (Gemini Server vs ChatGPT Server)

Both servers use the same lock file (~/.clipboard-lock). The file lock
uses OS-level kernel locks (msvcrt.locking on Windows) which are
automatically released when the process dies — no stale locks possible.

The WAIT phase (polling for Gemini to finish generating) happens
OUTSIDE the locks — only the short copy sequence (~2s) is locked.

Gemini-specific notes:
  - Response elements are <model-response> custom elements
  - Generation progress is tracked via aria-busy="true"/"false" on .markdown divs
  - Copy button has data-test-id="copy-button" and is always visible (no hover needed)
  - DOM fallback extracts from .markdown.markdown-main-panel
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pyperclip

from gemini_selectors import (
    GENERATION_BUSY,
    MODEL_RESPONSE,
    RESPONSE_TEXT,
    STOP_BUTTON_ALL,
)

logger = logging.getLogger(__name__)

# --- Locking ---
# Level 1: intra-process (fast, no thread overhead)
_clipboard_lock = asyncio.Lock()

# Level 2: cross-process file lock (Gemini server vs ChatGPT server)
# Both servers use the same lock file. OS releases the lock automatically
# if the process crashes — no deadlock possible.
_CLIPBOARD_LOCK_FILE = Path.home() / ".clipboard-lock"

if sys.platform == "win32":
    import msvcrt

    @asynccontextmanager
    async def _cross_process_clipboard_lock():
        """Acquire a cross-process file lock for clipboard access.

        Uses msvcrt.locking (Windows LockFileEx) — kernel-level lock
        that is auto-released on process exit/crash.
        Runs the blocking lock call in a thread to not block the event loop.
        """
        fh = open(_CLIPBOARD_LOCK_FILE, "w")
        try:
            await asyncio.to_thread(msvcrt.locking, fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            fh.close()
else:
    import fcntl

    @asynccontextmanager
    async def _cross_process_clipboard_lock():
        """Acquire a cross-process file lock for clipboard access (Unix)."""
        fh = open(_CLIPBOARD_LOCK_FILE, "w")
        try:
            await asyncio.to_thread(fcntl.flock, fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            fh.close()

# Timeouts
RESPONSE_POLL_INTERVAL_MS = 1000
RESPONSE_TIMEOUT_MS = 2_400_000  # 40 minutes (overridable via config)


async def extract_response_via_clipboard(
    page,
    previous_count: int = 0,
    response_timeout_ms: int = RESPONSE_TIMEOUT_MS,
) -> tuple[str, str]:
    """Wait for Gemini response, then extract text via copy button.

    The wait phase (polling for new message + generation complete) runs
    WITHOUT holding the clipboard lock. Only the final copy sequence
    (sentinel, click, read) acquires the lock briefly.

    Args:
        page: Playwright Page for this slot's tab.
        previous_count: Number of model-response elements before sending.
            Used to detect when a NEW response appears.
        response_timeout_ms: Max wait for Gemini to finish generating.

    Returns:
        Tuple of (response_text, format) where format is
        "markdown" (copy button worked) or "plaintext" (DOM fallback).

    Raises:
        TimeoutError: If Gemini does not respond within the timeout.
    """
    # --- Phase 1: Wait for new model-response element (NO lock) ---
    elapsed_ms = 0
    while elapsed_ms < 30_000:
        responses = await page.query_selector_all(MODEL_RESPONSE)
        if len(responses) > previous_count:
            break
        await page.wait_for_timeout(RESPONSE_POLL_INTERVAL_MS)
        elapsed_ms += RESPONSE_POLL_INTERVAL_MS
    else:
        logger.error("No new model-response element detected.")
        return ("", "plaintext")

    # --- Phase 2: Wait for generation to complete (NO lock) ---
    # Gemini signals generation in progress via aria-busy="true" on the
    # .markdown div. We also check for any stop button as a secondary signal.
    elapsed_ms = 0
    while elapsed_ms < response_timeout_ms:
        # Primary: check aria-busy on the last response's markdown div
        busy_elements = await page.query_selector_all(GENERATION_BUSY)
        if not busy_elements:
            # Secondary: also check for stop button (belt-and-suspenders)
            stop_btn = await page.query_selector(STOP_BUTTON_ALL)
            if not stop_btn:
                break
        await page.wait_for_timeout(RESPONSE_POLL_INTERVAL_MS)
        elapsed_ms += RESPONSE_POLL_INTERVAL_MS

    if elapsed_ms >= response_timeout_ms:
        raise TimeoutError(
            f"Gemini did not finish generating within {response_timeout_ms}ms"
        )

    # Extra settle time for DOM to stabilize after generation
    await page.wait_for_timeout(1500)

    # --- Phase 2b: Check for stopped/empty generation ---
    # If the user (or a double-click on the stop button) stopped the response,
    # the model-response element may be empty or contain a "stopped" indicator.
    responses = await page.query_selector_all(MODEL_RESPONSE)
    if responses:
        last_response = responses[-1]
        preview = ""
        try:
            preview = (await last_response.inner_text()).strip()
        except Exception:
            pass
        # Gemini shows "Du hast diese Antwort angehalten" or similar when stopped
        stopped_indicators = [
            "antwort angehalten",
            "response stopped",
            "you stopped this response",
        ]
        if any(indicator in preview.lower() for indicator in stopped_indicators):
            logger.error("Gemini response was stopped: '%s'", preview[:100])
            raise RuntimeError("Gemini response was stopped before completion")
        if not preview:
            logger.error("Gemini response element is empty")
            raise RuntimeError("Gemini response is empty — message may not have been sent")

    # --- Phase 3: Copy sequence (WITH locks, ~2s) ---
    # Level 1: intra-process (fast, avoids thread-pool overhead for common case)
    # Level 2: cross-process file lock (Gemini vs ChatGPT server)
    async with _clipboard_lock:
        async with _cross_process_clipboard_lock():
            return await _copy_response(page)


async def _copy_response(page) -> tuple[str, str]:
    """Find the last model-response, click its copy button, read clipboard.

    Must be called while holding _clipboard_lock.

    Gemini's action buttons (thumb up/down, regenerate, copy, more) are
    always visible in the response footer — no hovering is needed.

    Returns:
        Tuple of (text, "markdown"|"plaintext").
    """
    # Find all model-response elements
    responses = await page.query_selector_all(MODEL_RESPONSE)
    if not responses:
        logger.warning("No model-response elements found, using DOM fallback.")
        text = await _dom_scrape_response(page)
        return (text, "plaintext")

    last_response = responses[-1]

    # Find the copy button within this response (data-test-id="copy-button")
    copy_btn = await last_response.query_selector(
        'button[data-test-id="copy-button"]'
    )

    if not copy_btn:
        # Fallback: try aria-label based selectors within the response
        copy_btn = await last_response.query_selector(
            'button[aria-label="Kopieren"], '
            'button[aria-label="Copy"]'
        )

    if not copy_btn:
        # Last resort: try page-wide last copy button
        all_copy_btns = await page.query_selector_all(
            'button[data-test-id="copy-button"]'
        )
        if all_copy_btns:
            copy_btn = all_copy_btns[-1]

    if not copy_btn:
        logger.warning("Copy button not found in model-response, using DOM fallback.")
        text = await _dom_scrape_response(page)
        return (text, "plaintext")

    # Set sentinel to detect clipboard update
    pyperclip.copy("__SENTINEL__")

    # Click copy (force=True bypasses potential overlays)
    await copy_btn.click(force=True)
    await page.wait_for_timeout(800)

    # Try OS clipboard first
    clipboard_text = _read_os_clipboard()
    if clipboard_text and clipboard_text != "__SENTINEL__":
        return (clipboard_text, "markdown")

    # Fallback: JS Clipboard API
    try:
        js_text = await page.evaluate("navigator.clipboard.readText()")
        if js_text and js_text != "__SENTINEL__":
            return (js_text, "markdown")
    except Exception:
        pass

    # Last resort: DOM scrape (plaintext)
    logger.warning("Clipboard not updated, using DOM fallback.")
    text = await _dom_scrape_response(page)
    return (text, "plaintext")


def _read_os_clipboard() -> str:
    """Read the OS-level clipboard via pyperclip."""
    try:
        return pyperclip.paste()
    except Exception:
        return ""


async def _dom_scrape_response(page) -> str:
    """Extract the last assistant response directly from the DOM.

    Targets .markdown.markdown-main-panel within the last model-response element.
    Falls back to inner_text of the last model-response if markdown div not found.
    """
    # Try specific markdown panel first (cleanest text)
    responses = await page.query_selector_all(MODEL_RESPONSE)
    if responses:
        last = responses[-1]
        markdown_div = await last.query_selector(RESPONSE_TEXT)
        if markdown_div:
            return await markdown_div.inner_text()
        # Fallback: entire model-response text
        return await last.inner_text()

    # Ultimate fallback: any markdown panel on the page
    markdown_divs = await page.query_selector_all(RESPONSE_TEXT)
    if markdown_divs:
        return await markdown_divs[-1].inner_text()
    return ""
