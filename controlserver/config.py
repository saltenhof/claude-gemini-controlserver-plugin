"""Configuration loader for the Gemini Session Pool Service.

Loads a YAML config file into frozen dataclasses with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server binding configuration."""

    host: str = "127.0.0.1"
    port: int = 9200


@dataclass(frozen=True)
class PoolConfig:
    """Pool sizing and timeout configuration."""

    size: int = 4
    inactivity_timeout_s: int = 300
    max_queue_depth: int = 10


@dataclass(frozen=True)
class BrowserConfig:
    """Playwright browser configuration."""

    headless: bool = False
    chrome_profile_dir: str = "~/.gemini-session-pool/user_data"
    navigation_timeout_ms: int = 30_000
    navigation_retries: int = 3
    response_timeout_ms: int = 2_400_000
    gem_url: str = "https://gemini.google.com/gem/27117b3dc0da"
    preferred_model: str = "Pro"
    max_files_per_turn: int = 9

    @property
    def resolved_profile_dir(self) -> Path:
        """Return the chrome profile directory with ~ expanded."""
        return Path(os.path.expanduser(self.chrome_profile_dir))


@dataclass(frozen=True)
class HealthConfig:
    """Health and inactivity monitor intervals."""

    check_interval_s: int = 60
    inactivity_check_interval_s: int = 30


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""

    dir: str = "~/.gemini-session-pool/logs"
    level: str = "INFO"
    error_level: str = "DEBUG"
    max_file_size_mb: int = 50
    backup_count: int = 5


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration aggregating all sub-configs."""

    server: ServerConfig = field(default_factory=ServerConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _build_dataclass(cls: type, raw: dict[str, Any] | None):
    """Build a frozen dataclass from a dict, ignoring unknown keys."""
    if raw is None:
        return cls()
    known_fields = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return cls(**filtered)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load configuration from a YAML file.

    Falls back to defaults for any missing section or key.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A fully populated AppConfig instance.
    """
    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return AppConfig(
        server=_build_dataclass(ServerConfig, raw.get("server")),
        pool=_build_dataclass(PoolConfig, raw.get("pool")),
        browser=_build_dataclass(BrowserConfig, raw.get("browser")),
        health=_build_dataclass(HealthConfig, raw.get("health")),
        logging=_build_dataclass(LoggingConfig, raw.get("logging")),
    )
