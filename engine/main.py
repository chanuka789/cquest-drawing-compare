"""Application entry point: starts the engine, then opens the window.

Threading (the rule that decides whether this works at all)
-----------------------------------------------------------
On Windows the webview must own the main thread. Uvicorn therefore runs on a
daemon background thread::

    main thread  ->  webview.start()   blocks until the window closes
    background   ->  uvicorn.Server    serves the API

Getting this the wrong way round produces a window that never appears or one
that freezes as soon as it does.

Two channels
------------
Everything except native dialogs goes over HTTP, so the whole UI can be
developed and tested in a normal browser. :class:`JsApi` exposes only the
things a browser genuinely cannot do: pick a real folder, pick a real file,
and report which port the engine is listening on.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
from typing import Any

import httpx
import uvicorn
import webview
from loguru import logger

from engine.api.app import create_app
from engine.settings import get_settings
from engine.storage.paths import get_app_paths
from engine.utils.logging_setup import setup_logging

HOST = "127.0.0.1"
WINDOW_TITLE = "C-Quest Drawing Compare"
WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 900
MIN_WIDTH = 1100
MIN_HEIGHT = 700
BACKGROUND = "#14181C"  # --room-900, so the window never flashes white

#: How long to wait for the engine to answer /api/health before giving up.
STARTUP_TIMEOUT_SECONDS = 15.0
STARTUP_POLL_SECONDS = 0.1


# ── Port ───────────────────────────────────────────────────────────────


def find_free_port(host: str = HOST) -> int:
    """Ask the operating system for a free port.

    Never hard-code 8000. If the user already has something on that port the
    application fails to start, and that is a bad first impression.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


# ── Engine thread ──────────────────────────────────────────────────────


class EngineServer:
    """Uvicorn running on a daemon thread, with a clean shutdown."""

    def __init__(self, port: int) -> None:
        self.port = port
        config = uvicorn.Config(
            create_app(),
            host=HOST,
            port=port,
            log_level="warning",  # loguru owns the log files
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="cqdc-engine", daemon=True)

    def start(self) -> None:
        logger.info("Starting engine thread on {}:{}", HOST, self.port)
        self._thread.start()

    def wait_until_ready(self, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
        """Block until `/api/health` answers, or raise :class:`RuntimeError`."""
        url = f"http://{HOST}:{self.port}/api/health"
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            if not self._thread.is_alive():
                raise RuntimeError(
                    "The engine stopped while starting up. "
                    f"The reason is in the log file at {get_app_paths().logs}."
                )
            try:
                response = httpx.get(url, timeout=1.0)
                if response.status_code == 200:
                    logger.info("Engine ready after health check on {}", url)
                    return
                last_error = RuntimeError(f"health returned HTTP {response.status_code}")
            except httpx.HTTPError as exc:  # not up yet, keep polling
                last_error = exc
            time.sleep(STARTUP_POLL_SECONDS)

        raise RuntimeError(
            f"The engine did not start within {timeout:.0f} seconds. "
            f"Last error: {last_error}. Check the log file in {get_app_paths().logs}."
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Ask Uvicorn to exit and wait for the thread to finish.

        Without this, closing the window leaves a python.exe running forever.
        """
        if not self._thread.is_alive():
            return
        logger.info("Stopping engine thread")
        self._server.should_exit = True
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("Engine thread did not stop within {}s; forcing exit", timeout)
            self._server.force_exit = True
            self._thread.join(timeout=timeout)


# ── Native bridge ──────────────────────────────────────────────────────


class JsApi:
    """The only things the browser cannot do for itself.

    Native dialogs and the port number. Everything else is HTTP.
    """

    def __init__(self, port: int) -> None:
        self._port = port

    # -- the engine ----------------------------------------------------

    def get_api_port(self) -> int:
        """Tell the frontend which port the engine is listening on."""
        return self._port

    # -- dialogs -------------------------------------------------------

    def pick_folder(self, title: str = "Choose a folder") -> str | None:
        """Open the Windows folder dialog. Returns a real path, or None."""
        window = self._window()
        if window is None:
            return None
        result = window.create_file_dialog(webview.FOLDER_DIALOG, dialog_title=title)
        chosen = self._first(result)
        logger.info("pick_folder | title={} | chosen={}", title, chosen)
        return chosen

    def pick_file(
        self,
        title: str = "Choose a file",
        file_types: list[str] | tuple[str, ...] | None = None,
    ) -> str | None:
        """Open the Windows file dialog. Returns a real path, or None.

        `file_types` uses the pywebview form, for example
        ``["Drawing list (*.xlsx;*.xls;*.pdf)", "All files (*.*)"]``.
        """
        window = self._window()
        if window is None:
            return None
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            dialog_title=title,
            allow_multiple=False,
            file_types=tuple(file_types) if file_types else (),
        )
        chosen = self._first(result)
        logger.info("pick_file | title={} | chosen={}", title, chosen)
        return chosen

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _window() -> Any | None:
        return webview.windows[0] if webview.windows else None

    @staticmethod
    def _first(result: Any) -> str | None:
        """pywebview returns a tuple, a string, or None depending on the dialog."""
        if not result:
            return None
        if isinstance(result, str):
            return result
        return str(result[0])


# ── WebView2 ───────────────────────────────────────────────────────────

WEBVIEW2_MESSAGE = (
    "Microsoft Edge WebView2 Runtime is not installed.\n\n"
    "C-Quest Drawing Compare uses it to draw its window.\n\n"
    "Install the Evergreen Runtime from:\n"
    "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
    "Then start the application again."
)


def webview2_installed() -> bool:
    """Best-effort check for the WebView2 Runtime on Windows."""
    if sys.platform != "win32":
        return True

    import winreg

    key_paths = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
            r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
            r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ),
    ]
    for hive, path in key_paths:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version:
                    logger.debug("WebView2 runtime found | version={}", version)
                    return True
        except OSError:
            continue
    return False


def fail_loudly(message: str) -> None:
    """Report a fatal startup problem.

    A packaged build runs with `console=False`, so a message box is the only
    way the user will ever see this.
    """
    logger.error(message)
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, WINDOW_TITLE, 0x10)
    if sys.stderr is not None:
        sys.stderr.write(message + "\n")


# ── Entry point ────────────────────────────────────────────────────────


def main() -> int:
    """Start the engine, open the window, and shut everything down cleanly."""
    settings = get_settings()
    setup_logging()

    mode = "development" if settings.dev_mode else "production"
    logger.info("{} {} starting in {} mode", settings.app_name, settings.version, mode)

    if not webview2_installed():
        fail_loudly(WEBVIEW2_MESSAGE)
        return 2

    port = find_free_port()
    server = EngineServer(port)
    server.start()

    try:
        server.wait_until_ready()
    except RuntimeError as exc:
        fail_loudly(str(exc))
        server.stop()
        return 1

    # Dev mode loads from Vite so the UI hot-reloads; production loads the
    # built UI that this same engine serves.
    url = settings.dev_server_url if settings.dev_mode else f"http://{HOST}:{port}"
    logger.info("Opening window | url={} | api_port={}", url, port)

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        js_api=JsApi(port),
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        background_color=BACKGROUND,
        text_select=False,
    )
    window.events.closed += lambda: logger.info("Window closed by the user")

    exit_code = 0
    try:
        # Blocks on the main thread until the window closes.
        webview.start(debug=settings.dev_mode)
    except Exception as exc:
        fail_loudly(
            f"The application window could not be opened.\n\n{exc}\n\n"
            f"Log files: {get_app_paths().logs}"
        )
        exit_code = 3
    finally:
        server.stop()
        logger.info("Shutdown complete | exit_code={}", exit_code)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
