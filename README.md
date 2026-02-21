# Gemini Session Pool Service

A session pool manager for Google Gemini browser tabs, analogous to a database connection pool. Enables multiple Claude Code instances and sub-agents to use Gemini in parallel.

## Repository Structure

```
controlserver/     FastAPI + Playwright browser automation
mcp-plugin/        MCP thin client (Claude ↔ Controlserver bridge)
skill/             Skill instructions for Claude (/gemini-pool-review)
dev/               Development tools (not installed)
install.py         One-step installer
```

## Installation

```bash
python install.py
```

This copies files to `~/.gemini-session-pool/`, installs dependencies, registers the MCP server in `~/.claude.json`, and installs the skill.

Options:
- `--force` — Overwrite config.yaml even if customized
- `--skip-deps` — Skip pip install + playwright install
- `--skip-mcp` — Skip MCP registration
- `--skip-skill` — Skip skill installation

## After Installation

Start the controlserver:

```bash
# Windows
~\.gemini-session-pool\controlserver\start.cmd

# Linux/macOS
cd ~/.gemini-session-pool/controlserver && python server.py
```

On first start, Chrome opens for manual login (Google SSO). After login, the service warms up N tabs and starts the REST API on `http://127.0.0.1:9200`.

## API Overview

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/session/acquire` | POST | Request a slot (non-blocking) |
| `/api/session/{id}/send` | POST | Send message, get response |
| `/api/session/{id}/release` | POST | Release a slot |
| `/api/pool/status` | GET | Full pool status |
| `/api/pool/reset` | POST | Reset entire pool |
| `/api/pool/slot/{id}/reset` | POST | Reset single slot |
| `/api/health` | GET | Liveness probe |

## Typical Flow

```bash
# 1. Acquire a slot
curl -X POST http://localhost:9200/api/session/acquire \
  -H "Content-Type: application/json" \
  -d '{"owner": "my-agent"}'

# 2. Send a message
curl -X POST http://localhost:9200/api/session/0/send \
  -H "Content-Type: application/json" \
  -H "X-Lease-Token: <token>" \
  -d '{"message": "Hello Gemini"}'

# 3. Release the slot
curl -X POST http://localhost:9200/api/session/0/release \
  -H "X-Lease-Token: <token>"
```

## Configuration

Edit `~/.gemini-session-pool/controlserver/config.yaml` to adjust pool size, timeouts, browser settings, and logging.
