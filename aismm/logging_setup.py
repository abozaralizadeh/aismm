"""Logging configuration — call once per process, from every entrypoint.

Without this, nothing AISMM logs below WARNING is ever seen: Python's root logger
defaults to WARNING, so the ``logger.info(...)`` calls throughout the codebase
(run start, model wiring, Sora failover, memory writes, publish decisions) are
silently discarded. Under gunicorn that reads as "the service barely logs".

``LOG_LEVEL`` controls our own loggers (default ``INFO``). Third-party libraries
are pinned lower so their per-request chatter doesn't bury the run: httpx logs a
line per HTTP call, the Azure SDKs log request/response envelopes, and APScheduler
narrates every job wake-up.

Set ``LOG_LEVEL=DEBUG`` when chasing a problem — that turns on httpx's request
lines too, which is usually what you want when a platform API is misbehaving.
"""
from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%H:%M:%S"

# Libraries that are useful at DEBUG but noisy at INFO.
_NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "azure": logging.WARNING,
    "azure.core.pipeline.policies.http_logging_policy": logging.WARNING,
    "apscheduler.executors.default": logging.WARNING,
    "apscheduler.scheduler": logging.INFO,
    "openai": logging.WARNING,
    "openai._base_client": logging.WARNING,
    "PIL": logging.WARNING,
}

_configured = False


def configure_logging(level: str | None = None, *, force: bool = False) -> None:
    """Set up root logging. Idempotent — safe to call from several entrypoints."""
    global _configured
    if _configured and not force:
        return

    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    numeric = getattr(logging, resolved, logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric)
    # Replace any handler a host (gunicorn) installed, so our format wins.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    root.addHandler(handler)

    for name, lib_level in _NOISY.items():
        # At DEBUG the caller wants everything, including the libraries.
        logging.getLogger(name).setLevel(logging.DEBUG if numeric <= logging.DEBUG
                                         else lib_level)

    _configured = True
    logging.getLogger("aismm").info("Logging at %s", resolved)
