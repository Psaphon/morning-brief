"""Configuration loader for Morning Brief.

Loads settings from environment variables (with .env file support).
Follows twelve-factor app principles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    model: str = "qwen2.5:7b-instruct-q4_K_M"


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


def load_config() -> Config:
    """Load configuration from environment variables."""
    return Config(
        ollama=OllamaConfig(
            host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M"),
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
    )
