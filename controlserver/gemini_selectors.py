"""
Central selectors for Google Gemini Web UI elements.

Each key maps to a list of CSS selector candidates (tried in order via comma-join).
Update these when Gemini changes its frontend.

Verified against live Gemini UI (gemini.google.com) — February 2026.
Key facts:
  - Angular-based SPA with custom elements (model-response, rich-textarea, etc.)
  - Quill.js editor (ql-editor class) for the input textarea
  - data-test-id attributes (note: hyphenated, NOT data-testid like ChatGPT)
  - Send button hidden until text is entered
  - No Cloudflare; Google has its own bot detection
  - Copy button always visible in response footer (no hover needed)
"""

GEMINI_URL = "https://gemini.google.com/app"

# ---------------------------------------------------------------------------
# Login & session selectors (used directly, not via SELECTORS dict)
# ---------------------------------------------------------------------------

# Cookie consent banner "Accept all" button (consent.google.com)
COOKIE_ACCEPT_BTN = (
    'button:has-text("Alle akzeptieren"), '
    'button:has-text("Accept all"), '
    'button:has-text("Alle annehmen")'
)

# Indicators that the user is NOT logged in.
# When not logged in, Gemini shows a zero-state landing page or a sign-in button.
# The body element gets the class "zero-state-theme" in the logged-out state.
NOT_LOGGED_IN_INDICATORS = (
    'button.sign-in-button, '
    'button:has-text("Anmelden"), '
    'button:has-text("Sign in"), '
    'a:has-text("Sign in"), '
    'a:has-text("Anmelden")'
)

# Elements visible when truly logged in (Gemini app loaded with session).
# The Google account avatar link is the strongest indicator.
LOGGED_IN_INDICATORS = (
    'a[aria-label*="Google-Konto:"], '
    'a[aria-label*="Google Account:"], '
    'rich-textarea, '
    '.ql-editor[contenteditable="true"]'
)

# ---------------------------------------------------------------------------
# Error & recovery selectors
# ---------------------------------------------------------------------------

# Gemini error dialogs — try to auto-dismiss retry buttons
GEMINI_ERROR_DIALOGS = (
    'button:has-text("Try again"), '
    'button:has-text("Erneut versuchen"), '
    'button:has-text("Retry"), '
    'div:has-text("Something went wrong"), '
    'div:has-text("Es ist ein Fehler aufgetreten")'
)

# Session expired: redirected to Google login or sign-in button reappears
SESSION_EXPIRED_INDICATORS = (
    'button.sign-in-button, '
    'button:has-text("Sign in"), '
    'button:has-text("Anmelden")'
)

# Google bot detection (no Cloudflare, but Google has its own)
GOOGLE_BOT_DETECTION = (
    'div:has-text("unusual traffic"), '
    'div:has-text("ungewöhnlichen Datenverkehr")'
)

# ---------------------------------------------------------------------------
# Chat interaction selectors (used via find_element)
# ---------------------------------------------------------------------------

SELECTORS = {
    # The Quill.js editor inside rich-textarea.
    # Target the inner .ql-editor div for actual text interaction.
    "prompt_textarea": [
        ".ql-editor.textarea",
        'div[role="textbox"][contenteditable="true"]',
        ".ql-editor",
        "rich-textarea",
    ],
    # Send button — hidden when textarea is empty, becomes visible after text input.
    # Has class "send-button submit" on the button element.
    "send_button": [
        "button.send-button",
        'button[aria-label="Nachricht senden"]',
        'button[aria-label="Send message"]',
    ],
    # Stop/cancel button during generation.
    # Most reliable: mat-icon with data-mat-icon-name="stop" inside a button.
    # aria-label varies by language, so we prefer the icon attribute.
    "stop_button": [
        '[data-mat-icon-name="stop"]',
        'button:has([data-mat-icon-name="stop"])',
        'button[aria-label="Stop generating"]',
        'button[aria-label="Generierung stoppen"]',
        'button[aria-label="Antwort stoppen"]',
        "button.stop-button",
    ],
    # Copy button in the response footer (always visible, no hover needed).
    # Has stable data-test-id="copy-button".
    "copy_button": [
        'button[data-test-id="copy-button"]',
        'button[aria-label="Kopieren"]',
        'button[aria-label="Copy"]',
    ],
    # File upload — two-step process:
    # Step 1: Click the upload button at the bottom to open the flyout menu.
    "add_button": [
        '[aria-controls="upload-file-menu"]',
        'div.file-uploader button',
    ],
    # Step 2: In the flyout, click the local file uploader to trigger file dialog.
    "file_upload_button": [
        '[data-test-id="local-images-files-uploader-button"]',
        'button[data-test-id="local-images-files-uploader-button"]',
    ],
    # Model selector button (shows current model: "Pro", "Flash", etc.)
    "model_selector": [
        'button[data-test-id="bard-mode-menu-button"]',
        'button[aria-label="Modusauswahl öffnen"]',
    ],
    # Model menu items (appear after clicking model_selector)
    "model_menu_item": [
        'button.mat-mdc-menu-item',
        'mat-option',
        'div[role="menuitem"]',
        'button[role="menuitem"]',
    ],
    # New chat link (Gemini logo or sidebar button)
    "new_chat": [
        'a[aria-label="Neuer Chat"]',
        'a[aria-label="New chat"]',
        'side-nav-action-button[data-test-id="new-chat-button"] a',
    ],
}

# Pre-built combined selectors for query_selector_all calls
COPY_BUTTON_ALL = ", ".join(SELECTORS["copy_button"])
STOP_BUTTON_ALL = ", ".join(SELECTORS["stop_button"])

# ---------------------------------------------------------------------------
# Response structure selectors
# ---------------------------------------------------------------------------

# Each model response is a <model-response> custom element.
# This is the primary selector for counting and iterating responses.
MODEL_RESPONSE = "model-response"

# The actual rendered markdown text within a response.
# Structure: model-response > response-container > .model-response-text > message-content > .markdown
RESPONSE_TEXT = ".markdown.markdown-main-panel"

# The response container wrapping the content + actions
RESPONSE_CONTAINER = ".response-container"

# Generation-in-progress detection.
# The markdown div has aria-busy="true" during generation, "false" when done.
GENERATION_BUSY = '.markdown.markdown-main-panel[aria-busy="true"]'
GENERATION_DONE = '.markdown.markdown-main-panel[aria-busy="false"]'

# Hidden heading that appears with each response ("Gemini hat gesagt" / "Gemini said")
RESPONSE_HEADING = "h2.cdk-visually-hidden"

# ---------------------------------------------------------------------------
# Enterprise / Premium detection selectors
# ---------------------------------------------------------------------------

# Enterprise account indicators (confirmed in DOM analysis):
# - rich-textarea has class "enterprise"
# - enterprise-indicator-logo-container in the top bar
# - enterprise-display div in the right section
ENTERPRISE_INDICATORS = (
    'rich-textarea.enterprise, '
    '.enterprise-indicator-logo-container, '
    '.enterprise-display'
)

# Free/anonymous Gemini indicators:
# - Body has "zero-state-theme" class
# - No enterprise class on rich-textarea
FREE_GEMINI_INDICATORS = "body.zero-state-theme"

# ---------------------------------------------------------------------------
# Gem navigation selectors
# ---------------------------------------------------------------------------

# Gem sidebar buttons (for "claude-code-sparring" etc.)
GEM_SIDEBAR_BUTTON = 'button.bot-new-conversation-butt'


async def find_element(page, key, timeout=10000):
    """Find a UI element using combined CSS selectors for the given key.

    Tries all selector candidates simultaneously via CSS comma-join.
    Returns the first matching element or raises RuntimeError.
    """
    candidates = SELECTORS.get(key)
    if not candidates:
        raise ValueError(f"Unknown selector key: {key}")
    combined = ", ".join(candidates)
    try:
        return await page.wait_for_selector(combined, timeout=timeout)
    except Exception:
        raise RuntimeError(f"Element '{key}' not found. Tried: {combined}")
