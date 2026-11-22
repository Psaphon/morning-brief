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

# The SEC's published fair-access ceiling (requests per second); SEC_MAX_RPS is clamped to it.
SEC_RPS_CEILING = 10
DEFAULT_FILINGS_ITEMS = ("2.02", "7.01", "8.01", "1.01", "2.01", "5.02")

VALIDATOR_MODES = ("enforce", "warn", "off")
WRITER_MODES = ("local", "claude")


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
class FilingFactsConfig:
    """Filing-facts extraction and direction settings (all ``FILINGS_*`` env vars)."""

    num_ctx: int = 16384
    num_predict: int = 768
    seed: int = 42
    guidance_tolerance: float = 0.01
    w_eps_yoy: float = 0.3
    w_rev_yoy: float = 0.3
    w_rev_accel: float = 0.2
    w_guidance: float = 0.2
    k_eps_yoy: float = 25.0
    k_rev_yoy: float = 10.0
    k_rev_accel: float = 5.0
    k_guidance: float = 1.0

    def weights(self) -> dict[str, float]:
        return {
            "eps_yoy": self.w_eps_yoy,
            "rev_yoy": self.w_rev_yoy,
            "rev_accel": self.w_rev_accel,
            "guidance_change": self.w_guidance,
        }

    def scales(self) -> dict[str, float]:
        return {
            "eps_yoy": self.k_eps_yoy,
            "rev_yoy": self.k_rev_yoy,
            "rev_accel": self.k_rev_accel,
            "guidance_change": self.k_guidance,
        }


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _filing_facts_config() -> FilingFactsConfig:
    d = FilingFactsConfig()
    return FilingFactsConfig(
        num_ctx=int(_float_env("FILINGS_NUM_CTX", d.num_ctx)),
        num_predict=int(_float_env("FILINGS_NUM_PREDICT", d.num_predict)),
        seed=int(_float_env("FILINGS_SEED", d.seed)),
        guidance_tolerance=_float_env("GUIDANCE_TOLERANCE", d.guidance_tolerance),
        w_eps_yoy=_float_env("FILINGS_W_EPS_YOY", d.w_eps_yoy),
        w_rev_yoy=_float_env("FILINGS_W_REV_YOY", d.w_rev_yoy),
        w_rev_accel=_float_env("FILINGS_W_REV_ACCEL", d.w_rev_accel),
        w_guidance=_float_env("FILINGS_W_GUIDANCE", d.w_guidance),
        k_eps_yoy=_float_env("FILINGS_K_EPS_YOY", d.k_eps_yoy),
        k_rev_yoy=_float_env("FILINGS_K_REV_YOY", d.k_rev_yoy),
        k_rev_accel=_float_env("FILINGS_K_REV_ACCEL", d.k_rev_accel),
        k_guidance=_float_env("FILINGS_K_GUIDANCE", d.k_guidance),
    )


@dataclass
class APIKeys:
    finnhub: str = ""
    fred: str = ""
    coingecko: str = ""
    etherscan: str = ""
    sendgrid: str = ""


@dataclass
class WriterConfig:
    """Optional Claude section writer. The key is NOT here: it comes from a credential file."""

    mode: str = "local"  # "local" or "claude"
    model: str = "claude-sonnet-5-5"
    api_url: str = "https://api.anthropic.com/v1/messages"
    min_words: int = 250
    max_words: int = 450
    max_drop_share: float = 0.3
    timeout_s: float = 120.0
    monthly_cap_usd: float = 10.0
    # Prices are host config, not repo facts: unset means the claude writer refuses to run.
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None


@dataclass
class Config:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
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
    post_group_hours: int = 12
    contact_email: str = ""
    contact_url: str = ""
    filings_enabled: bool = False
    sec_max_rps: int = 5
    sec_max_retries: int = 3
    filings_items: tuple[str, ...] = DEFAULT_FILINGS_ITEMS
    filings_lookback_days: int = 7
    filings_emit_days: int = 30
    filings_model_cutoff: str = ""
    filing_facts: FilingFactsConfig = field(default_factory=FilingFactsConfig)


def contact_from_env() -> tuple[str, str]:
    """(CONTACT_EMAIL, CONTACT_URL), each stripped, empty when unset."""
    return (
        os.environ.get("CONTACT_EMAIL", "").strip(),
        os.environ.get("CONTACT_URL", "").strip(),
    )


def _sec_max_rps() -> int:
    """SEC_MAX_RPS (default 5); anything above the SEC ceiling is clamped with a WARNING."""
    rps = int(os.environ.get("SEC_MAX_RPS", "5") or "5")
    if rps > SEC_RPS_CEILING:
        logger.warning("SEC_MAX_RPS=%d exceeds the SEC ceiling; using %d", rps, SEC_RPS_CEILING)
        return SEC_RPS_CEILING
    if rps < 1:
        logger.warning("SEC_MAX_RPS=%d is not positive; using 1", rps)
        return 1
    return rps


def _filings_items() -> tuple[str, ...]:
    """FILINGS_ITEMS: comma-separated 8-K item numbers to keep (default 2.02,7.01,...)."""
    raw = os.environ.get("FILINGS_ITEMS", "")
    items = tuple(i.strip() for i in raw.split(",") if i.strip())
    return items or DEFAULT_FILINGS_ITEMS


def audit_min_words() -> int:
    """AUDIT_MIN_WORDS: words a feed item needs to count as full text (default 150)."""
    return int(os.environ.get("AUDIT_MIN_WORDS", "150"))


def _validator_mode() -> str:
    """VALIDATOR_MODE: enforce (default), warn or off; anything else falls back to enforce."""
    raw = os.environ.get("VALIDATOR_MODE", "enforce").strip().lower() or "enforce"
    if raw not in VALIDATOR_MODES:
        logger.warning("Unknown VALIDATOR_MODE %r; using 'enforce'", raw)
        return "enforce"
    return raw


def _writer_mode() -> str:
    """SUMMARY_WRITER: local (default) or claude; anything else falls back to local."""
    raw = os.environ.get("SUMMARY_WRITER", "local").strip().lower() or "local"
    if raw not in WRITER_MODES:
        logger.warning("Unknown SUMMARY_WRITER %r; using 'local'", raw)
        return "local"
    return raw


def _optional_float(name: str) -> float | None:
    """A float setting that may be unset; an unparseable value counts as unset (warning)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s is not a number; treating it as unset", name)
        return None


def _writer_config() -> WriterConfig:
    defaults = WriterConfig()
    return WriterConfig(
        mode=_writer_mode(),
        model=os.environ.get("WRITER_MODEL", defaults.model),
        api_url=os.environ.get("WRITER_API_URL", defaults.api_url),
        min_words=int(os.environ.get("WRITER_MIN_WORDS", defaults.min_words)),
        max_words=int(os.environ.get("WRITER_MAX_WORDS", defaults.max_words)),
        max_drop_share=float(os.environ.get("WRITER_MAX_DROP_SHARE", defaults.max_drop_share)),
        timeout_s=float(os.environ.get("WRITER_TIMEOUT_S", defaults.timeout_s)),
        monthly_cap_usd=float(os.environ.get("WRITER_MONTHLY_CAP_USD", defaults.monthly_cap_usd)),
        price_in_per_mtok=_optional_float("WRITER_PRICE_IN_PER_MTOK"),
        price_out_per_mtok=_optional_float("WRITER_PRICE_OUT_PER_MTOK"),
    )


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
        writer=_writer_config(),
        api_keys=APIKeys(
            finnhub=os.environ.get("FINNHUB_API_KEY", ""),
            fred=os.environ.get("FRED_API_KEY", ""),
            coingecko=os.environ.get("COINGECKO_API_KEY", ""),
            etherscan=os.environ.get("ETHERSCAN_API_KEY", ""),
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
        post_group_hours=int(os.environ.get("POST_GROUP_HOURS", "12")),
        contact_email=contact_from_env()[0],
        contact_url=contact_from_env()[1],
        filings_enabled=os.environ.get("FILINGS_ENABLED", "").lower() in ("1", "true", "yes"),
        sec_max_rps=_sec_max_rps(),
        sec_max_retries=max(0, int(os.environ.get("SEC_MAX_RETRIES", "3") or "3")),
        filings_items=_filings_items(),
        filings_lookback_days=int(os.environ.get("FILINGS_LOOKBACK_DAYS", "7") or "7"),
        filings_emit_days=int(os.environ.get("FILINGS_EMIT_DAYS", "30") or "30"),
        filings_model_cutoff=os.environ.get("FILINGS_MODEL_CUTOFF", "").strip(),
        filing_facts=_filing_facts_config(),
    )
