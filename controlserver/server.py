"""Gemini Session Pool Service — FastAPI REST server.

Manages a pool of Gemini browser tabs and exposes them via REST API
for parallel use by multiple Claude Code instances and sub-agents.

Usage:
    python server.py
    python server.py --config /path/to/config.yaml
"""

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, model_validator

from browser import PoolBrowser
from config import AppConfig, load_config
from pool import SlotPool, SlotAcquired, Queued, Rejected
from slot import Slot, LeaseExpiredError, InvalidTokenError

# ---------------------------------------------------------------------------
# Globals (initialized during lifespan)
# ---------------------------------------------------------------------------

pool: SlotPool | None = None
config: AppConfig | None = None

logger = logging.getLogger("session-pool")


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class AcquireRequest(BaseModel):
    """Request body for POST /api/session/acquire."""

    owner: str


class SendRequest(BaseModel):
    """Request body for POST /api/session/{slot_id}/send.

    File handling:
      - merge_paths: Text file paths whose contents are merged and embedded
        directly into the message text. No limit on count. The files are
        read as UTF-8 text and prepended to the message with filename headers.
        NOT uploaded as files — content goes into the prompt.
      - file_paths: Binary files uploaded individually via browser (images,
        PDFs, etc.). Maximum 9 per call.
    """

    message: str
    merge_paths: list[str] = []
    file_paths: list[str] = []

    @model_validator(mode="after")
    def validate_upload_limit(self) -> "SendRequest":
        """Enforce max 9 binary file uploads per turn.

        merge_paths are embedded in the message (no upload limit).
        file_paths are uploaded individually (max 9).
        """
        if len(self.file_paths) > 9:
            raise ValueError(
                f"Maximum 9 file uploads per turn "
                f"(got {len(self.file_paths)})"
            )
        return self


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(app_config: AppConfig) -> None:
    """Configure logging with rotating file handler and stderr output."""
    log_dir = Path(os.path.expanduser(app_config.logging.dir))
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "session-pool.log"
    max_bytes = app_config.logging.max_file_size_mb * 1024 * 1024

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=app_config.logging.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, app_config.logging.error_level.upper()))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(getattr(logging, app_config.logging.level.upper()))

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    file_handler.setFormatter(formatter)
    stderr_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stderr_handler)


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, launch browser, warm up slots, start monitors.

    Shutdown: stop monitors, close browser.
    """
    global pool, config

    # Load configuration
    config_path = os.environ.get("POOL_CONFIG", "config.yaml")
    config = load_config(config_path)
    _setup_logging(config)

    logger.info("Loading config from %s", config_path)
    logger.info(
        "Pool: %d slots, inactivity=%ds, queue_max=%d",
        config.pool.size, config.pool.inactivity_timeout_s, config.pool.max_queue_depth,
    )

    # Launch browser
    browser = PoolBrowser(config.browser)
    logger.info("Starting Chrome (headless=%s)...", config.browser.headless)
    await browser.start()

    # Warm up slots
    slots = []
    logger.info("Warming up %d slots...", config.pool.size)
    logger.info("Gem URL: %s", config.browser.gem_url)
    logger.info("Preferred model: %s", config.browser.preferred_model)

    # Step 1: Navigate to base Gemini app for login check.
    # We use the app URL first because the login flow (email → password → 2FA)
    # works more reliably on the main app page than on a Gem URL.
    first_page = None
    if browser._initial_page is not None:
        first_page = browser._initial_page
        browser._initial_page = None
        await browser._stealth.apply_stealth_async(first_page)
    else:
        first_page = await browser._context.new_page()
        await browser._stealth.apply_stealth_async(first_page)

    await browser._navigate_for_login(first_page)
    await browser._dismiss_cookie_consent(first_page)

    # Step 2: Check if logged in with enterprise/premium account
    logged_in = await browser.is_logged_in(first_page)
    if not logged_in:
        logger.warning("Not logged in! Browser window is open.")
        logger.warning(
            "Please log in manually (Google SSO: Email → Password → 2FA)..."
        )
        try:
            login_ok = await browser.wait_for_login(first_page)
        except Exception as exc:
            logger.error("wait_for_login crashed: %s", exc, exc_info=True)
            login_ok = False
        if not login_ok:
            logger.error(
                "Login not detected within timeout. "
                "Service starting anyway — send requests will fail until login."
            )
        else:
            logged_in = True
            logger.info("Login detected.")
            is_enterprise = await browser.is_enterprise(first_page)
            logger.info("Enterprise account: %s", is_enterprise)
    else:
        is_enterprise = await browser.is_enterprise(first_page)
        logger.info("Already logged in. Enterprise: %s", is_enterprise)

    # Step 3: Navigate first page to Gem URL and set model
    logger.info("  Slot 0: navigating to Gem...")
    try:
        await first_page.goto(
            config.browser.gem_url,
            timeout=config.browser.navigation_timeout_ms,
            wait_until="commit",
        )
        await first_page.wait_for_selector(
            'rich-textarea, .ql-editor[contenteditable="true"]',
            timeout=config.browser.navigation_timeout_ms,
        )
        await first_page.wait_for_timeout(1000)
        await browser._ensure_preferred_model(first_page)
    except Exception as exc:
        logger.warning("  Slot 0: Gem navigation failed: %s", exc)

    slots.append(Slot(0, first_page, config.browser))
    logger.info("  Slot 0: Gem loaded, login %s", "OK" if logged_in else "PENDING")

    # Remaining slots
    for slot_id in range(1, config.pool.size):
        try:
            page = await browser.create_slot_page()
            slots.append(Slot(slot_id, page, config.browser))
            logger.info("  Slot %d: gemini.google.com loaded", slot_id)
        except Exception as exc:
            logger.error("  Slot %d: warmup failed: %s", slot_id, exc)
            # Create a placeholder slot in ERROR state
            # We need a page, even a broken one — create and mark error
            try:
                page = await browser.create_slot_page()
            except Exception:
                # Last resort: create a new page without navigation
                page = await browser._context.new_page()
            slot = Slot(slot_id, page, config.browser)
            slot.mark_error()
            slots.append(slot)

    # Create pool
    pool = SlotPool(slots, config.pool, config.health, config.browser, browser)
    pool.start_monitors()

    free_count = sum(1 for s in slots if s.state.value == "FREE")
    logger.info("Pool ready: %d slots available", free_count)
    logger.info(
        "REST API listening on http://%s:%d",
        config.server.host, config.server.port,
    )

    yield

    # Shutdown
    logger.info("Shutting down...")
    if pool:
        await pool.shutdown()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Gemini Session Pool Service",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(LeaseExpiredError)
async def lease_expired_handler(request: Request, exc: LeaseExpiredError):
    return JSONResponse(status_code=410, content={"error": "lease_expired", "detail": str(exc)})


@app.exception_handler(InvalidTokenError)
async def invalid_token_handler(request: Request, exc: InvalidTokenError):
    return JSONResponse(status_code=403, content={"error": "invalid_token", "detail": str(exc)})


@app.exception_handler(KeyError)
async def key_error_handler(request: Request, exc: KeyError):
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/session/acquire")
async def acquire_session(body: AcquireRequest):
    """Acquire a Gemini session slot.

    Returns 200 with slot assignment, 202 with queue position,
    or 503 if pool is exhausted.
    """
    result = pool.acquire(body.owner)

    if isinstance(result, SlotAcquired):
        return JSONResponse(status_code=200, content={
            "status": result.status,
            "slot_id": result.slot_id,
            "lease_token": result.lease_token,
            "reattached": result.reattached,
            "expires_after_inactive_s": result.expires_after_inactive_s,
        })
    elif isinstance(result, Queued):
        return JSONResponse(status_code=202, content={
            "status": result.status,
            "queue_position": result.queue_position,
            "estimated_wait_s": result.estimated_wait_s,
        })
    else:
        return JSONResponse(status_code=503, content={
            "status": result.status,
            "error": result.error,
            "total_slots": result.total_slots,
            "queue_depth": result.queue_depth,
            "queue_max": result.queue_max,
        })


@app.post("/api/session/{slot_id}/send")
async def send_message(
    slot_id: int,
    body: SendRequest,
    x_lease_token: str = Header(..., alias="X-Lease-Token"),
):
    """Send a message to Gemini on the given slot.

    Requires the lease token in X-Lease-Token header.
    Blocks until Gemini responds (up to configured timeout).

    File handling:
      - merge_paths: text files are read, merged, and embedded in the message
      - file_paths: binary files are uploaded individually via browser
    """
    try:
        # Validate all file paths exist
        all_paths = body.merge_paths + body.file_paths
        for file_path in all_paths:
            if not Path(file_path).exists():
                raise HTTPException(
                    status_code=400,
                    detail=f"File not found: {file_path}",
                )

        # Build the final message: merge text files into the prompt
        message = body.message
        if body.merge_paths:
            merged_content = _merge_text_content(body.merge_paths)
            message = f"{merged_content}\n\n{body.message}"

        # Binary files are uploaded individually via browser
        upload_paths = body.file_paths if body.file_paths else None

        response_text, response_format, duration_ms = await pool.send(
            slot_id,
            x_lease_token,
            message,
            upload_paths,
        )
        return {
            "response": response_text,
            "duration_ms": duration_ms,
            "format": response_format,
        }
    except (LeaseExpiredError, InvalidTokenError, KeyError):
        raise
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.error("Send failed on slot %d: %s", slot_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Browser error: {exc}")


@app.post("/api/session/{slot_id}/release")
async def release_session(
    slot_id: int,
    x_lease_token: str = Header(..., alias="X-Lease-Token"),
):
    """Release a Gemini session slot.

    Requires the lease token in X-Lease-Token header.
    The slot is freed and the next queued client (if any) gets assigned.
    """
    pool.release(slot_id, x_lease_token)

    # Navigate to new chat in background for clean state
    slot = pool._slots.get(slot_id)
    if slot and slot.state.value == "FREE":
        asyncio.create_task(_navigate_slot_to_new_chat(slot_id))

    return {"released": True}


async def _navigate_slot_to_new_chat(slot_id: int) -> None:
    """Background task: navigate released slot to fresh chat."""
    try:
        slot = pool._slots.get(slot_id)
        if slot and slot.state.value == "FREE" and slot.owner is None:
            await pool._browser.navigate_to_new_chat(slot.page)
    except Exception as exc:
        logger.warning(
            "Failed to navigate slot %d to new chat after release: %s",
            slot_id, exc,
        )


@app.get("/api/pool/status")
async def pool_status():
    """Return full pool status including slots, queue, and system health."""
    return pool.get_status()


@app.post("/api/pool/reset")
async def pool_reset():
    """Reset the entire pool: stop all sessions, restart Chrome, warm up slots."""
    slots_available = await pool.reset_all()
    return {"reset": True, "slots_available": slots_available}


@app.post("/api/pool/slot/{slot_id}/reset")
async def slot_reset(slot_id: int):
    """Reset a single slot: close tab, open new one."""
    try:
        await pool.reset_slot(slot_id)
        return {"slot_id": slot_id, "state": "FREE"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Slot {slot_id} not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/health")
async def health():
    """Lightweight liveness probe."""
    return "ok"


@app.post("/api/shutdown")
async def graceful_shutdown():
    """Graceful shutdown: release all slots, close Chrome, stop server.

    Sends the HTTP response first, then initiates shutdown after a short
    delay so the caller receives the confirmation.
    """
    logger.info("Graceful shutdown requested via REST API.")
    asyncio.create_task(_do_graceful_shutdown())
    return {"shutdown": "initiated", "message": "Server shutting down gracefully..."}


async def _do_graceful_shutdown() -> None:
    """Background task: wait for response to be sent, then stop the process."""
    await asyncio.sleep(0.5)
    logger.info("Sending SIGINT to self (PID %d) for clean uvicorn shutdown...", os.getpid())
    # SIGINT triggers uvicorn's graceful shutdown, which runs the lifespan
    # cleanup (stop monitors, close browser, release slots).
    os.kill(os.getpid(), signal.SIGINT)


@app.get("/", response_class=HTMLResponse)
async def test_ui():
    """Serve the debug/test UI."""
    html_path = Path(__file__).parent / "test_ui.html"
    if not html_path.exists():
        return HTMLResponse("<h1>test_ui.html not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# File merge helper
# ---------------------------------------------------------------------------

def _merge_text_content(paths: list[str]) -> str:
    """Read and concatenate text files into a single string.

    Each file's content is preceded by a header line with the filename.
    The result is embedded directly into the message sent to Gemini.

    Args:
        paths: List of absolute file paths to merge.

    Returns:
        Merged text content with file headers.

    Raises:
        HTTPException: If a file cannot be read.
    """
    parts = []
    for file_path in paths:
        path_obj = Path(file_path)
        try:
            content = path_obj.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = path_obj.read_text(encoding="latin-1")
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot read file as text: {file_path} ({exc})",
                )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot read file: {file_path} ({exc})",
            )

        parts.append(f"=== {path_obj.name} ===\n{content}")

    merged_content = "\n\n".join(parts)
    logger.info("Merged %d files into message (%d chars)", len(paths), len(merged_content))
    return merged_content


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Start the uvicorn server with configuration from config.yaml."""
    # Load config early just for host/port
    config_path = os.environ.get("POOL_CONFIG", "config.yaml")
    app_config = load_config(config_path)

    uvicorn.run(
        "server:app",
        host=app_config.server.host,
        port=app_config.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
