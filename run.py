"""Supervisor for the Feli Voice agent + Cloudflare tunnel.

One command:

    python run.py

Spawns the FastAPI app (`uvicorn main:app`) and the Cloudflare tunnel
(`cloudflared tunnel run feli-voice`) as child processes, restarts either
one if it crashes (capped exponential backoff), prefixes their stdout so
you can tell which line came from which, and shuts both down cleanly on
Ctrl+C.

This is for set-and-forget operation. For dev with hot reload, run uvicorn
directly: `uvicorn main:app --reload`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# ANSI colors for log prefixes. Skip when not on a TTY (e.g. piped to a file)
# or when the user has set NO_COLOR.
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_CYAN = "\033[36m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_GRAY = "\033[90m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""

# `sys.executable -m uvicorn` keeps us inside whatever venv is running this
# supervisor — no need to pre-activate the venv before launching it.
PROCESSES: list[dict] = [
    {
        "name": "uvicorn",
        "cmd": [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
        "color": _CYAN,
    },
    {
        "name": "tunnel",
        "cmd": ["cloudflared", "tunnel", "run", "feli-voice"],
        "color": _YELLOW,
    },
]

_stop = threading.Event()
_children: dict[str, subprocess.Popen] = {}
_children_lock = threading.Lock()
_print_lock = threading.Lock()


def _log(color: str, name: str, line: str) -> None:
    """Thread-safe prefixed print so the two streams don't interleave mid-line."""
    with _print_lock:
        sys.stdout.write(f"{color}[{name}]{_RESET} {line}\n")
        sys.stdout.flush()


def _drain(name: str, color: str, stream) -> None:
    """Pump a child's combined stdout/stderr to our log, line by line."""
    try:
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            _log(color, name, raw.rstrip())
    except Exception:  # noqa: BLE001
        pass


def _spawn(cmd: list[str]) -> subprocess.Popen:
    kwargs: dict = dict(
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if os.name == "nt":
        # New process group on Windows so we can send CTRL_BREAK_EVENT on
        # shutdown without killing this supervisor too.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # New session on Unix so one signal hits the whole process group.
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _supervise(name: str, cmd: list[str], color: str) -> None:
    """Run one process forever, restarting with capped exponential backoff.

    Backoff escalates 1s → 2s → 4s → ... → 30s on rapid crashes, and resets
    to 1s after any run that lasted longer than 60s (so a one-off blip
    doesn't permanently slow recovery).
    """
    backoff = 1.0
    while not _stop.is_set():
        _log(color, name, f"starting: {' '.join(cmd)}")
        started = time.monotonic()
        try:
            proc = _spawn(cmd)
        except FileNotFoundError as e:
            _log(
                color,
                name,
                f"command not found ({e}). Retrying in {backoff:.0f}s.",
            )
            if _stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)
            continue

        with _children_lock:
            _children[name] = proc

        if proc.stdout is not None:
            _drain(name, color, proc.stdout)
        rc = proc.wait()

        with _children_lock:
            _children.pop(name, None)

        if _stop.is_set():
            return

        if time.monotonic() - started > 60.0:
            backoff = 1.0
        _log(color, name, f"exited (code={rc}). Restarting in {backoff:.0f}s.")
        if _stop.wait(backoff):
            return
        backoff = min(backoff * 2, 30.0)


def _shutdown(signum=None, frame=None) -> None:  # noqa: ARG001
    if _stop.is_set():
        return
    _log(_GRAY, "supervisor", "shutdown requested, stopping children...")
    _stop.set()
    with _children_lock:
        children = list(_children.items())
    for name, proc in children:
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    deadline = time.monotonic() + 5.0
    for name, proc in children:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _log(_GRAY, "supervisor", f"{name} didn't exit in time, killing.")
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        # SIGTERM isn't available to signal.signal on Windows.
        pass

    threads: list[threading.Thread] = []
    for p in PROCESSES:
        t = threading.Thread(
            target=_supervise,
            args=(p["name"], p["cmd"], p["color"]),
            daemon=True,
            name=f"supervise-{p['name']}",
        )
        t.start()
        threads.append(t)

    _log(
        _GRAY,
        "supervisor",
        f"running {len(PROCESSES)} processes — Ctrl+C to stop",
    )

    try:
        while not _stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _shutdown()

    for t in threads:
        t.join(timeout=3)


if __name__ == "__main__":
    main()
