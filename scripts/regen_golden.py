#!/usr/bin/env python3
"""Regenerate tests/fixtures/brief_fixture/signals.golden.json.

Runs the real ``cli run`` against the fixture articles (same harness as
tests/test_signals_golden.py) and writes the resulting signals artifact.
Run this only in a PR that intentionally changes the signal contract.

Exits 0 on success, non-zero if the pipeline run fails.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.test_signals_golden import (  # noqa: E402
    GOLDEN_PATH,
    load_fixture_articles,
    run_in_temp_dir,
)

logger = logging.getLogger("regen-golden")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        artifact, _ = run_in_temp_dir(load_fixture_articles())
    except Exception:
        logger.exception("Golden regeneration failed; golden file left untouched")
        return 1
    GOLDEN_PATH.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", "utf-8")
    logger.info("Wrote %s (%d signals)", GOLDEN_PATH, len(artifact["signals"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
