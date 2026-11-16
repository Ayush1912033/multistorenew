from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime


def create_logger(log_dir):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("POSFileDistributorPCBO")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    handler = logging.FileHandler(
        log_dir / f"transfer_{stamp}.log",
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)

    return logger
