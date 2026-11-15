"""Check the Meteomatics account: plan limits, and one small historical request.

    python -m ml.check_meteomatics

Prints the plan's limits and usage (never the credentials) and the first hours of wind at one
point a month ago, to confirm historical access works.
"""

import json
import sys
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.services.meteomatics import Meteomatics, MeteomaticsError


def main() -> None:
    s = Settings()
    if not s.meteomatics_enabled:
        sys.exit("Set FIRELENS_METEOMATICS_USERNAME and FIRELENS_METEOMATICS_PASSWORD in .env first")
    with httpx.Client(headers={"User-Agent": "FireLens/0.2"}) as http:
        mm = Meteomatics(http, s.meteomatics_username, s.meteomatics_password)
        try:
            stats = mm.user_stats_sync()
        except MeteomaticsError as exc:
            sys.exit(f"login failed: {exc}")
        stats.pop("user name", None)
        stats.pop("username", None)
        print("account:", json.dumps(stats, indent=1)[:2500])
        t0 = (datetime.now(UTC) - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            h = mm.hourly_sync(t0, t0 + timedelta(hours=5), [(-31.4, -64.2), (-2.8, -54.0)])
            for block in h:
                print({k: v[:3] for k, v in block.items()})
        except MeteomaticsError as exc:
            print("historical request failed:", exc)


if __name__ == "__main__":
    main()
