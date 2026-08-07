# Architecture & Design History

This file is the canonical home for "why does it work this way" for the
scheduling core (`task_runner.py` and its four collaborator modules). It
exists so that docstrings and comments throughout the code can stay short
descriptions of *current* behavior and link here for the *history* and
*rationale* behind it, instead of re-narrating design decisions inline.

If you're looking for "what does function X do," see `function_map.md`
instead. This file only covers "why is it built this way."

---

## 1. From God module to four collaborators

`task_runner.py` originally did everything itself: deciding what work
should exist, who should do it, how a claimed order gets carried out, and
reading `stock_config.json` off disk -- all in one file. It was split by
responsibility into four collaborator modules, each holding a reference
back to the owning `TaskEngine` (`self.engine`) and reading/writing that
engine's shared state (`engine.orders`, `engine.stock_rules`,
`engine.default_orders`, `engine._current_order`, etc.) rather than owning
private copies of it:

| Module | `TaskEngine` attribute | Responsibility |
|---|---|---|
| `order_manager.py` | `.order_manager` | Deciding WHAT work should exist |
| `scheduler.py` | `.scheduler` | Deciding WHO works which order right now |
| `executor.py` | `.executor` | Deciding HOW a claimed order is carried out |
| `config_watcher.py` | `.config_watcher` | The only module that touches the filesystem |

`TaskEngine` itself kept only: the shared state, a handful of trivial
cross-cutting helpers used by all four (`held`, `_order_for_code`,
`complete`), a thin public facade forwarding each old method name to
whichever collaborator now owns it (so `main.py` and other external
callers didn't need to change), and top-level lifecycle
(`initialize`/`run`/`stop`).

This split was purely mechanical (moving code, not changing behavior) and
is complete. It's what made the event-bus conversion below tractable: each
collaborator could add bus subscriptions in its own `__init__` without
touching the others.

---

## 2. The event-bus conversion

### Why

Before this conversion, four separate pieces of state were kept fresh by
unconditional polling loops, each re-deriving live engine state every
tick whether or not anything had actually changed:

1. **Character idling** -- `Scheduler.character_loop`'s idle branch slept
   `poll_interval` and re-checked for work on every wake, even when
   nothing in the order pool had changed.
2. **Equipment delivery** -- a `TaskEngine._delivery_loop` scanned every
   live order, every tick, checking whether any queued equip request could
   now be fulfilled.
3. **Auto-convert** -- a `TaskEngine._auto_convert_loop` re-swept every
   single-use-conversion candidate, every tick, to see if surplus raw
   material had crossed its stock floor.
4. **Config reload** -- `ConfigWatcher` (nee inline in `task_runner.py`)
   unconditionally re-read and re-parsed `stock_config.json` on a ~10x
   `poll_interval` timer, whether or not the file had changed.

All four were converted to reactive pub/sub subscribers driven by a
central `EventBus` (`events.py`), so each piece of state updates in
response to the specific change that could affect it, instead of on a
generic timer.

### The `EventBus`

Pure `asyncio`, no new dependency. `subscribe(event_type, handler)`
registers a sync-or-async handler; `emit(event)` schedules every matching
handler as its own `asyncio.Task` via `asyncio.create_task` and returns
immediately -- it does **not** await them.

**Why fire-and-forget instead of awaiting handlers inline:** `emit()` is
routinely called from code that may already hold a
`character.action_lock`/`busy_lock` (`Scheduler.claim`/`release`/
`complete`, `Executor`'s steps). Awaiting subscribers synchronously there
would risk a reentrant deadlock if a handler calls back into the engine
(e.g. a `StockBelowMinimum` handler that itself claims/releases an order).
Scheduling each handler as an independent task sidesteps that.

Domain events are deliberately flat dataclasses -- ids/codes only, never a
full `WorkOrder`/`Character` object -- so a subscriber always looks up
current live state (`engine.orders[order_id]`,
`engine.account.get_character(...)`) instead of risking a stale copy
captured at emit time.

### The four conversions

**Character idling (`Scheduler`).** `Character` gained a `work_available`
`asyncio.Event`. `Scheduler.__init__` subscribes to
`OrderCreated`/`OrderUpdated`/`OrderReleased`/`OrderCompleted` and `.set()`s
the relevant characters' events whenever the order pool changes in a way
that could give them something to do. `character_loop`'s idle branch now
`await`s `work_available` (bounded by a generous fallback timeout via
`Scheduler.IDLE_WAIT_FALLBACK_MULTIPLIER`) instead of unconditionally
sleeping `poll_interval`.

**Equipment delivery (`Executor`).** `Executor.__init__` subscribes to
`EquipmentRequested` (which names the exact order that just gained a
recipient) and `BankSynced` (which doesn't carry an order id, so the
handler sweeps every order that currently has pending `equip_requests` --
narrower than the whole pool, but not as targeted as the
`EquipmentRequested` path). Both call `_try_deliver_equipment`. The old
`TaskEngine._delivery_loop` was renamed
`_delivery_safety_sweep_loop` and kept as a much-lower-frequency backstop
(`DELIVERY_SWEEP_MULTIPLIER`, 20x `poll_interval`) rather than removed
outright, in case a reactive event fires before a subscriber exists or
races with another state change in the same tick.

**Auto-convert (`OrderManager`).** `OrderManager.__init__` subscribes
`_on_order_completed` (narrows to the one completed code) and
`_on_bank_synced` (re-runs the bounded `refresh_auto_convert_orders`
sweep, since `BankSynced` carries no code) to trigger
`_maybe_auto_convert`. The old `TaskEngine._auto_convert_loop` became
`_auto_convert_safety_sweep_loop`, kept as the same kind of backstop
(`AUTO_CONVERT_SWEEP_MULTIPLIER`, 20x `poll_interval`).

**Config reload (`ConfigWatcher`).** `ConfigWatcher.loop` no longer
unconditionally reparses the file. It does a cheap
`os.stat().st_mtime` check every `MTIME_CHECK_INTERVAL` (2s -- inode
metadata only, no content read) and emits `ConfigChanged` only when the
mtime has genuinely moved. `ConfigWatcher.__init__` subscribes
`_on_config_changed`, which does the actual reparse
(`load_stock_rules_from_file`) and `refresh_stock_orders()` call. This is
the dependency-free path (no `watchdog` added) -- the loop still exists to
perform the cheap mtime check, but the expensive work only happens in
response to a real change.

**Keep-in-stock thresholds (`OrderManager`).** As a fifth, closely related
piece: `_check_stock_thresholds` is subscribed to both `BankSynced` and
`OrderCompleted` (the same two events the auto-convert and delivery paths
already piggyback on, which between them fire at every point a stock
shortfall could newly appear: deposits, gathers completing, crafts
completing). It re-sweeps `engine.stock_rules` and emits one
`StockBelowMinimum` per rule currently under its floor;
`_on_stock_below_minimum` narrows straight to
`_maybe_queue_stock_order(code, minimum)` for that one code rather than
re-sweeping everything.

### The safety-sweep-backstop pattern

Every event-driven loop that replaced a polling loop kept a low-frequency
sweep behind it (typically 10-20x the original `poll_interval`), rather
than deleting the polling loop outright. This is deliberate: reactive
wiring can miss an event (a subscriber that didn't exist yet when an event
fired, two events racing in the same tick, a handler that raised and was
logged-and-swallowed). A rare backstop sweep bounds how long such a miss
can go unnoticed without reintroducing the original cost of polling every
tick. The multiplier constants
(`Scheduler.IDLE_WAIT_FALLBACK_MULTIPLIER`,
`TaskEngine.DELIVERY_SWEEP_MULTIPLIER`,
`TaskEngine.AUTO_CONVERT_SWEEP_MULTIPLIER`,
`ConfigWatcher.MTIME_CHECK_INTERVAL`) all express the same idea: the
safety net should fire rarely enough that it's obviously not doing the
real work.

---

## 3. Concurrency & safety audit fixes

A dedicated audit pass, done after all four polling-to-reactive
conversions landed, found and fixed three concurrency hazards that the
event-bus conversion introduced (none of these existed in the old
single-polling-loop design, because there was only ever one caller at a
time for each piece of state).

### Self-deadlock in `_try_deliver_equipment`

Equipment delivery can now be reached from several independent triggers
for the *same* order: the `EquipmentRequested`/`BankSynced` bus
subscribers, the safety-sweep loop, and a direct call from
`_run_gather_step`/`_run_craft_step` right after an order completes. That
direct call happens while `character_loop`'s
`async with character.busy_lock` is already held for the character that
just finished the order. If that same character is also one of the
order's `equip_requests` recipients (self-equipping -- gathering or
crafting their own upgrade), the delivery code must not try to
`async with requester.busy_lock` for that entry, since `asyncio.Lock`
isn't reentrant and the calling task already holds it -- an instant
deadlock.

**Fix:** `_try_deliver_equipment` takes an `already_locked` parameter (the
calling character's name, passed by `_run_gather_step`/`_run_craft_step`)
so the matching queue entry skips the redundant lock acquire and runs
inline instead.

### Double-delivery race

With multiple concurrent trigger paths for the same order, two calls could
both read `order.equip_requests[0]` before either had popped it, resulting
in two withdraws/equips for one queued unit.

**Fix:** `WorkOrder._delivering`, a plain `bool` field, guards the actual
queue-processing loop -- set `True` for its duration, checked at entry
with no `await` between check and set (so it's atomic on the
single-threaded event loop). A losing concurrent call sees `True` and
returns immediately rather than reprocessing the queue.

**Why a plain bool and not an `asyncio.Lock`:** the two fixes are linked.
If a reactive call (holding no `busy_lock`) blocked waiting for an
in-progress delivery flag held by a direct call that is itself waiting on
a *different* requester's `busy_lock`, and that requester's own
`character_loop` was in turn waiting on this same order's delivery flag,
blocking would recreate exactly the reentrant-deadlock cycle the
`already_locked` fix above was meant to close. A losing call returning
immediately instead of blocking means no call here ever waits on anything
but a single requester's `busy_lock` -- never on another call finishing.
This is why the double-delivery guard had to be a non-blocking flag rather
than a lock, once the self-deadlock fix above was in place.

### Absent subscriber cleanup

Nothing was unsubscribing bus handlers when a collaborator (or the engine)
was torn down. Left unfixed, a stopped-then-discarded `TaskEngine` would
leave its `Scheduler`/`Executor`/`OrderManager`/`ConfigWatcher` instances
referenced forever by the bus's internal subscriber lists, and a stray
event still in flight when `stop()` is called would keep calling back into
collaborators whose owning engine had already been told to shut down.

**Fix:** all four collaborators track every subscription they register in
`self._subscriptions` (set at construction time) and expose a `close()`
method that unsubscribes them all. `TaskEngine.stop()` calls all four
`close()` methods in addition to setting `self.running = False`. Every
`close()` is idempotent (`EventBus.unsubscribe` is a no-op for an
already-removed handler), so calling `stop()` more than once is harmless.

---

## 4. Patterns used throughout this codebase

These recur across the four collaborators and are worth naming once
rather than re-explaining at each call site:

- **Subscribe-in-`__init__`.** Every collaborator registers its bus
  subscriptions at construction time, with handlers extracted as named
  methods (never inline lambdas), so `close()` has something concrete to
  unsubscribe.
- **Late-binding via a `set_x()` method.** When a dependency is created
  after the object that needs it (e.g. `EventBus` is built by
  `TaskEngine.__init__` after `Account` already exists, since `Account` is
  constructed and first synced in `main.py` before the engine does), the
  dependency is attached via a `set_bus()`-style method mirroring the
  existing `set_map_db()` pattern, rather than changing constructor
  signatures. `sync_bank()`/`sync_pending_items()` no-op their `emit()`
  calls harmlessly if the bus hasn't been set yet.
- **Helper extraction for reactive narrowing.** Sweep methods that
  originally iterated a whole collection (`refresh_auto_convert_orders`,
  `refresh_stock_orders`) had their per-item logic extracted into a helper
  (`_maybe_auto_convert`, `_maybe_queue_stock_order`) so a reactive handler
  that knows exactly which one item changed can call the helper directly
  instead of re-sweeping everything.
- **Static vs. live cache distinction.** `database/base_store.py`'s
  TTL-based cache (`is_cache_expired`) was audited and deliberately left
  time-based rather than converted to event-driven. It guards static
  game-content catalogs (items/monsters/resources/maps) that only change
  when the game itself patches -- there's no domain event on `engine.bus`
  that plausibly means "the item catalog changed upstream," unlike the
  four polling sites above, which were all re-deriving live *engine*
  state that changes every tick.

---

## 5. Merging `Character` and `CharacterActions`

`CharacterActions` started as a separate class (`character.actions`, one
instance per `Character`, holding a `self.character` back-reference) so
that `character.py`'s state-parsing code wouldn't be tangled up with the
action/API-calling code. In practice the two were never actually
independent: `CharacterActions` had no purpose without exactly one
`Character` to act on, nothing else ever held a `CharacterActions`
instance on its own, and every one of its ~60 methods immediately reached
back into `self.character` for state. Two classes was overhead without a
corresponding separation of concerns.

The two were merged into one `Character` class in `character.py`. This
removed three pieces of indirection that existed only to bridge the
two-class split, not because they did anything useful on their own:

- **The lazy import in `Character.__init__`.** `from CharacterActions
  import CharacterActions` was deferred to avoid what would otherwise have
  been a circular import between the two files. With one file, there's
  nothing to dodge -- `MapStore` is now imported normally at module level.
- **`Character.__getattr__`'s fallback to `self.actions`.** This existed
  purely to make `character.rest()` work by proxying to
  `character.actions.rest()`. It's now a real, directly-defined method --
  no proxying required.
- **The dual `character.actions.foo()` / `character.foo()` spellings.**
  Both worked before (the second only via the `__getattr__` proxy above),
  which made it ambiguous which was "correct." There's now exactly one
  spelling.

The two classes' separate `__getstate__`/`__setstate__` pairs (one nulling
`action_lock`/`busy_lock`/`work_available`, the other nulling `api`) were
folded into one pair on `Character`, since they were always nulling out
non-picklable state belonging to what is now a single object.

Every external call site that went through `character.actions.foo(...)`
(`executor.py`, `planning.py`, `account.py`, `task_runner.py`) was updated
to call `character.foo(...)` directly. `CharacterActions.py` itself is
retired (kept only as a stub pointing here, since the editing tooling used
for this change had no delete capability on the project's filesystem) --
see `function_map.md`'s `character.py` section for the current method
index.
