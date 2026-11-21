"""Configuration loader for Morning Brief.

Loads settings from environment variables (with .env file support).
Follows twelve-factor app principles.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

VALIDATOR_MODES = ("enforce", "warn", "off")


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    model: str = "gemma4:e4b"
    # Per-article extraction (structured output)
    num_ctx: int = 8192
    extract_temperature: float = 0.1
    extract_seed: int = 42
    extract_max_words: int = 120
    extract_num_predict: int = 900
    # Briefing context window: up to READER_MAX_TOTAL articles with details.
    briefing_num_ctx: int = 32768
    chars_per_token: int = 4


@dataclass
class APIKeys:
    finnhub: str = ""
    fred: str = ""
    coingecko: str = ""
    etherscan: str = ""
    anthropic: str = ""
    sendgrid: str = ""


@dataclass
class Config:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    api_keys: APIKeys = field(default_factory=APIKeys)
    output_dir: Path = Path("data/output")
    database_path: Path = Path("data/morning_brief.db")
    feeds_path: Path = Path("docs/FEEDS.md")
    deploy_enabled: bool = False
    deploy_branch: str = "gh-pages"
    dashboard_hmac_key: str = ""
    signals_enabled: bool = False
    signals_dir: Path = Path("data/signals")
    dedupe_window_hours: int = 48
    feeds_overlay: Path | None = None
    validator_mode: str = "enforce"
    banned_overlay: Path | None = None
    reader_max_per_category: int = 15
    reader_max_total: int = 120
    run_deadline_minutes: int = 60


def _validator_mode() -> str:
    """VALIDATOR_MODE: enforce (default), warn or off; anything else falls back to enforce."""
    raw = os.environ.get("VALIDATOR_MODE", "enforce").strip().lower() or "enforce"
    if raw not in VALIDATOR_MODES:
        logger.warning("Unknown VALIDATOR_MODE %r; using 'enforce'", raw)
        return "enforce"
    return raw


def load_config() -> Config:
    """Load configuration from environment variables."""
    return Config(
        ollama=OllamaConfig(
            host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_MODEL", "gemma4:e4b"),
            num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
            extract_temperature=float(os.environ.get("EXTRACT_TEMPERATURE", "0.1")),
            extract_seed=int(os.environ.get("EXTRACT_SEED", "42")),
            extract_max_words=int(os.environ.get("EXTRACT_MAX_WORDS", "120")),
            extract_num_predict=int(os.environ.get("EXTRACT_NUM_PREDICT", "900")),
            briefing_num_ctx=int(os.environ.get("BRIEFING_NUM_CTX", "32768")),
            chars_per_token=int(os.environ.get("CHARS_PER_TOKEN", "4")),
        ),
        api_keys=APIKeys(
            finnhub=os.environ.get("FINNHUB_API_KEY", ""),
            fred=os.environ.get("FRED_API_KEY", ""),
            coingecko=os.environ.get("COINGECKO_API_KEY", ""),
            etherscan=os.environ.get("ETHERSCAN_API_KEY", ""),
            anthropic=os.environ.get("ANTHROPIC_API_KEY", ""),
            sendgrid=os.environ.get("SENDGRID_API_KEY", ""),
        ),
        output_dir=Path(os.environ.get("OUTPUT_DIR", "data/output")),
        database_path=Path(os.environ.get("DATABASE_PATH", "data/morning_brief.db")),
        deploy_enabled=os.environ.get("DEPLOY_ENABLED", "").lower() in ("1", "true", "yes"),
        deploy_branch=os.environ.get("DEPLOY_BRANCH", "gh-pages"),
        dashboard_hmac_key=os.environ.get("DASHBOARD_HMAC_KEY", ""),
        signals_enabled=os.environ.get("SIGNALS_ENABLED", "").lower() in ("1", "true", "yes"),
        signals_dir=Path(os.environ.get("SIGNALS_DIR", "data/signals")),
        dedupe_window_hours=int(os.environ.get("DEDUPE_WINDOW_HOURS", "48")),
        feeds_overlay=Path(v) if (v := os.environ.get("MORNING_BRIEF_FEEDS_OVERLAY")) else None,
        validator_mode=_validator_mode(),
        banned_overlay=Path(v) if (v := os.environ.get("MORNING_BRIEF_BANNED_OVERLAY")) else None,
        reader_max_per_category=int(os.environ.get("READER_MAX_PER_CATEGORY", "15")),
        reader_max_total=int(os.environ.get("READER_MAX_TOTAL", "120")),
        run_deadline_minutes=int(os.environ.get("RUN_DEADLINE_MINUTES", "60")),
    )
