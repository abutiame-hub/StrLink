import os
import sys
import logging
import logging.handlers
import tempfile
from pathlib import Path

# backend.py doesn't import webview at module level, so pulling in just
# these helpers here doesn't drag the heavy webview/pythonnet import forward.
from backend import Api, default_backup_dir, get_log_dir

# Ensure WebView2 user data folder is always writable and per-session
temp_dir = os.path.join(tempfile.gettempdir(), f"StrLink_WV2_{os.getpid()}")
os.makedirs(temp_dir, exist_ok=True)
os.environ["WEBVIEW2_USER_DATA_FOLDER"] = temp_dir

# StrLink runs windowed (no console), so without a real log file any
# startup failure - including pywebview/WebView2 engine errors, which are
# reported asynchronously and can't be caught with try/except - would be
# completely invisible to the user. Route everything to a small log file
# up front, before webview is imported, so it's captured from line one.
LOG_DIR = get_log_dir()
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "strlink.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")],
)
log = logging.getLogger("strlink")

import webview

def get_base_dir():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def show_fatal_error(message):
    """Last-resort visible feedback: MessageBoxW is stdlib (ctypes), so it
    works even if the app crashed before webview ever painted a window."""
    log.exception(message)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            f"{message}\n\nDetails were saved to:\n{LOG_PATH}",
            "StrLink - Startup Error",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass

def main():
    base_dir = get_base_dir()
    html_path = os.path.join(base_dir, "ui", "index.html")

    # Read HTML content directly
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    safe_script = Path(base_dir, "ui", "safe.js").read_text(encoding="utf-8")
    html_content = html_content.replace("<!-- STRLINK_SAFE_UI -->", "<script>" + safe_script + "</script>")
    token_guard_script = Path(base_dir, "ui", "token_guard.js").read_text(encoding="utf-8")
    html_content = html_content.replace("<!-- STRLINK_TOKEN_GUARD_UI -->", "<script>" + token_guard_script + "</script>")

    api = Api(backup_dir=default_backup_dir())
    log.info(f"Using backup_dir={api.backup_dir}")

    window = webview.create_window(
        title="StrLink 2.0 - Verified Backup & Safe Restore",
        html=html_content,
        js_api=api,
        width=1240,
        height=860,
        min_size=(960, 680),
        background_color="#0b0f19"
    )
    api.set_window(window)

    webview.start(debug=False)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        show_fatal_error(
            "StrLink failed to start.\n\n"
            "This usually means the Microsoft Edge WebView2 Runtime is "
            "missing or out of date. Try installing/updating it from "
            "Microsoft's website, then run StrLink again."
        )
        sys.exit(1)
