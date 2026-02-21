"""Multi-tab Playwright browser management for the Gemini Session Pool Service.

Manages one BrowserContext with N pages (tabs). Each tab is an independent slot.
Login state is shared across all tabs via cookies in the persistent profile.

Gemini-specific flow per slot:
  1. Navigate to gem_url (configurable, default: claude-code-sparring Gem)
  2. Detect login state (free vs. enterprise/premium)
  3. If not logged in: wait for manual login (email → password → 2FA)
  4. After login: ensure we're on the Gem page
  5. Switch model from Fast to Pro (Gemini defaults to Fast each session)
"""

import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright_stealth import Stealth

from config import BrowserConfig
from gemini_selectors import (
    COOKIE_ACCEPT_BTN,
    ENTERPRISE_INDICATORS,
    FREE_GEMINI_INDICATORS,
    GEMINI_ERROR_DIALOGS,
    GEMINI_URL,
    GOOGLE_BOT_DETECTION,
    LOGGED_IN_INDICATORS,
    NOT_LOGGED_IN_INDICATORS,
    SESSION_EXPIRED_INDICATORS,
)

logger = logging.getLogger(__name__)

# Maximum time to wait for manual login (5 minutes)
LOGIN_TIMEOUT_MS = 300_000
LOGIN_POLL_INTERVAL_MS = 2000


class PoolBrowser:
    """Manages a persistent Chromium browser context with multiple tabs.

    One Chrome instance, one BrowserContext (shared cookies/localStorage),
    N pages (one per slot). The browser profile is persisted on disk so
    that the Google login survives across service restarts.
    """

    def __init__(self, config: BrowserConfig):
        self._config = config
        self._playwright = None
        self._context: BrowserContext | None = None
        self._stealth = Stealth()
        self._initial_page: Page | None = None
        self._context_dead = False

    @property
    def gem_url(self) -> str:
        """The configured Gem URL for navigation."""
        return self._config.gem_url

    async def start(self) -> None:
        """Launch Chrome with a persistent profile. Does NOT create any pages.

        Pages are created later via create_slot_page().
        """
        profile_dir = self._config.resolved_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Clean stale lock files from previous crashed sessions
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = profile_dir / lock_file
            if lock_path.exists():
                lock_path.unlink(missing_ok=True)

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=self._config.headless,
            permissions=["clipboard-read", "clipboard-write"],
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation", "--no-sandbox"],
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
            ],
        )

        # Track context liveness via event (no tab creation needed)
        self._context_dead = False
        self._context.on("close", self._on_context_close)

        # Chrome needs at least one tab open. Keep the first page
        # for reuse as the first slot, close any extras.
        if self._context.pages:
            self._initial_page = self._context.pages[0]
            for extra in self._context.pages[1:]:
                await extra.close()
        else:
            self._initial_page = None

        logger.info("Browser started (headless=%s, profile=%s)", self._config.headless, profile_dir)

    async def create_slot_page(self) -> Page:
        """Create a new tab, apply stealth, navigate to Gem, dismiss cookies, set model.

        Returns:
            A Playwright Page ready for Gemini interaction on the configured Gem.

        Raises:
            RuntimeError: If navigation fails after all retries.
        """
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")

        # Reuse the initial page for the first slot (Chrome needs at least
        # one tab alive before new_page() works reliably)
        if self._initial_page is not None:
            page = self._initial_page
            self._initial_page = None
            await self._stealth.apply_stealth_async(page)
        else:
            page = await self._context.new_page()
            await self._stealth.apply_stealth_async(page)

        await self._navigate_to_gem(page)
        await self._dismiss_cookie_consent(page)
        await self._ensure_preferred_model(page)
        return page

    async def restart_slot_page(self, old_page: Page) -> Page:
        """Close an existing tab and create a fresh one.

        Used for slot recovery after errors.

        Args:
            old_page: The page to close.

        Returns:
            A new Page ready for Gemini interaction on the configured Gem.
        """
        try:
            if not old_page.is_closed():
                await old_page.close()
        except Exception:
            pass
        return await self.create_slot_page()

    async def navigate_to_new_chat(self, page: Page) -> None:
        """Navigate a page to the Gem URL to start a fresh conversation.

        Called when a slot is released — the next acquire gets a clean chat
        within the configured Gem (not the main Gemini app).

        Args:
            page: The Playwright Page to navigate.
        """
        await self._navigate_to_gem(page)
        await self._ensure_preferred_model(page)

    async def is_logged_in(self, page: Page) -> bool:
        """Check whether the user is logged in with a premium/enterprise account.

        Detection strategy (positive-only, most reliable first):
        1. URL guard: must be on gemini.google.com
        2. Enterprise indicator (logo container or enterprise class on rich-textarea)
        3. Google account avatar link (aria-label contains "Google-Konto:" or "Google Account:")
        4. Fallback: rich-textarea present (only rendered when app is fully loaded with session)

        Returns False during page navigations (safe for polling).
        """
        try:
            current_url = page.url
            if "gemini.google.com" not in current_url:
                return False

            # Strongest signal: enterprise indicator in the top bar
            enterprise = await page.query_selector(ENTERPRISE_INDICATORS)
            if enterprise:
                logger.debug("is_logged_in: enterprise indicator found → True")
                return True

            # Google account avatar link
            account_link = await page.query_selector(
                'a[aria-label*="Google-Konto:"], '
                'a[aria-label*="Google Account:"]'
            )
            if account_link:
                logger.debug("is_logged_in: account avatar link found → True")
                return True

            # Fallback: rich-textarea (only present when logged in
            # and the app is fully loaded)
            textarea = await page.query_selector("rich-textarea")
            if textarea:
                logger.debug("is_logged_in: rich-textarea found → True")
                return True

            logger.debug("is_logged_in: no positive indicator found → False")
            return False
        except Exception as exc:
            logger.debug("is_logged_in: exception: %s", exc)
            return False

    async def is_enterprise(self, page: Page) -> bool:
        """Check whether the logged-in account is an enterprise/premium account.

        Checks for enterprise indicators in the DOM:
        - rich-textarea with "enterprise" class
        - enterprise-indicator-logo-container in the header
        - enterprise-display div

        Returns False if not logged in or on error.
        """
        try:
            enterprise = await page.query_selector(ENTERPRISE_INDICATORS)
            return enterprise is not None
        except Exception:
            return False

    async def wait_for_login(self, page: Page) -> bool:
        """Poll until the user completes login or timeout is reached.

        The user will manually go through:
        1. Cookie consent (if fresh profile)
        2. Email entry
        3. Password entry
        4. Two-factor authentication (2FA)
        5. Landing on Gemini app

        This method polls is_logged_in() until it returns True.
        """
        elapsed_ms = 0
        _reloaded = False
        while elapsed_ms < LOGIN_TIMEOUT_MS:
            try:
                current_url = "<unknown>"
                try:
                    current_url = page.url
                except Exception:
                    pass
                logger.debug(
                    "Login poll: elapsed=%ds, page_closed=%s, url=%s",
                    elapsed_ms // 1000, page.is_closed(), current_url,
                )
                if page.is_closed():
                    logger.error("Login page was closed during login flow!")
                    return False

                # After returning from accounts.google.com, Gemini's SPA may
                # not refresh its body classes. Force a reload once to pick up
                # the new session state.
                if (
                    "gemini.google.com" in current_url
                    and elapsed_ms > 0
                    and not _reloaded
                ):
                    has_zero = await page.evaluate(
                        "document.body.classList.contains('zero-state-theme')"
                    )
                    if has_zero:
                        logger.info("Back on gemini.google.com with zero-state — reloading page")
                        await page.reload(wait_until="commit")
                        await page.wait_for_timeout(3000)
                        _reloaded = True

                if await self.is_logged_in(page):
                    return True
                await page.wait_for_timeout(LOGIN_POLL_INTERVAL_MS)
            except Exception as exc:
                logger.warning("Login poll exception: %s", exc)
                await asyncio.sleep(LOGIN_POLL_INTERVAL_MS / 1000)
            elapsed_ms += LOGIN_POLL_INTERVAL_MS
        return False

    async def detect_errors(self, page: Page) -> str | None:
        """Detect common Gemini error states on a specific page.

        Returns:
            A short description of the detected problem, or None if all clear.
        """
        # Google bot detection (instead of Cloudflare)
        try:
            bot_element = await page.query_selector(GOOGLE_BOT_DETECTION)
            if bot_element and await bot_element.is_visible():
                return "google_bot_detection"
        except Exception:
            pass

        # Session expired
        try:
            expired = await page.query_selector(SESSION_EXPIRED_INDICATORS)
            if expired and await expired.is_visible():
                return "session_expired"
        except Exception:
            pass

        # Error dialogs — try to auto-dismiss
        try:
            error_btn = await page.query_selector(GEMINI_ERROR_DIALOGS)
            if error_btn and await error_btn.is_visible():
                tag = await error_btn.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    await error_btn.click()
                    await page.wait_for_timeout(1000)
                    logger.info("Auto-dismissed Gemini error dialog.")
                    return "error_dialog_dismissed"
                return "error_dialog_visible"
        except Exception:
            pass

        return None

    def _on_context_close(self) -> None:
        """Event handler for context close/crash detection."""
        logger.warning("BrowserContext 'close' event fired — context is dead.")
        self._context_dead = True

    async def check_context_alive(self) -> bool:
        """Check whether the browser context is still alive.

        Uses a two-tier approach to avoid opening a new tab
        (which steals window focus on Windows):
        1. Fast: check the event-driven flag (instant, no I/O)
        2. Active: call cookies() as a lightweight IPC ping
        """
        if self._context is None or self._context_dead:
            return False
        try:
            await self._context.cookies()
            return True
        except Exception:
            self._context_dead = True
            return False

    async def check_page_alive(self, page: Page) -> bool:
        """Check whether a specific page/tab is still responsive."""
        try:
            if page.is_closed():
                return False
            await page.evaluate("document.readyState")
            return True
        except Exception:
            return False

    async def restart_browser(self) -> None:
        """Close and relaunch the entire browser context.

        All existing pages are destroyed. Callers must recreate slot pages.
        """
        logger.warning("Restarting browser context...")
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        await asyncio.sleep(2)
        await self.start()

    async def close(self) -> None:
        """Shut down browser context and Playwright."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        logger.info("Browser closed.")

    # --- Private helpers ---

    async def _navigate_to_gem(self, page: Page) -> None:
        """Navigate a page to the configured Gem URL with retries.

        This navigates to the Gem (e.g. "claude-code-sparring") so that
        all conversations happen within the Gem's context, not the main app.
        """
        gem_url = self._config.gem_url
        timeout_ms = self._config.navigation_timeout_ms
        max_retries = self._config.navigation_retries
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                await page.goto(
                    gem_url, timeout=timeout_ms, wait_until="commit"
                )
                # Wait for the rich-textarea or Google account link as a sign
                # that Gemini is ready (logged-in state)
                await page.wait_for_selector(
                    LOGGED_IN_INDICATORS, timeout=timeout_ms
                )
                await page.wait_for_timeout(1000)
                logger.info("Navigated to Gem: %s", gem_url)
                return
            except Exception as exc:
                last_error = exc
                try:
                    current_url = page.url
                except Exception:
                    current_url = "<unavailable>"
                logger.warning(
                    "Gem navigation attempt %d/%d failed: %s (URL: %s)",
                    attempt, max_retries, exc, current_url,
                )
                if attempt < max_retries:
                    await page.wait_for_timeout(2000)

        raise RuntimeError(
            f"Gem navigation failed after {max_retries} attempts: {gem_url}"
        ) from last_error

    async def _navigate_for_login(self, page: Page) -> None:
        """Navigate to the base Gemini app for login purposes.

        Used only during initial setup when the user needs to log in.
        The base app URL is more reliable for the login flow than a Gem URL.
        """
        timeout_ms = self._config.navigation_timeout_ms
        try:
            await page.goto(
                GEMINI_URL, timeout=timeout_ms, wait_until="commit"
            )
            await page.wait_for_timeout(2000)
        except Exception as exc:
            logger.warning("Login navigation failed: %s", exc)

    async def _dismiss_cookie_consent(self, page: Page) -> None:
        """Click 'Accept all' on the cookie consent banner if present."""
        try:
            accept_btn = await page.wait_for_selector(
                COOKIE_ACCEPT_BTN, timeout=3000
            )
            if accept_btn:
                await accept_btn.click()
                logger.info("Cookie consent accepted.")
                await page.wait_for_timeout(500)
        except Exception:
            pass

    async def _ensure_preferred_model(self, page: Page) -> None:
        """Check the current model and switch to the preferred one if needed.

        Gemini defaults to "Fast" (or "Flash") on every new session.
        This method reads the model selector button text and switches
        to the configured preferred_model (default: "Pro") if different.
        """
        preferred = self._config.preferred_model
        if not preferred:
            return

        try:
            # Wait for the model selector button (may take a moment after Gem navigation)
            try:
                model_btn = await page.wait_for_selector(
                    'button[data-test-id="bard-mode-menu-button"], '
                    'button[aria-label="Modusauswahl öffnen"]',
                    timeout=10_000,
                )
            except Exception:
                model_btn = None
            if not model_btn:
                logger.warning("Model selector button not found after 10s, skipping model switch.")
                return

            # Read current model from button text
            current_model = (await model_btn.inner_text()).strip()
            logger.info("Current model: '%s', preferred: '%s'", current_model, preferred)

            current_first_line = current_model.split("\n")[0].strip()
            if preferred.lower() == current_first_line.lower() or f" {preferred.lower()}" in f" {current_first_line.lower()}":
                logger.info("Model already set to '%s', no switch needed.", preferred)
                return

            # Click to open the model selection menu
            await model_btn.click()
            await page.wait_for_timeout(800)

            # Find and click the preferred model option in the dropdown
            # Angular Material menus use mat-menu-item or role="menuitem"
            menu_items = await page.query_selector_all(
                'button.mat-mdc-menu-item, '
                'button[role="menuitem"], '
                'div[role="menuitem"], '
                'mat-option'
            )

            clicked = False
            for item in menu_items:
                item_text = (await item.inner_text()).strip()
                # Use first line only (menu items have multi-line descriptions)
                first_line = item_text.split("\n")[0].strip()
                # Match as whole word to avoid "Pro" matching "Probleme"
                if preferred.lower() == first_line.lower() or f" {preferred.lower()}" in f" {first_line.lower()}":
                    await item.click()
                    clicked = True
                    logger.info("Switched model to '%s' (matched: '%s')", preferred, first_line)
                    break

            if not clicked:
                # Fallback: try text-based selector
                pro_option = await page.query_selector(
                    f'button:has-text("{preferred}"), '
                    f'div[role="menuitem"]:has-text("{preferred}")'
                )
                if pro_option:
                    await pro_option.click()
                    clicked = True
                    logger.info("Switched model to '%s' via text selector.", preferred)

            if not clicked:
                logger.warning(
                    "Could not find '%s' in model menu. Available: %s",
                    preferred,
                    [await item.inner_text() for item in menu_items],
                )
                # Close the menu by pressing Escape
                await page.keyboard.press("Escape")

            await page.wait_for_timeout(500)

        except Exception as exc:
            logger.warning("Model switch failed: %s", exc)
