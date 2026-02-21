"""Slot state machine and message sending logic.

Each Slot wraps a single Playwright Page (browser tab) and manages
its lifecycle: FREE -> BUSY -> FREE, with ERROR as a recovery state.

Gemini-specific notes:
  - Quill.js editor (.ql-editor) for text input — contenteditable div, not textarea
  - Send button hidden when input is empty; becomes visible after text is entered
  - No direct <input type="file"> — upload via button that opens a menu
  - Response counting via <model-response> custom elements
"""

import asyncio
import logging
import re
import time
import uuid
from enum import Enum
from pathlib import Path

import pyperclip
from playwright.async_api import Page

from clipboard import extract_response_via_clipboard
from config import BrowserConfig
from gemini_selectors import MODEL_RESPONSE, STOP_BUTTON_ALL, find_element

logger = logging.getLogger(__name__)

# Clipboard paste verification
MAX_PASTE_RETRIES = 3

# Upload timeouts
UPLOAD_TIMEOUT_MS = 60_000
UPLOAD_POLL_INTERVAL_MS = 500

# Overall timeout for send_message (slightly above response timeout)
SEND_TIMEOUT_MARGIN_S = 100


class SlotState(Enum):
    """Possible states of a pool slot."""

    FREE = "FREE"
    BUSY = "BUSY"
    ERROR = "ERROR"


class LeaseExpiredError(Exception):
    """Raised when a client uses a slot whose lease has expired."""


class InvalidTokenError(Exception):
    """Raised when a client presents an invalid lease token."""


class Slot:
    """A single Gemini session slot backed by a browser tab.

    Manages state transitions, lease tokens, and the send_message flow.
    Thread-safety is guaranteed by the asyncio event loop (single-threaded).
    """

    def __init__(self, slot_id: int, page: Page, config: BrowserConfig):
        self._slot_id = slot_id
        self._page = page
        self._config = config
        self._state = SlotState.FREE
        self._owner: str | None = None
        self._lease_token: str | None = None
        self._last_activity: float = time.monotonic()
        self._message_count: int = 0
        self._message_preview: str = ""
        self._is_sending: bool = False

    # --- Properties ---

    @property
    def slot_id(self) -> int:
        return self._slot_id

    @property
    def page(self) -> Page:
        return self._page

    @page.setter
    def page(self, new_page: Page) -> None:
        self._page = new_page

    @property
    def state(self) -> SlotState:
        return self._state

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def lease_token(self) -> str | None:
        return self._lease_token

    @property
    def is_sending(self) -> bool:
        return self._is_sending

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def message_preview(self) -> str:
        return self._message_preview

    # --- State transitions ---

    def acquire(self, owner: str) -> str:
        """Transition FREE -> BUSY and assign a lease token.

        Args:
            owner: Identifier of the client acquiring this slot.

        Returns:
            The generated lease token.

        Raises:
            RuntimeError: If the slot is not FREE.
        """
        if self._state != SlotState.FREE:
            raise RuntimeError(
                f"Slot {self._slot_id} is {self._state}, cannot acquire"
            )
        self._state = SlotState.BUSY
        self._owner = owner
        self._lease_token = uuid.uuid4().hex
        self._last_activity = time.monotonic()
        self._message_count = 0
        self._message_preview = ""
        logger.info("Slot %d acquired by '%s'", self._slot_id, owner)
        return self._lease_token

    def release(self) -> None:
        """Transition BUSY -> FREE and clear ownership."""
        self._state = SlotState.FREE
        self._owner = None
        self._lease_token = None
        self._message_count = 0
        self._message_preview = ""
        self._is_sending = False
        logger.info("Slot %d released", self._slot_id)

    def mark_error(self) -> None:
        """Transition any state -> ERROR."""
        prev = self._state
        self._state = SlotState.ERROR
        self._owner = None
        self._lease_token = None
        self._is_sending = False
        logger.warning("Slot %d -> ERROR (was %s)", self._slot_id, prev)

    def mark_free(self, new_page: Page) -> None:
        """Transition ERROR -> FREE with a new page.

        Args:
            new_page: The replacement Playwright Page.
        """
        self._page = new_page
        self._state = SlotState.FREE
        self._owner = None
        self._lease_token = None
        self._message_count = 0
        self._message_preview = ""
        self._is_sending = False
        self._last_activity = time.monotonic()
        logger.info("Slot %d recovered -> FREE", self._slot_id)

    def validate_lease(self, token: str) -> None:
        """Verify that the token matches the current lease.

        Raises:
            LeaseExpiredError: If the slot is no longer BUSY (lease timed out).
            InvalidTokenError: If the token does not match.
        """
        if self._state != SlotState.BUSY:
            raise LeaseExpiredError(
                f"Slot {self._slot_id} is {self._state} (lease expired)"
            )
        if self._lease_token != token:
            raise InvalidTokenError(
                f"Invalid token for slot {self._slot_id}"
            )

    def touch(self) -> None:
        """Update last_activity timestamp."""
        self._last_activity = time.monotonic()

    # --- Message sending ---

    async def send_message(
        self, message: str, file_paths: list[str] | None = None
    ) -> tuple[str, str, int]:
        """Send a message to Gemini and wait for the response.

        Args:
            message: The message text to send.
            file_paths: Optional list of absolute file paths to attach.
                All files are uploaded before the message is sent.

        Returns:
            Tuple of (response_text, format, duration_ms).

        Raises:
            TimeoutError: If the send/receive cycle exceeds the configured timeout.
            RuntimeError: On browser/page errors.
        """
        self._is_sending = True
        self.touch()
        start_time = time.monotonic()

        timeout_s = (self._config.response_timeout_ms / 1000) + SEND_TIMEOUT_MARGIN_S

        try:
            response_text, response_format = await asyncio.wait_for(
                self._send_impl(message, file_paths),
                timeout=timeout_s,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            self._message_count += 1
            self._message_preview = message[:50]
            self.touch()

            return (response_text, response_format, duration_ms)

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            raise TimeoutError(
                f"send_message timeout ({timeout_s}s) on slot {self._slot_id} "
                f"after {duration_ms}ms"
            )
        finally:
            self._is_sending = False

    async def _send_impl(
        self, message: str, file_paths: list[str] | None = None
    ) -> tuple[str, str]:
        """Internal send implementation (without timeout guard)."""
        page = self._page

        # Count existing model-response elements (for conversation continuation)
        existing_responses = await page.query_selector_all(MODEL_RESPONSE)
        previous_count = len(existing_responses)

        # Upload files if any
        if file_paths:
            await self._upload_files(page, file_paths)

        # Find the Quill.js editor and paste the message
        textarea = await find_element(page, "prompt_textarea")
        await self._clear_paste_and_verify(page, textarea, message)

        # Wait briefly for send button to become visible (it's hidden when empty)
        await page.wait_for_timeout(300)

        # Send via Enter key (Gemini's rich-textarea has enterkeyhint="send")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1000)

        # Verify the message was actually sent by checking if the editor is
        # now empty (Gemini clears the editor after sending).
        editor_text = ""
        try:
            editor_text = _normalize_text(await textarea.inner_text())
        except Exception:
            pass

        if editor_text:
            # Editor still has content — Enter didn't send. Try clicking send
            # button, but ONLY if it's truly the send button (not the stop button).
            logger.warning(
                "Slot %d: editor not empty after Enter, trying send button", self._slot_id
            )
            try:
                send_btn = await find_element(page, "send_button")
                if send_btn and await send_btn.is_visible():
                    # Double-check: make sure no stop button is visible
                    # (if stop button exists, the message WAS sent and the
                    # send button we see is actually the stop button)
                    stop_btn = await page.query_selector(STOP_BUTTON_ALL)
                    if stop_btn and await stop_btn.is_visible():
                        logger.info(
                            "Slot %d: stop button visible — message was sent, skipping click",
                            self._slot_id,
                        )
                    else:
                        await send_btn.click(force=True)
                        logger.info("Slot %d: used send button fallback", self._slot_id)
            except Exception:
                pass
        else:
            logger.debug("Slot %d: editor empty — message sent via Enter", self._slot_id)

        # Wait for response and extract via clipboard
        return await extract_response_via_clipboard(
            page,
            previous_count,
            response_timeout_ms=self._config.response_timeout_ms,
        )

    # --- Internal helpers ---

    async def _upload_files(self, page: Page, file_paths: list[str]) -> None:
        """Attach one or more files via the upload menu.

        Gemini does NOT have a direct <input type="file"> visible at all times.
        Instead, there is a button that opens an upload menu. However, when
        the upload menu is triggered, a hidden <input type="file"> may be
        injected into the DOM. We use Playwright's file chooser event to
        handle the upload.

        Args:
            page: The Playwright page.
            file_paths: List of absolute file paths to upload.
        """
        # Try direct file input first (may exist even if not visible)
        file_input = await page.query_selector('input[type="file"]')
        if file_input:
            await file_input.set_input_files(file_paths)
            await self._wait_for_upload_complete(page)
            logger.info(
                "Slot %d: attached %d file(s) via direct input: %s",
                self._slot_id, len(file_paths),
                ", ".join(Path(p).name for p in file_paths),
            )
            return

        # No direct file input — use the upload button + file chooser event
        upload_btn = await page.query_selector(
            'button.upload-card-button, '
            'button[aria-label*="Datei hochladen"], '
            'button[aria-label*="Upload file"]'
        )
        if not upload_btn:
            raise RuntimeError(
                f"Slot {self._slot_id}: no upload button or file input found"
            )

        # Listen for the file chooser dialog, click the upload button, then
        # set the files on the chooser
        async with page.expect_file_chooser() as fc_info:
            await upload_btn.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(file_paths)

        await self._wait_for_upload_complete(page)
        logger.info(
            "Slot %d: attached %d file(s) via file chooser: %s",
            self._slot_id, len(file_paths),
            ", ".join(Path(p).name for p in file_paths),
        )

    async def _wait_for_upload_complete(
        self, page: Page, timeout_ms: int = UPLOAD_TIMEOUT_MS
    ) -> None:
        """Wait until file upload finishes (send button no longer disabled)."""
        await page.wait_for_timeout(1000)

        elapsed_ms = 0
        while elapsed_ms < timeout_ms:
            # Check if the send button is disabled (upload still in progress)
            disabled_btn = await page.query_selector(
                'button.send-button[disabled], '
                'button.send-button.disabled, '
                'button[aria-label="Nachricht senden"][disabled]'
            )
            if not disabled_btn:
                return
            await page.wait_for_timeout(UPLOAD_POLL_INTERVAL_MS)
            elapsed_ms += UPLOAD_POLL_INTERVAL_MS

        logger.warning("Slot %d: upload timeout, sending anyway...", self._slot_id)

    async def _clear_paste_and_verify(
        self, page: Page, textarea, message: str
    ) -> None:
        """Clear the Quill.js editor, paste message via clipboard, verify content.

        The Gemini editor is a contenteditable div (.ql-editor) managed by
        Quill.js. We interact with it via:
        1. Click to focus
        2. Ctrl+A to select all
        3. Backspace to clear
        4. Clipboard paste via Ctrl+V
        5. Verify by reading inner_text
        """
        expected = _normalize_text(message)

        for attempt in range(1, MAX_PASTE_RETRIES + 1):
            # Focus and clear
            await textarea.click()
            await page.wait_for_timeout(200)
            await page.keyboard.press("Control+A")
            await page.wait_for_timeout(100)
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(300)

            # Paste via OS clipboard
            pyperclip.copy(message)
            await page.keyboard.press("Control+V")
            await page.wait_for_timeout(500)

            # Verify — Quill.js stores content in p/br elements inside .ql-editor
            actual_raw = await textarea.inner_text()
            actual = _normalize_text(actual_raw)

            if actual == expected:
                logger.debug(
                    "Slot %d: textarea verified (%d chars, attempt %d)",
                    self._slot_id, len(expected), attempt,
                )
                return

            logger.warning(
                "Slot %d: textarea verification failed (attempt %d/%d): "
                "expected %d chars, got %d chars.",
                self._slot_id, attempt, MAX_PASTE_RETRIES,
                len(expected), len(actual),
            )

            if attempt < MAX_PASTE_RETRIES:
                await page.wait_for_timeout(500)

        raise RuntimeError(
            f"Slot {self._slot_id}: textarea content mismatch after "
            f"{MAX_PASTE_RETRIES} attempts."
        )


def _normalize_text(text: str) -> str:
    """Normalize text for verification: strip, unify line endings, collapse whitespace."""
    text = text.strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text)
