#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_watcher.py: Decouples file-backed configuration from the scheduler.

Split out of task_runner.py (formerly a "God module"), per the refactor
recommendation to stop having the scheduler poll the filesystem directly.
This module owns reading stock_config.json (a JSON object of
{item_code: minimum_quantity} pairs) and pushing the result into shared
state (engine.stock_rules) plus nudging OrderManager to act on any change
(refresh_stock_orders()) -- so editing the file takes effect without a
restart, without OrderManager/Scheduler/Executor ever touching the
filesystem themselves.

TODO task 9: `loop()` used to unconditionally re-read and re-parse the
whole file every ~10x poll_interval, whether or not it had actually
changed. It now does a cheap `os.stat().st_mtime` diff on a much shorter
interval (MTIME_CHECK_INTERVAL) -- stat() only touches inode metadata, so
this can run far more often than a full JSON-parse-and-rebuild ever
justified -- and only emits `ConfigChanged` (which `_on_config_changed`
reacts to by actually reloading + calling refresh_stock_orders()) when the
mtime has genuinely moved since the last check. This is the
dependency-free path from the TODO's task-9 open decision (no `watchdog`);
see the open-decision note in TODO for the tradeoff against a real
filesystem-notification API.

The important boundary from before is preserved either way: config I/O
lives here and ONLY here.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from events import ConfigChanged
from order_manager import StockRule

if TYPE_CHECKING:
    from task_runner import TaskEngine


class ConfigWatcher:
    """Operates on TaskEngine's shared stock_rules list. See module
    docstring."""

    # How often loop() checks stock_config.json's mtime (TODO task 9).
    # os.stat() is a pure inode-metadata read -- no file content is read
    # unless the mtime has actually changed -- so this can be far shorter
    # than the old ~10x-poll_interval full-reparse cadence without adding
    # meaningful filesystem load.
    MTIME_CHECK_INTERVAL = 2.0

    def __init__(self, engine: "TaskEngine"):
        self.engine = engine
        # Path most recently passed to load_stock_rules_from_file(), if any --
        # remembered so the watch loop can keep checking the same file
        # without the caller having to pass the path twice.
        self.path: Optional[str] = None
        # mtime baseline for the mtime-diff watch (TODO task 9) -- set
        # whenever load_stock_rules_from_file() runs (initial load counts as
        # the baseline, so the first successful load never spuriously fires
        # a redundant ConfigChanged) and updated again each time the loop
        # detects a real change.
        self._last_mtime: Optional[float] = None

        # Reactive reload (TODO task 9), mirroring the subscribe-in-__init__
        # pattern used by Scheduler/Executor/OrderManager: loop() only
        # detects the change and emits; this handler does the actual
        # reparse-and-push-to-OrderManager work, so it's the one place that
        # logic lives regardless of whether it was triggered by the watch
        # loop or (in principle) any other future ConfigChanged emitter.
        #
        # Subscriptions are tracked in self._subscriptions (TODO task 12) so
        # close() can unsubscribe them all -- see that method.
        self._subscriptions = [
            (ConfigChanged, self.engine.bus.subscribe(ConfigChanged, self._on_config_changed)),
        ]

    def close(self) -> None:
        """Unsubscribes every handler this instance registered on
        engine.bus (TODO task 12: subscriber cleanup on TaskEngine.stop()).
        Idempotent -- see Executor.close()'s matching docstring."""
        for event_type, handler in self._subscriptions:
            self.engine.bus.unsubscribe(event_type, handler)

    def load_stock_rules_from_file(self, path: str = "stock_config.json") -> None:
        """Reads a JSON object of {item_code: minimum_quantity} pairs from
        `path` and REPLACES engine.stock_rules with them -- lets keep-in-stock
        targets be tuned by editing a file instead of hardcoding
        OrderManager.add_stock_rule() calls in main.py. Full replacement
        (rather than merging/appending) makes this idempotent and safe to
        call repeatedly: re-running it after the file changes picks up
        additions, edits, AND removals in one shot, rather than only ever
        accumulating stale rules for entries someone deleted from the file.
        Remembers `path` on self.path so the reload loop can keep reloading
        it periodically without the caller re-passing it.

        A missing file is a no-op (first run without a config file present
        is fine -- keep-in-stock is opt-in). A malformed file (bad JSON, not
        an object, or a non-int/negative minimum for some code) logs a
        warning and skips only the bad entries -- never crashes the
        scheduler over a typo in a hand-edited file.

        Also records the file's current mtime as the watch-loop baseline
        (TODO task 9) -- whether the file exists or not (None if missing),
        so a first-time reload never fires a spurious ConfigChanged, and a
        config file that didn't exist yet but appears later is still picked
        up (mtime goes from None to a real value, which counts as "changed").
        """
        self.path = path
        p = Path(path)
        if not p.exists():
            self._last_mtime = None
            return

        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[ConfigWatcher] Failed to read stock config '{path}': {e!r}")
            return

        if not isinstance(raw, dict):
            print(f"[ConfigWatcher] Stock config '{path}' must be a JSON object of "
                  f"{{item_code: minimum}} pairs -- skipping.")
            return

        new_rules: List[StockRule] = []
        for code, minimum in raw.items():
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
                print(f"[ConfigWatcher] Skipping invalid stock rule '{code}': {minimum!r} "
                      f"(must be a non-negative integer).")
                continue
            new_rules.append(StockRule(code=code, minimum=minimum))

        self.engine.stock_rules = new_rules
        self._last_mtime = self._get_mtime()

    def _get_mtime(self, path: Optional[str] = None) -> Optional[float]:
        """Cheap inode-metadata-only read of `path` (or self.path)'s mtime --
        None if no path is set yet or the file doesn't currently exist
        (distinct from "unchanged": None -> a real float still counts as a
        change in loop() below, so a config file created after startup is
        picked up too). Any OSError beyond "doesn't exist" (e.g. a
        permissions problem, or the path momentarily disappearing mid-save
        on some filesystems) is left to the caller -- loop() already guards
        its own call to this."""
        target = path or self.path
        if not target:
            return None
        p = Path(target)
        if not p.exists():
            return None
        return p.stat().st_mtime

    def _on_config_changed(self, event: ConfigChanged) -> None:
        """Reactive handler for ConfigChanged (TODO task 9), subscribed in
        __init__ -- does the actual reparse-and-push-to-OrderManager work
        that loop() used to run unconditionally on every tick. loop() only
        detects that the mtime moved and emits; this is the one place that
        turns "the file changed" into "stock_rules is up to date and any
        newly-below-minimum item has an order queued." Any exception here is
        caught and logged by EventBus._run_handler rather than raised here
        directly, mirroring every other bus subscriber in the project (none
        of Scheduler's/Executor's/OrderManager's _on_* handlers self-guard
        either)."""
        self.load_stock_rules_from_file(event.path)
        self.engine.order_manager.refresh_stock_orders()

    async def loop(self) -> None:
        """TODO task 9: replaces the old "unconditionally re-read and
        re-parse the whole file every ~10x poll_interval" design with a
        real change signal. Checks self.path's mtime every
        MTIME_CHECK_INTERVAL seconds (a pure os.stat() call -- no file
        content is read here) and, only when it has genuinely moved since
        the last check, emits ConfigChanged on engine.bus -- _on_config_changed
        above does the actual reload + refresh_stock_orders() in reaction.
        This means an edit to stock_config.json now takes effect within
        ~MTIME_CHECK_INTERVAL seconds instead of up to the old
        ~10x-poll_interval reload cadence, while the file is only ever
        actually re-parsed when it's truly changed rather than on every
        tick regardless. No-ops (just sleeps) if load_stock_rules_from_file
        was never called, so it's harmless to always include this loop in
        TaskEngine.run() regardless of whether file-backed stock rules are
        in use."""
        engine = self.engine
        while engine.running:
            await asyncio.sleep(self.MTIME_CHECK_INTERVAL)
            if not self.path:
                continue
            try:
                mtime = self._get_mtime()
            except OSError as e:
                # Mirrors the other background loops' per-tick guard: one
                # bad stat() (e.g. a transient permissions/IO hiccup) must
                # not propagate out of asyncio.gather() in TaskEngine.run()
                # and tear down every character's loop along with it.
                print(f"[ConfigWatcher] Error checking '{self.path}' mtime: {e!r}")
                continue
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                engine.bus.emit(ConfigChanged(path=self.path))
