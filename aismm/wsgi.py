"""WSGI entrypoint for production servers (gunicorn/uwsgi) — ``aismm.wsgi:application``.

    gunicorn -w 1 --threads 8 -b 0.0.0.0:8787 aismm.wsgi:application

Importing this module is the server-side equivalent of ``python -m aismm.cli run``:
it creates the data dirs, configures tracing, starts the APScheduler background
scheduler, and exposes the Flask dashboard as ``application``.

Run it with a **single worker**. The scheduler and the dashboard have to live in
the same process because the dashboard re-syncs jobs in-process after an
instruction is created/edited (``scheduler.refresh_jobs``). Extra workers would
each get their own scheduler while the dashboard talked to only one of them.
(Double-posting is still prevented by the store's single-flight lock, but the
other schedulers would drift from the DB until restart.) Use ``--threads`` for
concurrency instead.

Set ``AISMM_ENABLE_SCHEDULER=0`` to serve the dashboard only (approve/reject and
OAuth still work; nothing fires on a schedule).
"""
from __future__ import annotations

import logging

from .config import ensure_dirs, settings
from .dashboard import create_app
from .llm import configure_tracing

logger = logging.getLogger("aismm.wsgi")

ensure_dirs()
# Always — the dashboard's "Run now" drives the agent too, so tracing must be
# pointed somewhere valid even when the scheduler is off.
configure_tracing()

if settings.enable_scheduler:
    from . import scheduler

    scheduler.start()
    logger.info("AISMM scheduler started in the WSGI process")
else:
    logger.info("AISMM scheduler disabled (AISMM_ENABLE_SCHEDULER=0)")

application = create_app()
app = application  # alias, so `aismm.wsgi:app` works too
