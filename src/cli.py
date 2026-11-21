"""Command-line launcher: `synergy-analyser` (also the standalone binaries).

Starts the local web app on 127.0.0.1, picks a free port if the default is
taken, keeps saved datasets in a per-user folder (so it works the same no
matter which folder it's started from), and opens the app in the browser.
"""

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APP_DIR_NAME = "synergy-analyser"


def app_dir() -> Path:
    """Per-user folder for this app's files (saved data, downloaded browser)."""
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / APP_DIR_NAME


def default_data_dir() -> Path:
    """Where saved datasets live: SYNERGY_DATA_DIR, else a per-user app folder."""
    override = os.environ.get("SYNERGY_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return app_dir() / "data"


def configure_frozen_browser_path() -> None:
    """In a standalone binary, keep Playwright's downloaded browser in a
    permanent per-user folder.

    Inside a frozen build Playwright otherwise defaults to the binary's
    temporary unpack folder, which is recreated every launch, so the one-off
    browser download would be lost each time. Installs run with Python (uv,
    dev) keep Playwright's normal shared location.
    """
    if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(app_dir() / "browsers")


def find_free_port(preferred: int, attempts: int = 20) -> int:
    """`preferred` if it's free, else the next free port above it."""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port found between {preferred} and {preferred + attempts - 1}.")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="synergy-analyser",
        description="Synergy Battery Feasibility Analyser — opens in your web browser.",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port to use (default {DEFAULT_PORT}; the next free one if taken)")
    p.add_argument("--data-dir", type=Path, default=None,
                   help="folder for saved datasets (default: a per-user app folder)")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open the browser automatically")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    configure_frozen_browser_path()
    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    import uvicorn

    from . import main as app_module

    app_module.DATA_DIR = data_dir
    port = find_free_port(args.port)
    url = f"http://{HOST}:{port}"

    print("Synergy Battery Feasibility Analyser")
    print(f"  Open:        {url}")
    print(f"  Saved data:  {data_dir}")
    print("  Stop:        press Ctrl+C (or close this window)")
    sys.stdout.flush()

    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    uvicorn.run(app_module.app, host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
