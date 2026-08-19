"""Notification sweep. Run hourly from cron.

Hourly rather than daily because each student is reminded at their own local
hour; a single hourly run covers every timezone without per-user scheduling.

    */5 * * * *  cd /app && python -m scripts.send_reminders
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import session_scope  # noqa: E402
from app.notifications import sweep_all  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    # CRON_MODE=daily when the scheduler can only fire once a day.
    hourly = os.environ.get("CRON_MODE", "hourly").lower() != "daily"
    with session_scope() as db:
        n = sweep_all(db, respect_reminder_hour=hourly)
    logging.info("sweep complete: %d notifications created", n)
