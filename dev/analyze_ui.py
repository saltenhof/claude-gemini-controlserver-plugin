"""Gemini UI Analyzer — Playwright-based DOM inspection tool.

Opens Gemini in a real Chrome browser (with persistent profile for login),
takes screenshots and dumps DOM structure at each step. Designed to be
run interactively to discover CSS selectors for automation.

Usage:
    python analyze_ui.py                  # Full analysis (login → landing → chat → response → copy)
    python analyze_ui.py --step login     # Only login flow analysis
    python analyze_ui.py --step landing   # Only landing page
    python analyze_ui.py --step chat      # Only chat interface
    python analyze_ui.py --step response  # Send test message + analyze response
    python analyze_ui.py --step copy      # Analyze copy/action buttons
    python analyze_ui.py --step sidebar   # Sidebar/navigation

Steps:
    login     — Navigate to Gemini, track each login/2FA state with screenshots+DOM
    landing   — Analyze the loaded Gemini app page
    chat      — Analyze the chat input area (textarea, buttons, file upload)
    response  — Send a test message and analyze response structure
    copy      — Analyze copy/action buttons on a response
    sidebar   — Analyze sidebar/navigation structure
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# Output directory for screenshots and DOM dumps
OUTPUT_DIR = Path(__file__).parent / "analysis_output"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

GEMINI_URL = "https://gemini.google.com/app"
PROFILE_DIR = Path(os.path.expanduser("~/.gemini-session-pool/user_data"))


# ---------------------------------------------------------------------------
# Login state detection
# ---------------------------------------------------------------------------

class LoginState:
    """Possible states during the Google login flow."""
    ALREADY_LOGGED_IN = "already_logged_in"
    GOOGLE_EMAIL = "google_email_entry"
    GOOGLE_PASSWORD = "google_password_entry"
    GOOGLE_2FA_PROMPT = "google_2fa_phone_prompt"
    GOOGLE_2FA_AUTHENTICATOR = "google_2fa_authenticator"
    GOOGLE_2FA_SMS = "google_2fa_sms"
    GOOGLE_2FA_SECURITY_KEY = "google_2fa_security_key"
    GOOGLE_2FA_BACKUP_CODES = "google_2fa_backup_codes"
    GOOGLE_2FA_UNKNOWN = "google_2fa_unknown"
    GOOGLE_CONSENT = "google_consent_screen"
    GOOGLE_CAPTCHA = "google_captcha"
    GOOGLE_ACCOUNT_CHOOSER = "google_account_chooser"
    GEMINI_LOADING = "gemini_loading"
    GEMINI_READY = "gemini_ready"
    GEMINI_TERMS = "gemini_terms_acceptance"
    UNKNOWN = "unknown"


async def detect_login_state(page: Page) -> tuple[str, dict]:
    """Detect the current login/authentication state.

    Returns:
        Tuple of (state_name, details_dict) with diagnostic info.
    """
    url = page.url
    details = {"url": url, "title": ""}

    try:
        details["title"] = await page.title()
    except Exception:
        pass

    # --- Already on Gemini? ---
    if "gemini.google.com" in url:
        # Check if there's a consent/terms page
        terms_indicators = await page.query_selector_all(
            'button:has-text("I agree"), '
            'button:has-text("Ich stimme zu"), '
            'button:has-text("Accept"), '
            'button:has-text("Akzeptieren"), '
            'button:has-text("Try Gemini"), '
            'button:has-text("Gemini ausprobieren")'
        )
        for el in terms_indicators:
            if await el.is_visible():
                details["terms_button"] = await el.inner_text()
                return LoginState.GEMINI_TERMS, details

        # Check if the chat input is visible (= fully loaded)
        for sel in ['rich-textarea', 'div[contenteditable="true"]',
                     '.input-area-container', 'div[role="textbox"]',
                     'textarea']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                details["input_selector"] = sel
                return LoginState.GEMINI_READY, details

        # Page loading but no input yet
        return LoginState.GEMINI_LOADING, details

    # --- Google Accounts flow ---
    if "accounts.google.com" not in url:
        return LoginState.UNKNOWN, details

    # Detailed state detection within accounts.google.com
    state_checks = await page.evaluate("""() => {
        const body = document.body;
        if (!body) return {};

        const text = body.innerText || '';
        const html = body.innerHTML || '';

        return {
            // Text content indicators
            hasEmailInput: !!document.querySelector('input[type="email"]'),
            hasPasswordInput: !!document.querySelector('input[type="password"]'),
            hasPhonePrompt: text.includes('Tippen Sie auf') || text.includes('Tap yes')
                || text.includes('Auf dem Smartphone bestätigen')
                || text.includes('Confirm on your phone'),
            hasAuthenticator: text.includes('Authenticator') || text.includes('Bestätigungscode')
                || text.includes('verification code') || text.includes('Google Authenticator'),
            hasSmsCode: text.includes('SMS') || text.includes('Bestätigungscode per SMS')
                || text.includes('verification code via SMS')
                || text.includes('code we sent'),
            hasSecurityKey: text.includes('Sicherheitsschlüssel') || text.includes('Security key')
                || text.includes('security key'),
            hasBackupCodes: text.includes('Ersatzcode') || text.includes('Backup code')
                || text.includes('backup codes'),
            hasCaptcha: !!document.querySelector('iframe[src*="recaptcha"]')
                || !!document.querySelector('#captchaimg')
                || text.includes('Captcha'),
            hasAccountChooser: text.includes('Konto auswählen') || text.includes('Choose an account')
                || !!document.querySelector('[data-identifier]'),
            hasConsentScreen: text.includes('hat Zugriff') || text.includes('wants access')
                || text.includes('Allow') && text.includes('permission'),

            // 2FA challenge identifiers
            has2faChallenge: !!document.querySelector('[data-challengetype]'),
            challengeType: document.querySelector('[data-challengetype]')?.getAttribute('data-challengetype') || null,

            // Visible input types
            visibleInputs: Array.from(document.querySelectorAll('input')).filter(
                i => i.getBoundingClientRect().height > 0
            ).map(i => ({
                type: i.type,
                name: i.name,
                id: i.id,
                ariaLabel: i.getAttribute('aria-label'),
                placeholder: i.placeholder,
            })),

            // Visible buttons
            visibleButtons: Array.from(document.querySelectorAll('button, div[role="button"]')).filter(
                b => b.getBoundingClientRect().height > 0
            ).map(b => ({
                text: b.textContent.trim().substring(0, 80),
                id: b.id,
                jsname: b.getAttribute('jsname'),
                class: b.className?.substring(0, 100),
            })),

            // Headings
            headings: Array.from(document.querySelectorAll('h1, h2, h3')).map(
                h => h.textContent.trim().substring(0, 100)
            ),

            // URL path hints
            urlPath: window.location.pathname,
            urlParams: window.location.search,
        };
    }""")

    details["checks"] = state_checks

    # Account chooser
    if state_checks.get("hasAccountChooser"):
        return LoginState.GOOGLE_ACCOUNT_CHOOSER, details

    # Email entry
    if state_checks.get("hasEmailInput") and not state_checks.get("hasPasswordInput"):
        return LoginState.GOOGLE_EMAIL, details

    # Password entry
    if state_checks.get("hasPasswordInput"):
        return LoginState.GOOGLE_PASSWORD, details

    # Captcha
    if state_checks.get("hasCaptcha"):
        return LoginState.GOOGLE_CAPTCHA, details

    # 2FA states (order matters: most specific first)
    if state_checks.get("hasSecurityKey"):
        return LoginState.GOOGLE_2FA_SECURITY_KEY, details

    if state_checks.get("hasAuthenticator"):
        return LoginState.GOOGLE_2FA_AUTHENTICATOR, details

    if state_checks.get("hasSmsCode"):
        return LoginState.GOOGLE_2FA_SMS, details

    if state_checks.get("hasPhonePrompt"):
        return LoginState.GOOGLE_2FA_PROMPT, details

    if state_checks.get("hasBackupCodes"):
        return LoginState.GOOGLE_2FA_BACKUP_CODES, details

    if state_checks.get("has2faChallenge"):
        details["challenge_type"] = state_checks.get("challengeType")
        return LoginState.GOOGLE_2FA_UNKNOWN, details

    # Consent/permissions screen
    if state_checks.get("hasConsentScreen"):
        return LoginState.GOOGLE_CONSENT, details

    return LoginState.UNKNOWN, details


# ---------------------------------------------------------------------------
# DOM / screenshot helpers
# ---------------------------------------------------------------------------

async def dump_dom_tree(page: Page, filename: str, max_depth: int = 8) -> str:
    """Extract a simplified DOM tree showing tag, id, class, role, aria-label, data-* attrs."""
    tree = await page.evaluate("""(maxDepth) => {
        function getAttrs(el) {
            const attrs = {};
            const dominated = ['id', 'class', 'role', 'aria-label', 'aria-labelledby',
                               'contenteditable', 'type', 'name', 'placeholder',
                               'data-testid', 'data-message-author-role', 'data-placeholder',
                               'data-challengetype', 'data-identifier', 'jsname'];
            for (const attr of dominated) {
                if (el.hasAttribute(attr)) attrs[attr] = el.getAttribute(attr);
            }
            for (const attr of el.attributes) {
                if (attr.name.startsWith('data-') && !attrs[attr.name]) {
                    attrs[attr.name] = attr.value.substring(0, 100);
                }
            }
            return attrs;
        }

        function walk(node, depth) {
            if (depth > maxDepth) return null;
            if (node.nodeType !== 1) return null;
            const tag = node.tagName.toLowerCase();
            if (['script', 'style', 'noscript', 'link', 'meta'].includes(tag)) return null;
            if (tag === 'svg') return { tag, attrs: getAttrs(node), children: [], text: '' };

            const attrs = getAttrs(node);
            const children = [];
            for (const child of node.children) {
                const c = walk(child, depth + 1);
                if (c) children.push(c);
            }
            let text = '';
            for (const child of node.childNodes) {
                if (child.nodeType === 3) {
                    const t = child.textContent.trim();
                    if (t) text += t + ' ';
                }
            }
            text = text.trim().substring(0, 200);
            return { tag, attrs, children, text };
        }

        return walk(document.body, 0);
    }""", max_depth)

    def format_tree(node, indent=0):
        if not node:
            return ""
        prefix = "  " * indent
        tag = node["tag"]
        attrs = node.get("attrs", {})
        text = node.get("text", "")
        attr_str = ""
        for k, v in attrs.items():
            attr_str += f' {k}="{v}"'
        line = f"{prefix}<{tag}{attr_str}>"
        if text:
            line += f'  "{text[:80]}"'
        lines = [line]
        for child in node.get("children", []):
            child_lines = format_tree(child, indent + 1)
            if child_lines:
                lines.append(child_lines)
        return "\n".join(lines)

    formatted = format_tree(tree)
    output_path = OUTPUT_DIR / filename
    output_path.write_text(formatted, encoding="utf-8")
    logger.info("DOM tree saved to %s (%d lines)", output_path, formatted.count("\n") + 1)
    return formatted


async def dump_focused_area(page: Page, selector: str, filename: str, label: str) -> str:
    """Dump DOM tree of a specific area matching the selector."""
    tree = await page.evaluate("""(args) => {
        const [selector, maxDepth] = args;
        const root = document.querySelector(selector);
        if (!root) return null;

        function getAttrs(el) {
            const attrs = {};
            for (const attr of el.attributes) {
                attrs[attr.name] = attr.value.substring(0, 200);
            }
            return attrs;
        }

        function walk(node, depth) {
            if (depth > maxDepth) return null;
            if (node.nodeType !== 1) return null;
            const tag = node.tagName.toLowerCase();
            if (['script', 'style', 'noscript'].includes(tag)) return null;
            if (tag === 'svg') return { tag, attrs: getAttrs(node), children: [], text: '' };
            const attrs = getAttrs(node);
            const children = [];
            for (const child of node.children) {
                const c = walk(child, depth + 1);
                if (c) children.push(c);
            }
            let text = '';
            for (const child of node.childNodes) {
                if (child.nodeType === 3) {
                    const t = child.textContent.trim();
                    if (t) text += t + ' ';
                }
            }
            text = text.trim().substring(0, 200);
            return { tag, attrs, children, text };
        }

        return walk(root, 0);
    }""", [selector, 12])

    if not tree:
        logger.warning("No element found for selector: %s", selector)
        return f"NOT FOUND: {selector}"

    def format_tree(node, indent=0):
        if not node:
            return ""
        prefix = "  " * indent
        tag = node["tag"]
        attrs = node.get("attrs", {})
        text = node.get("text", "")
        attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        if attr_str:
            attr_str = " " + attr_str
        line = f"{prefix}<{tag}{attr_str}>"
        if text:
            line += f'  "{text[:100]}"'
        lines = [line]
        for child in node.get("children", []):
            child_lines = format_tree(child, indent + 1)
            if child_lines:
                lines.append(child_lines)
        return "\n".join(lines)

    formatted = f"=== {label} ({selector}) ===\n\n{format_tree(tree)}"
    output_path = OUTPUT_DIR / filename
    output_path.write_text(formatted, encoding="utf-8")
    logger.info("Focused DOM (%s) saved to %s", label, output_path)
    return formatted


async def take_screenshot(page: Page, name: str) -> Path:
    """Take a screenshot and save it."""
    path = OUTPUT_DIR / f"{name}.png"
    await page.screenshot(path=str(path), full_page=False)
    logger.info("Screenshot saved to %s", path)
    return path


async def find_all_interactive(page: Page, filename: str) -> str:
    """Find all interactive elements: buttons, inputs, textareas, contenteditable."""
    elements = await page.evaluate("""() => {
        const results = [];
        const selectors = [
            'button', 'input', 'textarea', 'select',
            '[contenteditable]', '[role="button"]', '[role="textbox"]',
            '[role="tab"]', '[role="menuitem"]', '[role="option"]',
            'a[href]'
        ];

        for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;

                const info = {
                    tag: el.tagName.toLowerCase(),
                    selector: sel,
                    id: el.id || null,
                    class: el.className ? (typeof el.className === 'string' ? el.className.substring(0, 200) : '') : null,
                    type: el.type || null,
                    role: el.getAttribute('role'),
                    ariaLabel: el.getAttribute('aria-label'),
                    placeholder: el.getAttribute('placeholder'),
                    contenteditable: el.getAttribute('contenteditable'),
                    text: el.textContent.trim().substring(0, 100),
                    visible: rect.width > 0 && rect.height > 0,
                    rect: { x: Math.round(rect.x), y: Math.round(rect.y),
                            w: Math.round(rect.width), h: Math.round(rect.height) },
                };

                for (const attr of el.attributes) {
                    if (attr.name.startsWith('data-')) {
                        info['data_' + attr.name.substring(5)] = attr.value.substring(0, 100);
                    }
                }
                results.push(info);
            }
        }
        return results;
    }""")

    lines = [f"=== Interactive Elements ({len(elements)} found) ===\n"]
    for el in elements:
        parts = [f"  <{el['tag']}>"]
        if el.get('id'):
            parts.append(f"id={el['id']}")
        if el.get('ariaLabel'):
            parts.append(f'aria-label="{el["ariaLabel"]}"')
        if el.get('role'):
            parts.append(f'role="{el["role"]}"')
        if el.get('type'):
            parts.append(f'type="{el["type"]}"')
        if el.get('placeholder'):
            parts.append(f'placeholder="{el["placeholder"]}"')
        if el.get('contenteditable'):
            parts.append(f'contenteditable="{el["contenteditable"]}"')
        if el.get('text') and len(el['text']) > 0:
            parts.append(f'text="{el["text"][:60]}"')
        if el.get('class'):
            parts.append(f'class="{el["class"][:100]}"')
        for key, val in el.items():
            if key.startswith('data_'):
                parts.append(f'{key.replace("_", "-")}="{val}"')
        r = el.get('rect', {})
        parts.append(f"[{r.get('x',0)},{r.get('y',0)} {r.get('w',0)}x{r.get('h',0)}]")
        lines.append(" ".join(parts))

    formatted = "\n".join(lines)
    output_path = OUTPUT_DIR / filename
    output_path.write_text(formatted, encoding="utf-8")
    logger.info("Interactive elements saved to %s (%d elements)", output_path, len(elements))
    return formatted


# ---------------------------------------------------------------------------
# Browser launch
# ---------------------------------------------------------------------------

async def launch_browser() -> tuple:
    """Launch Chrome with persistent profile."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = PROFILE_DIR / lock_file
        if lock_path.exists():
            lock_path.unlink(missing_ok=True)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        permissions=["clipboard-read", "clipboard-write"],
        viewport={"width": 1280, "height": 900},
        ignore_default_args=["--enable-automation", "--no-sandbox"],
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
        ],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return pw, context, page


# ---------------------------------------------------------------------------
# Analysis steps
# ---------------------------------------------------------------------------

async def step_login(page: Page) -> bool:
    """Track the complete login flow with state detection at each stage.

    Navigates to Gemini, detects the current auth state, takes screenshot+DOM
    at each transition. Waits for user to complete manual login/2FA steps.

    Returns True if login succeeded, False on timeout.
    """
    logger.info("=" * 60)
    logger.info("STEP: Login Flow Analysis")
    logger.info("=" * 60)

    # Navigate to Gemini
    logger.info("Navigating to %s ...", GEMINI_URL)
    try:
        await page.goto(GEMINI_URL, timeout=30000, wait_until="commit")
    except Exception as e:
        logger.error("Navigation failed: %s", e)
        await take_screenshot(page, "login_00_nav_failed")
        return False

    await page.wait_for_timeout(3000)

    # Track state transitions
    previous_state = None
    state_counter = 0
    login_log = []
    max_wait_s = 300  # 5 minutes total
    elapsed_s = 0
    poll_interval_s = 2

    while elapsed_s < max_wait_s:
        state, details = await detect_login_state(page)

        if state != previous_state:
            state_counter += 1
            prefix = f"login_{state_counter:02d}_{state}"

            logger.info("-" * 40)
            logger.info("STATE TRANSITION: %s → %s", previous_state or "START", state)
            logger.info("  URL: %s", details.get("url", ""))
            logger.info("  Title: %s", details.get("title", ""))

            # Detailed logging for auth states
            checks = details.get("checks", {})
            if checks:
                if checks.get("headings"):
                    logger.info("  Headings: %s", checks["headings"])
                if checks.get("visibleInputs"):
                    logger.info("  Visible inputs: %s",
                                [f"{i['type']}({i.get('name','')}/{i.get('id','')})"
                                 for i in checks["visibleInputs"]])
                if checks.get("visibleButtons"):
                    logger.info("  Visible buttons: %s",
                                [b['text'][:40] for b in checks["visibleButtons"][:8]])
                if checks.get("challengeType"):
                    logger.info("  Challenge type: %s", checks["challengeType"])

            # Take screenshot + DOM dump for this state
            await take_screenshot(page, prefix)
            await dump_dom_tree(page, f"{prefix}_dom.txt")
            await find_all_interactive(page, f"{prefix}_interactive.txt")

            login_log.append({
                "step": state_counter,
                "state": state,
                "url": details.get("url", ""),
                "title": details.get("title", ""),
                "elapsed_s": elapsed_s,
            })

            previous_state = state

            # If we reached Gemini ready state, we're done
            if state == LoginState.GEMINI_READY:
                logger.info("=" * 40)
                logger.info("LOGIN COMPLETE — Gemini is ready!")
                logger.info("Total login states observed: %d", state_counter)
                break

            # Log specific guidance
            if state == LoginState.GOOGLE_EMAIL:
                logger.info("  → Waiting for email entry...")
            elif state == LoginState.GOOGLE_PASSWORD:
                logger.info("  → Waiting for password entry...")
            elif state == LoginState.GOOGLE_ACCOUNT_CHOOSER:
                logger.info("  → Waiting for account selection...")
            elif state == LoginState.GOOGLE_2FA_PROMPT:
                logger.info("  → Waiting for phone tap confirmation (2FA)...")
            elif state == LoginState.GOOGLE_2FA_AUTHENTICATOR:
                logger.info("  → Waiting for authenticator code (2FA)...")
            elif state == LoginState.GOOGLE_2FA_SMS:
                logger.info("  → Waiting for SMS code (2FA)...")
            elif state == LoginState.GOOGLE_2FA_SECURITY_KEY:
                logger.info("  → Waiting for security key tap (2FA)...")
            elif state == LoginState.GOOGLE_2FA_UNKNOWN:
                logger.info("  → Unknown 2FA challenge (type: %s). Manual action needed.",
                            details.get("checks", {}).get("challengeType", "?"))
            elif state == LoginState.GOOGLE_CAPTCHA:
                logger.info("  → CAPTCHA detected! Manual solving needed.")
            elif state == LoginState.GOOGLE_CONSENT:
                logger.info("  → Consent/permissions screen. Manual action needed.")
            elif state == LoginState.GEMINI_TERMS:
                logger.info("  → Gemini terms/welcome screen. Button: %s",
                            details.get("terms_button", "?"))
            elif state == LoginState.GEMINI_LOADING:
                logger.info("  → Gemini page loading...")
            elif state == LoginState.ALREADY_LOGGED_IN:
                break

        await page.wait_for_timeout(poll_interval_s * 1000)
        elapsed_s += poll_interval_s

    # Write login flow summary
    summary_lines = ["=== Login Flow Summary ===\n"]
    for entry in login_log:
        summary_lines.append(
            f"  Step {entry['step']}: {entry['state']} "
            f"(+{entry['elapsed_s']}s) URL: {entry['url']}"
        )
    summary_lines.append(f"\nTotal time: {elapsed_s}s")
    summary_lines.append(f"Total states: {state_counter}")
    summary_lines.append(f"Final state: {previous_state}")

    summary = "\n".join(summary_lines)
    (OUTPUT_DIR / "login_summary.txt").write_text(summary, encoding="utf-8")
    logger.info("\n%s", summary)

    is_ready = previous_state in (LoginState.GEMINI_READY, LoginState.ALREADY_LOGGED_IN)
    if not is_ready and elapsed_s >= max_wait_s:
        logger.error("Login timeout after %ds. Last state: %s", elapsed_s, previous_state)

    return is_ready


async def step_landing(page: Page):
    """Analyze the Gemini landing/chat page."""
    logger.info("=" * 60)
    logger.info("STEP: Landing Page Analysis")
    logger.info("=" * 60)

    await page.wait_for_timeout(3000)
    logger.info("Current URL: %s", page.url)

    await take_screenshot(page, "01_landing")
    await dump_dom_tree(page, "01_landing_dom.txt")
    await find_all_interactive(page, "01_landing_interactive.txt")

    logger.info("Landing page analysis complete.")


async def step_chat(page: Page):
    """Analyze the chat input area in detail."""
    logger.info("=" * 60)
    logger.info("STEP: Chat Input Area Analysis")
    logger.info("=" * 60)

    await page.wait_for_timeout(2000)

    # Broad search for input-like elements
    input_candidates = [
        "rich-textarea",
        ".ql-editor",
        'div[contenteditable="true"]',
        "textarea",
        '.input-area-container',
        '.text-input-field',
        'div[role="textbox"]',
        '.prompt-textarea',
        '#prompt-textarea',
        'p[data-placeholder]',
    ]

    found_inputs = []
    for selector in input_candidates:
        el = await page.query_selector(selector)
        if el:
            visible = await el.is_visible()
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            outer = await el.evaluate("el => el.outerHTML.substring(0, 300)")
            found_inputs.append(f"  FOUND: {selector} (visible={visible}, tag={tag})")
            found_inputs.append(f"         HTML: {outer[:200]}")
        else:
            found_inputs.append(f"  NOT FOUND: {selector}")

    input_report = "\n".join(found_inputs)
    logger.info("Input element scan:\n%s", input_report)

    # Send button candidates
    send_candidates = [
        'button[aria-label="Send message"]',
        'button[aria-label="Nachricht senden"]',
        'button.send-button',
        '.send-button',
        'button[data-testid="send-button"]',
        'button[aria-label="Send"]',
        'button[aria-label="Senden"]',
    ]

    found_sends = []
    for selector in send_candidates:
        el = await page.query_selector(selector)
        if el:
            visible = await el.is_visible()
            outer = await el.evaluate("el => el.outerHTML.substring(0, 300)")
            found_sends.append(f"  FOUND: {selector} (visible={visible})")
            found_sends.append(f"         HTML: {outer[:200]}")
        else:
            found_sends.append(f"  NOT FOUND: {selector}")

    send_report = "\n".join(found_sends)
    logger.info("Send button scan:\n%s", send_report)

    # File upload input
    file_inputs = await page.query_selector_all('input[type="file"]')
    logger.info("File inputs found: %d", len(file_inputs))

    await take_screenshot(page, "02_chat_input")

    # Focused dump on whichever input area we found
    for selector in input_candidates:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await dump_focused_area(page, selector, "02_chat_input_focused.txt", f"Input: {selector}")
            break

    # Write combined report
    report = f"=== Chat Input Report ===\n\nInput Elements:\n{input_report}\n\nSend Buttons:\n{send_report}\n\nFile Inputs: {len(file_inputs)}"
    (OUTPUT_DIR / "02_chat_report.txt").write_text(report, encoding="utf-8")

    logger.info("Chat input analysis complete.")


async def step_response(page: Page):
    """Send a test message and analyze the response structure."""
    logger.info("=" * 60)
    logger.info("STEP: Response Analysis (sending test message)")
    logger.info("=" * 60)

    # Find textarea
    textarea = None
    textarea_sel = None
    for selector in ["rich-textarea", 'div[contenteditable="true"]', "textarea",
                     'div[role="textbox"]', 'p[data-placeholder]']:
        textarea = await page.query_selector(selector)
        if textarea and await textarea.is_visible():
            textarea_sel = selector
            logger.info("Using textarea: %s", selector)
            break
        textarea = None

    if not textarea:
        logger.error("Could not find textarea! Taking diagnostic screenshot.")
        await take_screenshot(page, "03_no_textarea")
        await dump_dom_tree(page, "03_no_textarea_dom.txt", max_depth=10)
        return

    # Type test message
    test_message = "Say exactly: GEMINI_TEST_OK"
    await textarea.click()
    await page.wait_for_timeout(300)
    await page.keyboard.type(test_message, delay=30)
    await page.wait_for_timeout(500)
    await take_screenshot(page, "03_message_typed")

    # Send
    await page.keyboard.press("Enter")
    logger.info("Message sent, waiting for response...")
    await page.wait_for_timeout(2000)
    await take_screenshot(page, "03_waiting_response")

    # Wait for response (up to 60 seconds)
    for i in range(60):
        await page.wait_for_timeout(1000)

        # Check for generating indicators
        stop_candidates = [
            'button[aria-label="Stop generating"]',
            'button[aria-label="Antwort stoppen"]',
            'button[aria-label="Stop"]',
            '.stop-button',
        ]
        still_generating = False
        for sel in stop_candidates:
            el = await page.query_selector(sel)
            if el:
                try:
                    if await el.is_visible():
                        still_generating = True
                        break
                except Exception:
                    pass

        if not still_generating and i > 3:
            logger.info("Response appears complete (no stop button visible, %ds)", i)
            break

        if i % 5 == 0:
            logger.info("Still waiting... (%ds)", i)

    await page.wait_for_timeout(2000)
    await take_screenshot(page, "04_response_received")
    await dump_dom_tree(page, "04_response_dom.txt", max_depth=10)
    await find_all_interactive(page, "04_response_interactive.txt")

    # Search for response containers with broader selectors
    response_candidates = [
        '.model-response-text', '.response-container', 'model-response',
        'message-content', '.conversation-turn', '.turn-container',
        '[data-message-author-role="model"]', '.markdown-main-panel',
        '.response-content', '.message-body', '.chat-message',
    ]

    found_responses = []
    for selector in response_candidates:
        els = await page.query_selector_all(selector)
        if els:
            count = len(els)
            found_responses.append(f"  FOUND: {selector} ({count} elements)")
            # Dump the last one (most recent response)
            safe_name = selector.replace('.', '_').replace('[', '_').replace(']', '').replace('"', '').replace('=', '_')
            await dump_focused_area(page, selector, f"04_resp_{safe_name}.txt", f"Response: {selector}")

    if found_responses:
        logger.info("Response containers found:\n%s", "\n".join(found_responses))
    else:
        logger.warning("No known response containers found! Full DOM dumped for manual inspection.")

    logger.info("Response analysis complete.")


async def step_copy(page: Page):
    """Analyze copy/action buttons on the response."""
    logger.info("=" * 60)
    logger.info("STEP: Copy Button / Action Analysis")
    logger.info("=" * 60)

    await page.wait_for_timeout(1000)

    # Try hovering over response areas
    response_candidates = [
        '.model-response-text', 'model-response', '.response-container',
        '.conversation-turn:last-child', '.turn-container:last-child',
        '.message-body:last-child',
    ]

    for selector in response_candidates:
        els = await page.query_selector_all(selector)
        if els:
            logger.info("Found %d elements for %s, hovering last...", len(els), selector)
            await els[-1].hover()
            await page.wait_for_timeout(1500)
            await take_screenshot(page, "05_hover_response")
            break

    # Copy button scan
    copy_candidates = [
        'button[aria-label="Copy"]',
        'button[aria-label="Kopieren"]',
        'button[aria-label="Copy response"]',
        'button[aria-label="Copy to clipboard"]',
        'button[aria-label="In die Zwischenablage kopieren"]',
        '.copy-button',
        'button[data-testid="copy-button"]',
    ]

    for selector in copy_candidates:
        el = await page.query_selector(selector)
        if el:
            visible = await el.is_visible()
            logger.info("Copy button FOUND: %s (visible=%s)", selector, visible)
        else:
            logger.info("Copy button NOT FOUND: %s", selector)

    # Full button scan
    buttons = await page.evaluate("""() => {
        const results = [];
        for (const btn of document.querySelectorAll('button')) {
            const rect = btn.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            results.push({
                ariaLabel: btn.getAttribute('aria-label'),
                title: btn.getAttribute('title'),
                text: btn.textContent.trim().substring(0, 80),
                class: typeof btn.className === 'string' ? btn.className.substring(0, 150) : '',
                dataTestid: btn.getAttribute('data-testid'),
                jsname: btn.getAttribute('jsname'),
                rect: { x: Math.round(rect.x), y: Math.round(rect.y),
                        w: Math.round(rect.width), h: Math.round(rect.height) }
            });
        }
        return results;
    }""")

    button_report = "\n".join(
        f"  <button> aria-label={b.get('ariaLabel')} title={b.get('title')} "
        f"jsname={b.get('jsname')} text=\"{b.get('text', '')[:40]}\" "
        f"class=\"{b.get('class', '')[:60]}\" data-testid={b.get('dataTestid')} "
        f"[{b['rect']['x']},{b['rect']['y']} {b['rect']['w']}x{b['rect']['h']}]"
        for b in buttons
    )
    logger.info("All visible buttons:\n%s", button_report)

    (OUTPUT_DIR / "05_buttons_report.txt").write_text(button_report, encoding="utf-8")
    await find_all_interactive(page, "05_copy_interactive.txt")

    logger.info("Copy button analysis complete.")


async def step_sidebar(page: Page):
    """Analyze sidebar/navigation structure."""
    logger.info("=" * 60)
    logger.info("STEP: Sidebar / Navigation Analysis")
    logger.info("=" * 60)

    await page.wait_for_timeout(1000)

    nav_candidates = ['nav', '[role="navigation"]', '.sidebar', '.side-nav', 'aside']

    for selector in nav_candidates:
        el = await page.query_selector(selector)
        if el:
            safe = selector.replace('.', '_').replace('[', '_').replace(']', '').replace('"', '').replace('=', '_')
            await dump_focused_area(page, selector, f"06_sidebar_{safe}.txt", f"Sidebar: {selector}")
            logger.info("Sidebar element found: %s", selector)

    await take_screenshot(page, "06_sidebar")
    logger.info("Sidebar analysis complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Gemini UI Analyzer")
    parser.add_argument(
        "--step",
        choices=["login", "landing", "chat", "response", "copy", "sidebar", "all"],
        default="all",
        help="Which analysis step to run",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Starting Gemini UI Analyzer...")
    logger.info("Output directory: %s", OUTPUT_DIR)
    logger.info("Profile directory: %s", PROFILE_DIR)

    pw, context, page = await launch_browser()

    try:
        # Login step always runs first (unless skipped)
        if args.step in ("login", "all"):
            logged_in = await step_login(page)
            if not logged_in:
                logger.error("Login did not complete. Dumping final state for analysis.")
                await take_screenshot(page, "login_final_failed")
                await dump_dom_tree(page, "login_final_failed_dom.txt")
                if args.step == "login":
                    return  # Only login requested, stop here
                # For "all", continue anyway — user might want to see the state
        else:
            # For non-login steps, just navigate and check
            await page.goto(GEMINI_URL, timeout=30000, wait_until="commit")
            await page.wait_for_timeout(5000)

        # Run requested steps
        steps = {
            "landing": step_landing,
            "chat": step_chat,
            "response": step_response,
            "copy": step_copy,
            "sidebar": step_sidebar,
        }

        if args.step == "all":
            for name, func in steps.items():
                try:
                    await func(page)
                except Exception as e:
                    logger.error("Step '%s' failed: %s", name, e, exc_info=True)
                    await take_screenshot(page, f"error_{name}")
        elif args.step != "login":
            await steps[args.step](page)

        logger.info("=" * 60)
        logger.info("ANALYSIS COMPLETE")
        logger.info("Output files in: %s", OUTPUT_DIR)
        logger.info("=" * 60)

        # Keep browser open for manual inspection
        logger.info("Browser stays open for manual inspection. Press Ctrl+C to close.")
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Closing browser...")
    finally:
        await context.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
