"""Logging helpers: console plus a rotating file under logs/.

All named loggers propagate to the root logger, which carries the single
configured handler set -- so every module's messages use the same format
regardless of import order.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import LOGS_DIR

_configured = False
_FMT = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")


def get_logger(name: str = "stockllm") -> logging.Logger:
    global _configured
    logger = logging.getLogger(name)
    if _configured:
        return logger
    _configured = True
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(_FMT)
        root.addHandler(console)
    try:
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            fh = RotatingFileHandler(LOGS_DIR / "stockllm.log", maxBytes=5_000_000,
                                     backupCount=3, encoding="utf-8")
            fh.setFormatter(_FMT)
            root.addHandler(fh)
    except OSError:
        pass
    return logger
