"""MCP Thin Client for Gemini Session Pool Service.

Translates MCP tool calls into HTTP requests against the Pool Service
REST API (localhost:9200). No Playwright, no browser, no state.

Can run in multiple Claude Code instances simultaneously — each instance
gets its own MCP server process, all sharing the same pool service.

Usage (after install.py):
    Automatically registered in ~/.claude.json by install.py.
    Manual: claude mcp add gemini-pool -- python ~/.gemini-session-pool/mcp-plugin/mcp_client.py
"""

import os
import sys

# Force UTF-8 on Windows — MCP stdio transport expects UTF-8.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")

import httpx
from mcp.server.fastmcp import FastMCP

POOL_BASE_URL = os.environ.get("GEMINI_POOL_URL", "http://127.0.0.1:9200")

# Send can block for up to 40 minutes while waiting for Gemini response.
SEND_TIMEOUT_S = 2500

mcp = FastMCP(
    "gemini-pool",
    instructions="""
## Gemini Session Pool — Nutzungsprotokoll

Dieses MCP steuert einen Pool von Google-Gemini-Browser-Tabs. Du MUSST das
Acquire/Send/Release-Protokoll einhalten.

### Pflicht-Ablauf (immer einhalten)

1. **Acquire**: gemini_acquire(owner="<eindeutiger-name>")
   - Du bekommst slot_id + lease_token zurueck
   - MERKE DIR BEIDE WERTE — du brauchst sie fuer jeden weiteren Call
   - Falls "queued": warte die geschaetzte Zeit, dann erneut acquire aufrufen
   - Falls "rejected": Pool ist voll, spaeter versuchen

2. **Send** (1..n mal): gemini_send(slot_id=X, token="...", message="...")
   - Blockiert bis Gemini antwortet
   - Mehrere Sends hintereinander = Konversation (Gemini merkt sich Kontext)
   - Optional: merge_paths (Textdateien zusammenfassen) und file_paths (einzeln senden)

3. **Release**: gemini_release(slot_id=X, token="...")
   - IMMER am Ende aufrufen, auch bei Fehlern
   - Gibt den Slot fuer andere Agents frei

### Owner-Namenskonvention

Verwende eindeutige, sprechende Owner-Namen:
- Hauptkontext: "main-context" oder "main-<aufgabe>"
- Sub-Agents: "sub-<aufgabe>" (z.B. "sub-review", "sub-translate")
- Wichtig: Gleicher Owner = Reattach (bekommst bestehenden Slot zurueck)

### Datei-Handling

- merge_paths=["datei1.md", "datei2.md"]: Werden zu EINER Datei zusammengefasst
  und als ein Upload gesendet. Fuer Textdateien, Code, Konzepte.
- file_paths=["bild.png", "doc.pdf"]: Jede Datei einzeln hochgeladen.
  Max 9 pro Turn (bzw. 8 wenn auch merge_paths angegeben). Fuer Bilder, PDFs.
- Alle Pfade muessen absolute Pfade auf dem lokalen Rechner sein.

### Fehlerbehandlung

- 410 (lease_expired): Slot wurde wegen Inaktivitaet freigegeben (>5 min).
  → Neues Acquire noetig.
- 403 (invalid_token): Falsches Token. Prüfe ob du den richtigen Wert verwendest.
- 503 (pool_exhausted): Alle Slots + Queue voll. Spaeter versuchen.
- Verbindungsfehler: Pool Service laeuft nicht. User informieren.

### Parallele Nutzung (Sub-Agents)

Jeder Sub-Agent fuehrt sein eigenes Acquire/Send/Release durch.
Der Orchestrator kann gemini_pool_status() aufrufen um den
Ueberblick zu behalten (braucht keinen eigenen Slot).

### Wichtig

- NIEMALS einen Slot belegen ohne ihn am Ende freizugeben
- NIEMALS parallele Sends auf demselben Slot (ein Slot = ein Agent)
- Token und Slot-ID NICHT raten — immer aus dem Acquire-Ergebnis nehmen
- Bei Fehlern: erst release versuchen, dann ggf. neu acquiren
""",
)


async def _pool_request(
    method: str,
    path: str,
    json: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
) -> dict | str:
    """Make an HTTP request to the pool service.

    Returns parsed JSON or error string.
    """
    url = f"{POOL_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method, url, json=json, headers=headers, timeout=timeout,
            )
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return response.text
    except httpx.ConnectError:
        return (
            "Pool Service nicht erreichbar. "
            "Bitte starten: start.cmd "
            "in ~/.gemini-session-pool/controlserver/"
        )
    except httpx.TimeoutException:
        return "Timeout bei Anfrage an Pool Service."
    except Exception as exc:
        return f"HTTP-Fehler: {exc}"


def _format_acquire_result(data: dict | str) -> str:
    """Format acquire response for Claude."""
    if isinstance(data, str):
        return data

    status = data.get("status", "unknown")
    if status == "acquired":
        reattach = " (reattached)" if data.get("reattached") else ""
        return (
            f"Slot acquired{reattach}.\n"
            f"  slot_id: {data['slot_id']}\n"
            f"  lease_token: {data['lease_token']}\n"
            f"  expires_after_inactive_s: {data.get('expires_after_inactive_s', 300)}"
        )
    elif status == "queued":
        return (
            f"Queued at position {data['queue_position']}. "
            f"Estimated wait: {data['estimated_wait_s']}s. "
            f"Call gemini_acquire again with the same owner to poll."
        )
    elif status == "rejected":
        return (
            f"Pool exhausted. All {data['total_slots']} slots busy, "
            f"queue full ({data['queue_depth']}/{data['queue_max']}). "
            f"Try again later."
        )
    return str(data)


def _format_status(data: dict | str) -> str:
    """Format pool status for Claude."""
    if isinstance(data, str):
        return data

    lines = [
        f"Pool: {data['free']} free, {data['busy']} busy, "
        f"{data['error']} error, {data['queue_depth']} queued"
    ]

    for slot in data.get("slots", []):
        if slot["state"] == "BUSY":
            lines.append(
                f"  Slot {slot['id']}: BUSY "
                f"(owner={slot.get('owner')}, idle={slot.get('idle_s', 0)}s, "
                f"msgs={slot.get('message_count', 0)})"
            )
        else:
            lines.append(f"  Slot {slot['id']}: {slot['state']}")

    for entry in data.get("queue", []):
        lines.append(
            f"  Queue #{entry['position']}: {entry['owner']} "
            f"(waiting {entry['waiting_since_s']}s)"
        )

    sys_info = data.get("system", {})
    lines.append(
        f"System: chrome={sys_info.get('chrome')}, "
        f"login={sys_info.get('login')}, "
        f"uptime={sys_info.get('uptime_s', 0)}s"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def gemini_acquire(owner: str) -> str:
    """Acquire a Gemini session slot from the pool.

    Non-blocking. Returns immediately with one of:
    - Slot assigned (slot_id + lease_token) — ready to send messages
    - Queued (position + estimated wait) — call again to poll
    - Rejected (pool exhausted) — try later

    If the same owner already has a slot, returns the existing slot (reattach).

    Args:
        owner: Unique identifier for this client (e.g. "sub-agent-review").
    """
    data = await _pool_request("POST", "/api/session/acquire", json={"owner": owner})
    return _format_acquire_result(data)


@mcp.tool()
async def gemini_send(
    slot_id: int,
    token: str,
    message: str,
    merge_paths: list[str] | None = None,
    file_paths: list[str] | None = None,
) -> str:
    """Send a message to Gemini on an acquired slot and return the response.

    Blocks until Gemini responds (up to ~40 min for long generations).

    File handling:
    - merge_paths: Text files concatenated into ONE upload (unlimited count).
      Use for source code, docs, concepts that should be reviewed together.
    - file_paths: Files uploaded individually (max 8 if merge_paths given,
      else max 9 per turn). Use for images, PDFs, binary files.

    Args:
        slot_id: The slot ID from gemini_acquire.
        token: The lease token from gemini_acquire.
        message: The message/prompt to send to Gemini.
        merge_paths: Optional list of text file paths to merge into one upload.
        file_paths: Optional list of file paths to upload individually.
    """
    body = {"message": message}
    if merge_paths:
        body["merge_paths"] = merge_paths
    if file_paths:
        body["file_paths"] = file_paths

    data = await _pool_request(
        "POST",
        f"/api/session/{slot_id}/send",
        json=body,
        headers={"X-Lease-Token": token},
        timeout=SEND_TIMEOUT_S,
    )

    if isinstance(data, str):
        return data
    if "error" in data:
        return f"Error: {data.get('detail', data.get('error', 'unknown'))}"

    response_text = data.get("response", "")
    duration_ms = data.get("duration_ms", 0)
    fmt = data.get("format", "unknown")
    return f"{response_text}\n\n---\n[{fmt}, {duration_ms}ms]"


@mcp.tool()
async def gemini_release(slot_id: int, token: str) -> str:
    """Release a Gemini session slot back to the pool.

    The slot becomes available for other clients. Always release
    when done to avoid blocking other agents.

    Args:
        slot_id: The slot ID to release.
        token: The lease token from gemini_acquire.
    """
    data = await _pool_request(
        "POST",
        f"/api/session/{slot_id}/release",
        headers={"X-Lease-Token": token},
    )
    if isinstance(data, str):
        return data
    if data.get("released"):
        return f"Slot {slot_id} released."
    return str(data)


@mcp.tool()
async def gemini_pool_status() -> str:
    """Get full pool status: slots, queue, system health.

    Does not require a slot. Use this to check availability before
    acquiring, or to monitor sub-agent activity.
    """
    data = await _pool_request("GET", "/api/pool/status")
    return _format_status(data)


@mcp.tool()
async def gemini_health() -> str:
    """Quick health check of the pool service.

    Returns 'ok' if the service is running and responsive.
    """
    data = await _pool_request("GET", "/api/health")
    if isinstance(data, str):
        return data
    return "ok"


@mcp.tool()
async def gemini_pool_reset() -> str:
    """Reset the entire pool: terminate all sessions, restart Chrome.

    Use as last resort when the pool is stuck or Chrome has crashed.
    All active sessions are lost.
    """
    data = await _pool_request("POST", "/api/pool/reset", timeout=120.0)
    if isinstance(data, str):
        return data
    return f"Pool reset. {data.get('slots_available', '?')} slots available."


@mcp.tool()
async def gemini_shutdown() -> str:
    """Graceful shutdown of the Gemini pool service.

    Releases all slots, closes Chrome cleanly, and stops the server.
    Use this instead of killing the process manually.
    After shutdown, the server must be restarted manually or via auto-start.
    """
    data = await _pool_request("POST", "/api/shutdown", timeout=30.0)
    if isinstance(data, str):
        return data
    return "Gemini pool service is shutting down gracefully."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
