"""FIRELENS desktop launcher: the whole app on one computer, no Python or Node needed.

Started by FIRELENS.exe (see backend/firelens.spec). It
  * keeps every file in the user's folder (%LOCALAPPDATA%\\FIRELENS): database, map-context tiles,
    reports, log, and an optional .env with API keys;
  * starts the API on a free local port, serving the website from the same address;
  * opens the browser once the API answers;
  * on the very first start loads the last 24 hours of real NASA FIRMS data (no key needed); with
    no internet it uses synthetic demo data instead, so the map is never empty;
  * if FIRELENS is already running, just opens the browser on it.

Closing the console window (or Ctrl+C) stops FIRELENS.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

BANNER = r"""
  ███████╗██╗██████╗ ███████╗██╗     ███████╗███╗   ██╗███████╗
  ██╔════╝██║██╔══██╗██╔════╝██║     ██╔════╝████╗  ██║██╔════╝
  █████╗  ██║██████╔╝█████╗  ██║     █████╗  ██╔██╗ ██║███████╗
  ██╔══╝  ██║██╔══██╗██╔══╝  ██║     ██╔══╝  ██║╚██╗██║╚════██║
  ██║     ██║██║  ██║███████╗███████╗███████╗██║ ╚████║███████║
  ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝╚═╝  ╚═══╝╚══════╝
  Satellite fire intelligence · ÆOn Software Solutions
"""

FIRMS_PROBE = "https://firms.modaps.eosdis.nasa.gov/"
FIRST_PORT = 8765


def home() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "FIRELENS"
    (base / "data").mkdir(parents=True, exist_ok=True)
    return base


def bundled(*parts: str) -> Path:
    """A file shipped inside the executable (PyInstaller) or, from source, in the repository."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root.joinpath(*parts)


def web_dir() -> Path | None:
    for candidate in (bundled("web"), bundled("frontend", "dist")):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def online() -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(FIRMS_PROBE, method="HEAD"), timeout=6)
        return True
    except Exception:
        return False


def answering(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def free_port() -> int:
    for port in range(FIRST_PORT, FIRST_PORT + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free local port between 8765 and 8814.")


def configure(base: Path) -> str:
    """Point FIRELENS at the user's folder. Returns 'live' or 'demo'. Must run before app.* imports."""
    os.chdir(base)  # an optional .env with keys (FIRMS, Copernicus) is read from here
    live_db = base / "firelens.db"
    demo_db = base / "firelens-demo.db"
    mode = "live"
    if os.environ.get("FIRELENS_DEMO_MODE", "").lower() in ("1", "true", "yes"):
        mode = "demo"
    elif not live_db.exists() and not online():
        mode = "demo"  # first start without internet: synthetic data, so the app still works
    os.environ["FIRELENS_DATA_DIR"] = str(base / "data")
    os.environ["FIRELENS_DATABASE_URL"] = f"sqlite:///{(demo_db if mode == 'demo' else live_db).as_posix()}"
    os.environ["FIRELENS_DEMO_MODE"] = "true" if mode == "demo" else "false"
    os.environ.setdefault("FIRELENS_ALLOW_SCHEMA_UPGRADE", "true")  # the app's own local database
    # the whole planet for 7 days needs several GB of memory at once; 24 h fits any laptop, and
    # the hourly refresh keeps adding to it while FIRELENS runs
    os.environ.setdefault("FIRELENS_SEED_WINDOW", "24h")
    web = web_dir()
    if web:
        os.environ["FIRELENS_WEB_DIR"] = str(web)
    return mode


def open_when_ready(url: str, port: int) -> None:
    for _ in range(240):
        if answering(port):
            webbrowser.open(url)
            print(f"\n  Opened {url} in your browser.\n  Keep this window open while you use FIRELENS; close it to stop.\n")
            return
        time.sleep(0.5)
    print("\n  FIRELENS is taking long to start; see firelens.log in", home())


def main() -> None:
    base = home()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(BANNER)
    saved = base / "port.txt"
    if saved.exists():
        try:
            old = int(saved.read_text().strip())
            if answering(old):
                print(f"  FIRELENS is already running: opening http://127.0.0.1:{old}")
                webbrowser.open(f"http://127.0.0.1:{old}")
                time.sleep(2)
                return
        except ValueError:
            pass

    mode = configure(base)
    port = free_port()
    saved.write_text(str(port))
    url = f"http://127.0.0.1:{port}"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(base / "firelens.log", encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )
    print(f"  Data folder: {base}")
    if mode == "demo":
        print("  No internet on first start: showing DEMO data (synthetic fires).")
        print("  Connect to the internet and delete firelens-demo.db to load real NASA data.")
    elif not (base / "firelens.db").exists():
        print("  First start: loading the last 24 hours of NASA fire data (a minute or two).")
        print("  The map fills in as it arrives.")
    print(f"  Starting at {url} ...")

    import uvicorn

    from app.main import create_app  # after configure(): settings come from the environment

    threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
