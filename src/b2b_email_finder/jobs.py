"""A8: run a search in a background thread so the UI stays responsive and can cancel it.

The search is async (asyncio). It runs in its own event loop on a worker thread; the
UI polls `messages` / `done` and can call `cancel()`, which cancels the asyncio task
from the UI thread in a thread-safe way.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

from .export import save_search
from .pipeline import SearchOptions, run_search


class _ListHandler(logging.Handler):
    def __init__(self, messages: list[str]):
        super().__init__(level=logging.INFO)
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{time.strftime('%H:%M:%S')}  {record.getMessage()}")


class SearchJob:
    def __init__(self, opts: SearchOptions, out_base: Path):
        self.opts = opts
        self.out_base = out_base
        self.messages: list[str] = []
        self.done = False
        self.cancelled = False
        self.error: str | None = None
        self.json_path: Path | None = None
        self.shown = False  # the UI has switched to the new result file
        self.started = time.time()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread = threading.Thread(target=self._run, name="search-job", daemon=True)

    def start(self) -> SearchJob:
        self._thread.start()
        return self

    def _run(self) -> None:
        logger = logging.getLogger("b2b_email_finder")
        handler = _ListHandler(self.messages)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self._loop = asyncio.new_event_loop()
        try:
            self._task = self._loop.create_task(run_search(self.opts))
            result = self._loop.run_until_complete(self._task)
            self.json_path, _ = save_search(result, self.opts.categories, self.out_base)
            self.messages.append(f"Done: {len(result.rows)} places saved to {self.json_path.name}")
        except asyncio.CancelledError:
            self.cancelled = True
            self.messages.append("Search cancelled.")
        except Exception as exc:  # reported in the UI
            self.error = f"{type(exc).__name__}: {exc}"
            self.messages.append(f"Search failed: {self.error}")
        finally:
            logger.removeHandler(handler)
            self._loop.close()
            self.done = True

    def cancel(self) -> None:
        if self._loop is not None and self._task is not None and not self.done:
            self._loop.call_soon_threadsafe(self._task.cancel)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def elapsed(self) -> str:
        seconds = int(time.time() - self.started)
        return f"{seconds // 60}:{seconds % 60:02d}"
