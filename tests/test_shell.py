"""The desktop shell: port selection, engine thread lifecycle, native bridge.

These are the three things that quietly break desktop Python applications, so
they are covered here rather than left to manual testing.
"""

from __future__ import annotations

import socket
import time

import httpx
import pytest

from engine.main import EngineServer, JsApi, find_free_port


def test_find_free_port_returns_a_usable_port():
    port = find_free_port()

    assert 1024 < port < 65536
    # Nothing is holding it, so it can be bound.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_find_free_port_does_not_hard_code_8000():
    """A user with something on 8000 must still be able to start the app."""
    ports = {find_free_port() for _ in range(5)}
    assert 8000 not in ports


def test_engine_starts_answers_and_stops_cleanly():
    port = find_free_port()
    server = EngineServer(port)
    server.start()
    try:
        server.wait_until_ready(timeout=15)
        response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        server.stop()

    # After stop, the port is released and nothing answers.
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1)


def test_stopping_a_server_that_never_started_is_harmless():
    EngineServer(find_free_port()).stop()


def test_a_dead_engine_thread_is_reported_at_once():
    """No point waiting fifteen seconds for a thread that already died."""
    server = EngineServer(find_free_port())  # never started, so not alive

    with pytest.raises(RuntimeError) as info:
        server.wait_until_ready(timeout=5)

    message = str(info.value)
    assert "stopped while starting up" in message
    assert "log file" in message  # tell the user where to look


def test_wait_until_ready_times_out_with_a_readable_message():
    """A silent hang is worse than a clear failure."""
    import threading

    stop = threading.Event()
    server = EngineServer(find_free_port())
    # A thread that stays alive but never serves anything, so the health
    # check keeps failing and the timeout is the branch under test.
    server._thread = threading.Thread(target=stop.wait, daemon=True)
    server._thread.start()

    try:
        with pytest.raises(RuntimeError) as info:
            server.wait_until_ready(timeout=1)
    finally:
        stop.set()

    message = str(info.value)
    assert "did not start within" in message
    assert "log file" in message


def test_js_api_reports_the_port():
    assert JsApi(54321).get_api_port() == 54321


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, None),
        ((), None),
        ("", None),
        ("D:\\drawings", "D:\\drawings"),
        (("D:\\drawings",), "D:\\drawings"),
        (["D:\\a", "D:\\b"], "D:\\a"),
    ],
)
def test_js_api_normalises_every_dialog_return_shape(result, expected):
    """pywebview returns a tuple, a string or None depending on the dialog."""
    assert JsApi._first(result) == expected


def test_dialogs_return_none_when_there_is_no_window():
    """In a browser, or before the window exists, dialogs must not raise."""
    api = JsApi(1234)
    assert api.pick_folder("Choose a folder") is None
    assert api.pick_file("Choose a file", ["All files (*.*)"]) is None


def test_the_engine_stops_promptly_with_a_progress_socket_open():
    """Regression: closing the window took the full shutdown timeout.

    The progress WebSocket looped forever, so Uvicorn's graceful shutdown
    waited it out and the process only exited via `force_exit`. A user closing
    the window should not sit watching it for five seconds.
    """
    from fastapi.testclient import TestClient

    from engine.api.app import create_app

    port = find_free_port()
    server = EngineServer(port)
    server.start()
    server.wait_until_ready(timeout=15)

    # Hold a progress socket open, exactly as the UI does.
    with TestClient(create_app()) as client, client.websocket_connect("/api/ws/progress"):
        started = time.perf_counter()
        server.stop(timeout=10)
        elapsed = time.perf_counter() - started

    assert elapsed < 4.0, f"shutdown took {elapsed:.1f}s with a socket open"


# ── The native dialogs ─────────────────────────────────────────────────


class _FakeWindow:
    """Stands in for a pywebview window, but checks the call the way it would.

    The point is the signature. The real failure was calling
    `create_file_dialog(dialog_title=...)`, which pywebview does not accept, so
    every folder and file pick raised and nothing reached the application.
    """

    def __init__(self, returns: object = None) -> None:
        self.returns = returns
        self.calls: list[tuple[tuple, dict]] = []

    def create_file_dialog(self, *args, **kwargs):
        import inspect

        from webview.window import Window

        # Bind against the real signature: a keyword pywebview does not have
        # must fail here exactly as it would at runtime.
        inspect.signature(Window.create_file_dialog).bind(self, *args, **kwargs)
        self.calls.append((args, kwargs))
        return self.returns


def test_pick_folder_calls_pywebview_the_way_pywebview_expects(monkeypatch):
    """Regression: `dialog_title` is not a pywebview parameter."""
    window = _FakeWindow(returns=(r"D:\drawings\IFC Rev D",))
    monkeypatch.setattr(JsApi, "_window", staticmethod(lambda: window))

    chosen = JsApi(1234).pick_folder("Choose the folder for the previous issue")

    assert chosen == r"D:\drawings\IFC Rev D"
    assert window.calls, "the dialog was never opened"


def test_pick_file_calls_pywebview_the_way_pywebview_expects(monkeypatch):
    window = _FakeWindow(returns=(r"D:\lists\register.xlsx",))
    monkeypatch.setattr(JsApi, "_window", staticmethod(lambda: window))

    chosen = JsApi(1234).pick_file(
        "Choose the issued drawing list", ["Drawing list (*.xlsx)", "All files (*.*)"]
    )

    assert chosen == r"D:\lists\register.xlsx"
    _, kwargs = window.calls[0]
    assert kwargs["allow_multiple"] is False
    assert kwargs["file_types"] == ("Drawing list (*.xlsx)", "All files (*.*)")


def test_cancelling_a_dialog_returns_none(monkeypatch):
    window = _FakeWindow(returns=None)
    monkeypatch.setattr(JsApi, "_window", staticmethod(lambda: window))

    assert JsApi(1234).pick_folder() is None
    assert JsApi(1234).pick_file() is None


def test_the_dialog_kinds_are_the_ones_pywebview_understands():
    """Folder and open are different dialogs; mixing them picks the wrong thing."""
    from engine.main import FOLDER_DIALOG, OPEN_DIALOG

    assert int(FOLDER_DIALOG) == 20
    assert int(OPEN_DIALOG) == 10
