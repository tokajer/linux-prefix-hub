"""Run blocking work off the GTK main loop.

Discovery walks several disks, connecting rewrites launcher configs and a
redirect moves files. All of that must not freeze the window, and none of it
may touch widgets from a worker thread -- so the result comes back through
`GLib.idle_add`, which lands on the main loop.

Deliberately tiny: one function. No thread pool, because the UI never has
more than a handful of these in flight and a pool would only hide errors.
"""
from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

from gi.repository import GLib


def run(work: Callable[[], Any],
        done: Callable[[Any, Exception | None], None]) -> None:
    """Call `work()` in a thread, then `done(result, error)` on the main loop.

    Exactly one of `result`/`error` is set. `done` always runs, so callers can
    re-enable whatever button they disabled without a second code path.
    """
    def target() -> None:
        try:
            result, error = work(), None
        except Exception as exc:           # noqa: BLE001 -- reported, not lost
            traceback.print_exc()
            result, error = None, exc
        GLib.idle_add(lambda: (done(result, error), False)[1])

    threading.Thread(target=target, daemon=True).start()
