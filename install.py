#!/usr/bin/env python3
"""Installer for Gemini Session Pool.

Copies controlserver, mcp-plugin, and skill files to ~/.gemini-session-pool/,
installs dependencies, registers the MCP server in ~/.claude.json,
and installs the skill to ~/.claude/skills/.

Requirements: Python 3.10+ (stdlib only — no pip packages needed to run this).

Usage:
    python install.py
    python install.py --force          # Overwrite config.yaml even if it exists
    python install.py --skip-deps      # Skip pip install + playwright install
    python install.py --skip-mcp       # Skip MCP registration in ~/.claude.json
    python install.py --skip-skill     # Skip skill installation
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_DIR = Path(__file__).resolve().parent
HOME = Path.home()
INSTALL_DIR = HOME / ".gemini-session-pool"
SKILL_DIR = HOME / ".claude" / "skills" / "gemini-pool-review"
CLAUDE_JSON = HOME / ".claude.json"

# Directories to copy from repo → install target
COPY_MAP = {
    "controlserver": INSTALL_DIR / "controlserver",
    "mcp-plugin": INSTALL_DIR / "mcp-plugin",
    "skill": INSTALL_DIR / "skill",
}

# Files inside controlserver/ that should NEVER be overwritten
# (unless --force is given)
PRESERVE_FILES = {"config.yaml"}

# Directories that must NEVER be touched
NEVER_TOUCH = {INSTALL_DIR / "user_data", INSTALL_DIR / "logs"}


def _banner(msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def _info(msg: str) -> None:
    print(f"  [+] {msg}")


def _warn(msg: str) -> None:
    print(f"  [!] {msg}")


def _error(msg: str) -> None:
    print(f"  [X] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Phase 1: Copy files
# ---------------------------------------------------------------------------

def phase_copy(force: bool) -> None:
    """Copy code files to ~/.gemini-session-pool/."""
    _banner("Phase 1: Copying files")

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    for src_name, dst_dir in COPY_MAP.items():
        src_dir = REPO_DIR / src_name
        if not src_dir.is_dir():
            _warn(f"Source directory not found: {src_dir} — skipping")
            continue

        dst_dir.mkdir(parents=True, exist_ok=True)

        for src_file in src_dir.iterdir():
            if src_file.is_dir():
                continue  # Skip subdirectories (e.g. __pycache__)

            dst_file = dst_dir / src_file.name

            # Preserve config.yaml on re-install (user may have customized it)
            if src_file.name in PRESERVE_FILES and dst_file.exists() and not force:
                _info(f"Keeping existing {dst_file.relative_to(HOME)}")
                continue

            shutil.copy2(src_file, dst_file)
            _info(f"{'Overwrote' if dst_file.exists() else 'Copied'} → {dst_file.relative_to(HOME)}")

    _info(f"Files installed to {INSTALL_DIR}")


# ---------------------------------------------------------------------------
# Phase 2: Dependencies
# ---------------------------------------------------------------------------

def phase_deps() -> None:
    """Install Python packages and Playwright browser."""
    _banner("Phase 2: Installing dependencies")

    python = sys.executable

    for name, req_dir in [
        ("controlserver", INSTALL_DIR / "controlserver"),
        ("mcp-plugin", INSTALL_DIR / "mcp-plugin"),
    ]:
        req_file = req_dir / "requirements.txt"
        if not req_file.exists():
            _warn(f"{req_file} not found — skipping {name} deps")
            continue

        _info(f"Installing {name} dependencies...")
        result = subprocess.run(
            [python, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _error(f"pip install failed for {name}:")
            print(result.stderr)
        else:
            _info(f"{name} dependencies installed")

    _info("Installing Playwright Chromium...")
    result = subprocess.run(
        [python, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _error("Playwright install failed:")
        print(result.stderr)
    else:
        _info("Playwright Chromium installed")


# ---------------------------------------------------------------------------
# Phase 3: MCP registration
# ---------------------------------------------------------------------------

def phase_mcp() -> None:
    """Register gemini-pool MCP server in ~/.claude.json."""
    _banner("Phase 3: Registering MCP server")

    mcp_script = INSTALL_DIR / "mcp-plugin" / "mcp_client.py"
    if not mcp_script.exists():
        _error(f"MCP script not found at {mcp_script}")
        return

    # Use forward slashes for the path (works on Windows too)
    script_path = str(mcp_script).replace("\\", "/")
    python = sys.executable.replace("\\", "/")

    # Build the MCP entry
    mcp_entry = {
        "command": python,
        "args": [script_path],
        "type": "stdio",
    }

    # Load or create ~/.claude.json
    claude_config = {}
    if CLAUDE_JSON.exists():
        try:
            claude_config = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _warn(f"Could not parse {CLAUDE_JSON}: {exc}")
            _warn("Creating backup and starting fresh")
            backup = CLAUDE_JSON.with_suffix(".json.bak")
            shutil.copy2(CLAUDE_JSON, backup)
            claude_config = {}

    # Ensure mcpServers section exists
    if "mcpServers" not in claude_config:
        claude_config["mcpServers"] = {}

    # Check if already registered with same config
    existing = claude_config["mcpServers"].get("gemini-pool")
    if existing == mcp_entry:
        _info("MCP server 'gemini-pool' already registered (unchanged)")
        return

    claude_config["mcpServers"]["gemini-pool"] = mcp_entry

    CLAUDE_JSON.write_text(
        json.dumps(claude_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _info(f"Registered 'gemini-pool' in {CLAUDE_JSON}")
    _info(f"  command: {python}")
    _info(f"  args: [{script_path}]")


# ---------------------------------------------------------------------------
# Phase 4: Skill installation
# ---------------------------------------------------------------------------

def phase_skill() -> None:
    """Copy SKILL.md to ~/.claude/skills/gemini-pool-review/."""
    _banner("Phase 4: Installing skill")

    src = INSTALL_DIR / "skill" / "SKILL.md"
    if not src.exists():
        _error(f"SKILL.md not found at {src}")
        return

    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    dst = SKILL_DIR / "SKILL.md"
    shutil.copy2(src, dst)
    _info(f"Skill installed to {dst}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Install Gemini Session Pool to ~/.gemini-session-pool/",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite config.yaml even if it already exists",
    )
    parser.add_argument(
        "--skip-deps", action="store_true",
        help="Skip pip install and playwright install",
    )
    parser.add_argument(
        "--skip-mcp", action="store_true",
        help="Skip MCP registration in ~/.claude.json",
    )
    parser.add_argument(
        "--skip-skill", action="store_true",
        help="Skip skill installation to ~/.claude/skills/",
    )
    args = parser.parse_args()

    print(f"Gemini Session Pool Installer")
    print(f"  Source:  {REPO_DIR}")
    print(f"  Target:  {INSTALL_DIR}")
    print(f"  Python:  {sys.executable}")
    print(f"  Platform: {platform.system()} {platform.release()}")

    # Phase 1: Always run
    phase_copy(force=args.force)

    # Phase 2: Dependencies
    if args.skip_deps:
        _banner("Phase 2: Skipped (--skip-deps)")
    else:
        phase_deps()

    # Phase 3: MCP registration
    if args.skip_mcp:
        _banner("Phase 3: Skipped (--skip-mcp)")
    else:
        phase_mcp()

    # Phase 4: Skill
    if args.skip_skill:
        _banner("Phase 4: Skipped (--skip-skill)")
    else:
        phase_skill()

    _banner("Installation complete!")
    print()
    print(f"  Start the server:")
    if platform.system() == "Windows":
        print(f"    {INSTALL_DIR / 'controlserver' / 'start.cmd'}")
    else:
        print(f"    cd {INSTALL_DIR / 'controlserver'} && pip install -r requirements.txt && python server.py")
    print()
    print(f"  MCP server: registered as 'gemini-pool' (auto-started by Claude Code)")
    print(f"  Skill: /gemini-pool-review <prompt>")
    print()


if __name__ == "__main__":
    main()
