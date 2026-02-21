"""Slot pool manager with acquire/release queue and background monitors.

Manages N Slot instances, a FIFO waiting queue, and background tasks
for inactivity timeout enforcement and health monitoring.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from browser import PoolBrowser
from config import PoolConfig, HealthConfig, BrowserConfig
from slot import Slot, SlotState, LeaseExpiredError, InvalidTokenError

logger = logging.getLogger(__name__)


@dataclass
class SlotAcquired:
    """Returned when a slot is immediately available."""

    status: str = "acquired"
    slot_id: int = 0
    lease_token: str = ""
    reattached: bool = False
    expires_after_inactive_s: int = 300


@dataclass
class Queued:
    """Returned when the client is placed in the waiting queue."""

    status: str = "queued"
    queue_position: int = 0
    estimated_wait_s: int = 30


@dataclass
class Rejected:
    """Returned when the pool is exhausted and the queue is full."""

    status: str = "rejected"
    error: str = "pool_exhausted"
    total_slots: int = 0
    queue_depth: int = 0
    queue_max: int = 0


@dataclass
class _QueueEntry:
    """Internal queue entry tracking a waiting client."""

    owner: str
    enqueued_at: float


class SlotPool:
    """Manages a pool of Gemini session slots.

    Provides non-blocking acquire, release with queue handoff,
    message sending, pool status, and reset operations.
    Background tasks monitor inactivity timeouts and slot health.
    """

    def __init__(
        self,
        slots: list[Slot],
        pool_config: PoolConfig,
        health_config: HealthConfig,
        browser_config: BrowserConfig,
        browser: PoolBrowser,
    ):
        self._slots = {slot.slot_id: slot for slot in slots}
        self._pool_config = pool_config
        self._health_config = health_config
        self._browser_config = browser_config
        self._browser = browser
        self._queue: list[_QueueEntry] = []
        self._start_time = time.monotonic()
        self._last_health_check = time.monotonic()
        self._inactivity_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._login_ok = True

    # --- Public API ---

    def acquire(self, owner: str) -> SlotAcquired | Queued | Rejected:
        """Attempt to acquire a slot for the given owner.

        Non-blocking. Returns immediately with one of three outcomes:
        - SlotAcquired: a slot was assigned (or reattached)
        - Queued: the owner is placed in the waiting queue
        - Rejected: pool exhausted, queue full

        Args:
            owner: Identifier of the requesting client.
        """
        # Reattach check: owner already has a BUSY slot
        for slot in self._slots.values():
            if slot.state == SlotState.BUSY and slot.owner == owner:
                logger.info(
                    "Reattach: owner '%s' -> slot %d", owner, slot.slot_id
                )
                return SlotAcquired(
                    slot_id=slot.slot_id,
                    lease_token=slot.lease_token,
                    reattached=True,
                    expires_after_inactive_s=self._pool_config.inactivity_timeout_s,
                )

        # Owner already in queue — return current position
        for idx, entry in enumerate(self._queue):
            if entry.owner == owner:
                return Queued(
                    queue_position=idx + 1,
                    estimated_wait_s=max(1, (idx + 1) * 30),
                )

        # Try to find a FREE slot
        for slot in self._slots.values():
            if slot.state == SlotState.FREE:
                token = slot.acquire(owner)
                return SlotAcquired(
                    slot_id=slot.slot_id,
                    lease_token=token,
                    reattached=False,
                    expires_after_inactive_s=self._pool_config.inactivity_timeout_s,
                )

        # No free slot — try to queue
        if len(self._queue) < self._pool_config.max_queue_depth:
            self._queue.append(_QueueEntry(owner=owner, enqueued_at=time.monotonic()))
            position = len(self._queue)
            logger.info("Owner '%s' queued at position %d", owner, position)
            return Queued(
                queue_position=position,
                estimated_wait_s=max(1, position * 30),
            )

        # Queue full
        return Rejected(
            total_slots=len(self._slots),
            queue_depth=len(self._queue),
            queue_max=self._pool_config.max_queue_depth,
        )

    def release(self, slot_id: int, token: str) -> None:
        """Release a slot and assign the next queued client if any.

        Args:
            slot_id: The slot to release.
            token: The lease token for validation.

        Raises:
            KeyError: If slot_id does not exist.
            LeaseExpiredError: If the slot is not BUSY.
            InvalidTokenError: If the token is wrong.
        """
        slot = self._get_slot(slot_id)
        slot.validate_lease(token)
        slot.release()
        self._assign_next_in_queue(slot)

    async def send(
        self, slot_id: int, token: str, message: str,
        file_paths: list[str] | None = None,
    ) -> tuple[str, str, int]:
        """Send a message on the given slot.

        Args:
            slot_id: The slot to send on.
            token: The lease token for validation.
            message: The message text.
            file_paths: Optional list of file paths to attach.

        Returns:
            Tuple of (response_text, format, duration_ms).

        Raises:
            KeyError: If slot_id does not exist.
            LeaseExpiredError: If the slot is not BUSY.
            InvalidTokenError: If the token is wrong.
        """
        slot = self._get_slot(slot_id)
        slot.validate_lease(token)
        return await slot.send_message(message, file_paths)

    def get_status(self) -> dict[str, Any]:
        """Return full pool status for orchestrator visibility."""
        slots_info = []
        for slot in self._slots.values():
            info: dict[str, Any] = {
                "id": slot.slot_id,
                "state": slot.state.value,
            }
            if slot.state == SlotState.BUSY:
                info["owner"] = slot.owner
                info["idle_s"] = int(slot.idle_seconds)
                info["message_count"] = slot.message_count
                info["message_preview"] = slot.message_preview
            slots_info.append(info)

        queue_info = []
        now = time.monotonic()
        for idx, entry in enumerate(self._queue):
            queue_info.append({
                "owner": entry.owner,
                "waiting_since_s": int(now - entry.enqueued_at),
                "position": idx + 1,
            })

        free_count = sum(1 for s in self._slots.values() if s.state == SlotState.FREE)
        busy_count = sum(1 for s in self._slots.values() if s.state == SlotState.BUSY)
        error_count = sum(1 for s in self._slots.values() if s.state == SlotState.ERROR)

        return {
            "total_slots": len(self._slots),
            "free": free_count,
            "busy": busy_count,
            "error": error_count,
            "queue_depth": len(self._queue),
            "slots": slots_info,
            "queue": queue_info,
            "system": {
                "chrome": "running" if self._browser is not None else "dead",
                "login": "ok" if self._login_ok else "expired",
                "last_health_check_s": int(now - self._last_health_check),
                "uptime_s": int(now - self._start_time),
            },
        }

    async def reset_all(self) -> int:
        """Stop monitors, release all slots, restart browser, recreate slots.

        Returns:
            Number of slots available after reset.
        """
        logger.warning("Full pool reset initiated...")
        self._stop_monitors()

        # Release all busy slots
        for slot in self._slots.values():
            if slot.state == SlotState.BUSY:
                slot.release()
            elif slot.state == SlotState.ERROR:
                pass  # will be recreated

        # Clear queue
        self._queue.clear()

        # Restart browser
        await self._browser.restart_browser()

        # Recreate all slot pages
        for slot_id in list(self._slots.keys()):
            try:
                page = await self._browser.create_slot_page()
                self._slots[slot_id].mark_free(page)
            except Exception as exc:
                logger.error("Failed to recreate slot %d: %s", slot_id, exc)
                self._slots[slot_id].mark_error()

        self._start_monitors()
        available = sum(1 for s in self._slots.values() if s.state == SlotState.FREE)
        logger.info("Pool reset complete: %d slots available", available)
        return available

    async def reset_slot(self, slot_id: int) -> None:
        """Reset a single slot: close tab, open new one, mark FREE.

        Args:
            slot_id: The slot to reset.

        Raises:
            KeyError: If slot_id does not exist.
        """
        slot = self._get_slot(slot_id)
        logger.info("Resetting slot %d...", slot_id)

        try:
            new_page = await self._browser.restart_slot_page(slot.page)
            slot.mark_free(new_page)
            self._assign_next_in_queue(slot)
        except Exception as exc:
            logger.error("Failed to reset slot %d: %s", slot_id, exc)
            slot.mark_error()
            raise

    # --- Background monitors ---

    def start_monitors(self) -> None:
        """Start the inactivity and health monitor background tasks."""
        self._start_monitors()

    def _start_monitors(self) -> None:
        """Internal: create and start monitor tasks."""
        self._inactivity_task = asyncio.create_task(self._inactivity_monitor())
        self._health_task = asyncio.create_task(self._health_monitor())
        logger.info(
            "Monitors started (inactivity=%ds, health=%ds)",
            self._health_config.inactivity_check_interval_s,
            self._health_config.check_interval_s,
        )

    def _stop_monitors(self) -> None:
        """Cancel background monitor tasks."""
        if self._inactivity_task and not self._inactivity_task.done():
            self._inactivity_task.cancel()
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        logger.info("Monitors stopped.")

    async def _inactivity_monitor(self) -> None:
        """Periodically check for idle BUSY slots and auto-release them."""
        interval = self._health_config.inactivity_check_interval_s
        timeout = self._pool_config.inactivity_timeout_s

        while True:
            try:
                await asyncio.sleep(interval)
                for slot in self._slots.values():
                    if slot.state != SlotState.BUSY:
                        continue
                    if slot.is_sending:
                        continue
                    if slot.idle_seconds > timeout:
                        logger.info(
                            "Slot %d idle for %.0fs (owner='%s'), auto-releasing",
                            slot.slot_id, slot.idle_seconds, slot.owner,
                        )
                        slot.release()
                        # Navigate to new chat for clean state
                        try:
                            await self._browser.navigate_to_new_chat(slot.page)
                        except Exception as exc:
                            logger.warning(
                                "Failed to navigate slot %d to new chat after "
                                "inactivity release: %s", slot.slot_id, exc,
                            )
                        self._assign_next_in_queue(slot)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Inactivity monitor error: %s", exc)

    async def _health_monitor(self) -> None:
        """Periodically check slot and browser health.

        Only runs checks when at least one slot is BUSY — avoids
        unnecessary browser interaction (tab creation, DOM queries)
        that can steal window focus on Windows.
        """
        interval = self._health_config.check_interval_s

        while True:
            try:
                await asyncio.sleep(interval)

                # Skip when pool is idle — no reason to poke the browser
                has_busy = any(
                    s.state == SlotState.BUSY for s in self._slots.values()
                )
                if not has_busy:
                    continue

                self._last_health_check = time.monotonic()

                # Check browser context liveness
                context_alive = await self._browser.check_context_alive()
                if not context_alive:
                    logger.error("Browser context is dead! Initiating full reset...")
                    await self.reset_all()
                    continue

                # Check individual pages
                for slot in self._slots.values():
                    if slot.state == SlotState.ERROR:
                        continue
                    if slot.is_sending:
                        continue

                    page_alive = await self._browser.check_page_alive(slot.page)
                    if not page_alive:
                        logger.warning(
                            "Slot %d page is dead, attempting recovery...",
                            slot.slot_id,
                        )
                        was_busy = slot.state == SlotState.BUSY
                        slot.mark_error()
                        try:
                            new_page = await self._browser.restart_slot_page(slot.page)
                            slot.mark_free(new_page)
                            self._assign_next_in_queue(slot)
                        except Exception as exc:
                            logger.error(
                                "Slot %d recovery failed: %s", slot.slot_id, exc
                            )

                # Login check on first available slot
                for slot in self._slots.values():
                    if slot.state == SlotState.FREE:
                        logged_in = await self._browser.is_logged_in(slot.page)
                        if not logged_in:
                            logger.warning("Login check failed — session may have expired")
                            self._login_ok = False
                        else:
                            self._login_ok = True
                        break

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Health monitor error: %s", exc)

    # --- Private helpers ---

    def _get_slot(self, slot_id: int) -> Slot:
        """Look up a slot by ID, raising KeyError if not found."""
        slot = self._slots.get(slot_id)
        if slot is None:
            raise KeyError(f"Slot {slot_id} does not exist")
        return slot

    def _assign_next_in_queue(self, slot: Slot) -> None:
        """If the slot is FREE and the queue is not empty, assign the next owner."""
        if slot.state != SlotState.FREE:
            return
        if not self._queue:
            return

        entry = self._queue.pop(0)
        token = slot.acquire(entry.owner)
        wait_time = time.monotonic() - entry.enqueued_at
        logger.info(
            "Queue handoff: owner '%s' -> slot %d (waited %.0fs)",
            entry.owner, slot.slot_id, wait_time,
        )

    async def shutdown(self) -> None:
        """Stop monitors and close the browser."""
        self._stop_monitors()
        await self._browser.close()
