from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure one predictable format for API and worker processes."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
