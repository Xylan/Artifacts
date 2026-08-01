# Artifacts Project — Function Map

Quick-reference index of every function/method in the project, grouped by file. Use your editor's search to jump to `def <name>` once you know which file it's in.

---

## `main.py`
| Function | Purpose |
|---|---|
| `main()` | Entry point: builds `GameDatabase`/`Account`, syncs, sets naming scheme + roles, runs `TaskEngine` |

---

## `config.py`
No functions — just `TOKEN`, `BASE_URL`, `DB_PATH` constants.

---

## `client.py` — `ArtifactsAPI`
Low-level HTTP layer. Every action/data method is a thin wrapper around `request()`.

**Core**
| Function | Purpose |
|---|---|
| `request(method, endpoint, character=None, payload=None, params=None, return_full=False)` | Public entry; acquires `character.action_lock` if a character is passed |
| `_send_request(...)` | Does the actual httpx call, cooldown wait, rate-limit tracking, error translation |
| `__getstate__` / `__setstate__` | Pickle safety (nulls the httpx client) |
| `__aenter__` / `__aexit__` / `close()` | Async context manager lifecycle |

**Character actions** (`/my/{name}/action/*`)
`move` · `transition` · `fight` · `rest` · `gathering` · `crafting` · `recycling` · `equip` · `unequip` · `use` · `bank_deposit_item` · `bank_deposit_gold` · `bank_withdraw_item` · `bank_withdraw_gold` · `bank_buy_expansion` · `npc_buy` · `npc_sell` · `ge_buy` · `ge_create_sell_order` · `ge_create_buy_order` · `ge_fill_buy_order` · `ge_cancel_order` · `task_new` · `task_complete` · `task_cancel` · `task_exchange` · `task_trade` · `give_gold` · `give_items` · `claim_item` · `delete_item` · `change_skin` · `rename_character`

**Catalog/data endpoints** (paginated, `return_full=True`)
`get_items` · `get_monsters` · `get_resources` · `get_maps` · `get_events` · `get_events_active`

**Account-level** (live, not cached)
`get_my_details` · `get_my_characters` · `get_my_bank` · `get_my_bank_items` · `get_my_pending_items` · `get_my_rates` · `get_account`

**Roster management**
`create_character` · `get_skins`

**Exceptions**: `APIError`, `CharacterAlreadyAtDestinationError` (490), `CharacterInCooldownError` (499), `InventoryFullError` (497)

---

## `character.py`
**`Cooldown`**
| Function | Purpose |
|---|---|
| `start(duration=None)` | (Re)starts the cooldown timer |
| `remaining` *(property)* | Seconds left |
| `is_ready` *(property)* | Whether cooldown has expired |

**Module-level**
| Function | Purpose |
|---|---|
| `_remaining_from_expiration(expiration_raw)` | Computes true remaining cooldown from absolute `cooldown_expiration`, survives restarts |

**`Character`**
| Function | Purpose |
|---|---|
| `__init__(raw_data, api=None, map_db=None)` | Parses raw API dict into `skills`/`stats`/`equipment`/`location`, builds `.actions` (`CharacterActions`), `action_lock`, `busy_lock`, `work_available` (event-driven idle signal -- see below) |
| `cooldown` *(property/setter)* | Rounded seconds remaining / sets new cooldown |
| `is_ready` *(property)* | Cooldown expired? |
| `inventory_used` *(property)* | Total item count carried |
| `is_inventory_full_by_slots` *(property)* | All slots occupied? |
| `is_inventory_full` *(property)* | Full by qty or by slots |
| `is_inventory_empty` *(property)* | No items carried |
| `current_task` *(property)* | Builds a `Task` view from flat task fields |
| `wait_cooldown()` | Sleeps out any remaining cooldown |
| `update_from_dict(data)` | Syncs character/stats/skills/equipment/location/inventory from an API response |
| `is_at(target, y=None)` | Position/Location/tuple/int equality check |
| `__getstate__` / `__setstate__` | Pickle safety (nulls `action_lock`/`busy_lock`/`work_available`, recreated on unpickle) |
| `__getattr__(name)` | Falls through to `self.actions.<name>` (e.g. `character.rest()`) |
| `__repr__` | Debug string |

**`work_available`** *(`asyncio.Event`, TODO task 5)*: per-character
event-driven idle signal. `Scheduler.character_loop`'s idle branch
`await`s this (bounded by a generous fallback timeout) instead of
unconditionally sleeping `poll_interval`; `Scheduler` subscribes to
`OrderCreated`/`OrderUpdated`/`OrderReleased`/`OrderCompleted` on
`engine.bus` and `.set()`s the relevant characters' events whenever the
order pool changes in a way that could give them something to do -- see
`scheduler.py`'s section below.

Dataclasses (no methods): `Skills`, `Stats`, `Equipment`

---

## `CharacterActions.py` — `CharacterActions`
Per-character action set (`character.actions`), one instance per `Character`.

**Decorator**
| Function | Purpose |
|---|---|
| `sync_character_state(func)` | Wraps an action so its response auto-updates `self.character` |

**Setup / helpers**
`__init__` · `__getstate__` · `__setstate__` · `_normalize_target(target)` · `get_closest_bank(map_db=None)` · `is_at_bank(map_db=None)`

**Movement**
| Function | Purpose |
|---|---|
| `move_to(target)` | Raw move API call (no-ops if already there) |
| `transition()` | Fires the current tile's transition |
| `smart_move(destination, map_db=None)` | Pathfinds via `MapStore` and walks/transitions the route |
| `temporary_relocate(destination, map_db=None, return_to_origin=True)` | Async context manager: go there, yield, return |
| `run_and_return(destination, action_coro, *args, map_db=None, **kwargs)` | Runs one coroutine at a destination then returns |
| `_navigate_to_content(content_identifier, map_db=None)` | Resolves closest tile for a content code and moves there |

**Combat / rest**
`fight(target=None, map_db=None)` · `_execute_fight()` · `rest()`

**Bank** (deposit/withdraw items & gold, expansion)
`_execute_deposit(items)` · `deposit_items(items, map_db=None, return_to_origin=True)` · `deposit_all(map_db=None, return_to_origin=True)` · `_execute_deposit_gold(quantity)` · `deposit_gold(quantity, map_db=None, return_to_origin=True)` · `_execute_withdraw_items(items)` · `withdraw_items(items, map_db=None)` · `_execute_withdraw_gold(quantity)` · `withdraw_gold(quantity, map_db=None)` · `_execute_buy_bank_expansion()` · `buy_bank_expansion(map_db=None)`

**Gathering / Crafting / Recycling**
`gather(resource=None, map_db=None)` · `_execute_gather()` · `craft(code, quantity=1, workshop=None, map_db=None)` · `_execute_craft(code, quantity=1)` · `recycle(code, quantity=1, enhanced=False, workshop=None, map_db=None)` · `_execute_recycle(code, quantity=1, enhanced=False)`

**Equipment**
`equip_items(items)` · `equip(code, slot, quantity=1)` · `unequip_items(slots)` · `unequip(slot, quantity=1)` · `use_item(code, quantity=1)`
> Note: `equip`/`unequip` normalize a trailing `"_slot"` suffix off before hitting the API.

**NPC trading**
`npc_buy(code, quantity, npc=None, map_db=None)` · `_execute_npc_buy(code, quantity)` · `npc_sell(code, quantity, npc=None, map_db=None)` · `_execute_npc_sell(code, quantity)`

**Grand Exchange**
`_at_grand_exchange(map_db=None)` · `ge_buy(order_id, quantity, map_db=None)` · `_execute_ge_buy(order_id, quantity)` · `ge_create_sell_order(code, quantity, price, map_db=None)` · `_execute_ge_create_sell_order(...)` · `ge_create_buy_order(code, quantity, price, map_db=None)` · `_execute_ge_create_buy_order(...)` · `ge_fill_buy_order(order_id, quantity, map_db=None)` · `_execute_ge_fill_buy_order(...)` · `ge_cancel_order(order_id, map_db=None)` · `_execute_ge_cancel_order(order_id)`

**Tasks (board)**
`_at_tasks_master(tasks_master="tasks_master", map_db=None)` · `task_new(...)` · `_execute_task_new()` · `task_complete(...)` · `_execute_task_complete()` · `task_cancel(...)` · `_execute_task_cancel()` · `task_exchange(...)` · `_execute_task_exchange()` · `task_trade(code, quantity, ...)` · `_execute_task_trade(code, quantity)`

**Give / claim / misc**
`give_gold(quantity, to_character)` · `give_items(items, to_character)` · `claim_pending_item(pending_item_id)` · `delete_item(code, quantity)` · `change_skin(skin)` · `rename(new_name)`

---

## `account.py`
**Module-level**
| Function | Purpose |
|---|---|
| `classify_bucket(method, endpoint)` | Maps an HTTP call to its rate-limit bucket (`account`/`data`/`action`/`simulation`) |

**`RateLimitWindow`**
`is_exhausted` *(property)* · `seconds_until_reset` *(property)*

**`RateLimiter`**
`update_from_headers(bucket, headers)` · `update_from_rates_payload(data)` · `wait_if_needed(bucket)`

**`AccountDetails`**
`from_dict(data)` *(classmethod)*

**`Bank`**
`used_slots` *(property)* · `is_full` *(property)*

**`PendingItem`**
`from_dict(data)` *(classmethod)*

**`Account`**
| Function | Purpose |
|---|---|
| `__init__(api, map_db=None)` | Holds live account state + character roster |
| `__getstate__` / `__setstate__` | Pickle safety |
| `set_map_db(map_db)` | Late-binds map DB, propagates to all characters |
| `rate_limiter` *(property)* | Proxies `api.rate_limiter` |
| `sync_details()` | `GET /my/details` |
| `sync_bank()` | `GET /my/bank` + paginated `/my/bank/items`; emits `BankSynced` on `self.bus` at the end (TODO task 6 -- no-ops if `self.bus` is still `None`, see below) |
| `sync_pending_items()` | Paginated `/my/pending_items`; also emits `BankSynced` at the end (optional per TODO task 6 -- pending items rarely change bank state directly, but it's a cheap nudge for any subscriber tracking "account state may have changed") |
| `set_bus(bus)` | Late-binds `engine.bus` onto the account (TODO task 6) -- called once by `TaskEngine.__init__` right after it builds `self.bus`. Needed because `Account` is constructed and first `sync()`ed in `main.py` *before* `TaskEngine`/its bus exist, so the bus can't be a constructor arg; mirrors `set_map_db`'s late-binding pattern |
| `sync_active_events()` | Paginated `/events/active` |
| `sync_characters()` | Builds/updates `Character` objects from `/my/characters` |
| `sync_rate_limits()` | `GET /my/rates` |
| `sync()` | Runs all of the above |
| `get_character(name)` | Roster lookup |
| `__repr__` | Debug string |

---

## `models.py`
Pure dataclasses/enums shared across modules.

| Function | Purpose |
|---|---|
| `parse_reset(value)` | Parses a timestamp (epoch or ISO string) into `datetime` |
| `Task.is_complete` / `Task.is_active` *(properties)* | Task-state checks |
| `Resource.from_dict(data)` *(classmethod)* | |
| `CraftRecipe.from_dict(data)` *(classmethod)* | |
| `Item.from_dict(data)` *(classmethod)*, `is_craftable`, `is_equipable` *(properties)* | |
| `PlanTask.is_assigned` / `is_done` *(properties)* | |
| `Event.from_dict(data)` *(classmethod)* | |

Plain dataclasses with no methods: `Position`, `Location`, `InventoryItem`, `RecipeIngredient`, `ItemCondition`, `ItemEffect`. Enums: `TaskType`, `TaskStatus`.

---

## `planning.py` — one-off wishlist → static plan (`GearList`/`GearPlan`/`PlanRunner`)
| Function | Purpose |
|---|---|
| `item_score(item)` | Naive gear-upgrade heuristic (sum of effect values) |
| `GearList.add(code, quantity=1)` / `.remove(code)` | Wishlist mutation |
| `GearList.for_upgrades(character, db)` *(classmethod)* | Auto-builds a wishlist of best craftable upgrades per slot |
| `GearList.resolve(db, have=None)` | Expands wishlist into a dependency-linked `GearPlan` (recursing craft ingredients / gather nodes) |
| `GearPlan.gather_tasks` / `.craft_tasks` / `.is_complete` *(properties)* | |
| `GearPlan.get(task_id)` | Task lookup |
| `GearPlan.assign(task_id, character_name)` | Manual assignment |
| `GearPlan.auto_assign(characters)` | Greedy least-loaded-eligible-character assignment |
| `GearPlan.summary()` | Human-readable plan dump |
| `held_snapshot(account)` | Total qty of every item across inventories + bank |
| `load_open_plans(task_store)` | Rehydrates in-progress plans from `TaskStore` |
| `PlanRunner._held(code)` | Total qty of `code` held (mirrors `TaskEngine.held`) |
| `PlanRunner._deposit_and_sync(character)` | Deposit + bank resync |
| `PlanRunner._withdraw_for_craft(character, task, crafts_needed)` | Pulls exactly what's needed from bank for N crafts |
| `PlanRunner.run(plan)` | Executes every task in the plan (respecting `depends_on`), concurrently per character |
| `PlanRunner.deposit_all()` | All characters deposit concurrently |

---

## `events.py` — `EventBus` + domain event dataclasses
Foundation for converting `task_runner.py`'s four polling loops (see TODO)
into reactive subscribers. Pure `asyncio`, no new dependency.

| Function | Purpose |
|---|---|
| `EventBus.subscribe(event_type, handler)` | Registers a sync-or-async `handler` for `event_type`; returns `handler` unchanged (handy for later `unsubscribe`) |
| `EventBus.unsubscribe(event_type, handler)` | Idempotent removal (no-op if not subscribed) |
| `EventBus.emit(event)` | Fire-and-forget: schedules every handler subscribed to `type(event)` as its own `asyncio.Task` via `asyncio.create_task` and returns immediately -- does NOT await them (returns the created tasks for callers, e.g. tests, that want to `await asyncio.gather(*...)`) |
| `EventBus._run_handler(handler, event)` *(async, static)* | Runs one handler (awaiting it if it returns an awaitable), catching+logging any exception so one bad subscriber can't kill the emitter's task |

> **Why fire-and-forget instead of awaiting handlers inline:** `emit()` is
> expected to be called from code that may already hold a
> `character.action_lock`/`busy_lock` (`Scheduler.claim`/`release`/
> `complete`, `Executor`'s steps). Awaiting subscribers synchronously there
> risks a reentrant deadlock if a handler calls back into the engine (e.g. a
> `StockBelowMinimum` handler that itself claims/releases an order).
> Scheduling each handler as an independent task sidesteps that -- see the
> task 12 concurrency-audit item in TODO, which should re-check this as
> real subscribers land.

**Domain events** (all plain dataclasses, deliberately flat -- ids/codes
only, never a full `WorkOrder`/`Character` object, so a subscriber always
looks up current live state via `engine.orders[order_id]` /
`engine.account.get_character(...)` instead of risking a stale copy):

| Event | Emitted by | Carries |
|---|---|---|
| `OrderCreated` | **live** — `OrderManager.request_item` (the one emission point; `_bump_ingredients`/`request_equipment`/`refresh_stock_orders`/`refresh_auto_convert_orders` all route through it) whenever a new order is created | `order_id, code, kind, priority, target_quantity` |
| `OrderUpdated` | **live** — `OrderManager.request_item`, when bumping an existing order's `target_quantity` | `order_id, code, target_quantity, priority` |
| `OrderClaimed` | **live** — `Scheduler.claim`, after `order.claimed_by`/`locked_to`/`engine._current_order` are updated | `order_id, character_name` |
| `OrderReleased` | **live** — `Scheduler.release`, after the same bookkeeping is undone | `order_id, character_name` |
| `OrderCompleted` | **live** — `Scheduler.complete` (also reached via `TaskEngine.complete`, a thin forward), after `order.done`/`claimed_by`/`locked_to` are updated | `order_id, code` |
| `BankSynced` | **live** — `Account.sync_bank` (+ `sync_pending_items`, also wired per the "optionally") — TODO task 6, done | *(no fields)* |
| `EquipmentRequested` | **live** — `OrderManager.request_item`, whenever a `requester`/`equip_slot` pair is queued on a new-or-existing order (covers `request_equipment` calls too, since it routes through `request_item`) | `order_id, character_name, code, slot` |
| `EquipmentDelivered` | **live** — `Executor._try_deliver_equipment`, right after a single queued `(character, slot)` request is popped and equipped — TODO task 7, done | `order_id, character_name, code, slot` |
| `StockBelowMinimum` | **live** — `OrderManager._check_stock_thresholds` — TODO task 10, done. Subscribed to `BankSynced`/`OrderCompleted` (the same two events that already fire at every point the task calls out: deposits, gathers completing, crafts completing), it re-sweeps `engine.stock_rules` and emits one `StockBelowMinimum` per rule currently under its floor. Consumed reactively by `OrderManager._on_stock_below_minimum`, which narrows straight to `_maybe_queue_stock_order(event.code, event.minimum)` | `code, current, minimum` |
| `ConfigChanged` | **live** — `ConfigWatcher.loop`, when an `os.stat().st_mtime` check finds `stock_config.json`'s mtime has moved since the last check — TODO task 9, done | `path` |

> **Status:** the bus is now wired into `TaskEngine` (TODO task 2):
> `TaskEngine.__init__` builds `self.bus = EventBus()` before constructing
> `OrderManager`/`Scheduler`/`Executor`/`ConfigWatcher`, and since each of
> those already takes an `engine` reference at construction, they all reach
> it via `self.engine.bus` -- no constructor signature changes were needed
> anywhere. TODO task 3 is now done: `order_manager.py` emits `OrderCreated`/
> `OrderUpdated`/`EquipmentRequested` (see its section below). TODO task 4 is
> now done too: `scheduler.py`'s `Scheduler.claim`/`release`/`complete` each
> emit `OrderClaimed`/`OrderReleased`/`OrderCompleted` on `engine.bus` right
> after mutating `order.claimed_by`/`locked_to`/`done`/`engine._current_order`
> (see its section below). **TODO task 5 is now done too:** `Scheduler`
> subscribes to `OrderCreated`/`OrderUpdated`/`OrderReleased`/
> `OrderCompleted` at construction time and wakes the relevant `Character`s'
> new `work_available` `asyncio.Event`s (see `character.py`'s and
> `scheduler.py`'s sections above), so **polling site 1,
> `Scheduler.character_loop`, is now event-driven** -- its idle branch
> `await`s `work_available` (with a generous fallback timeout) instead of
> `asyncio.sleep(engine.poll_interval)`. **TODO task 6 is now done too:**
> `account.py`'s `Account.sync_bank`/`sync_pending_items` emit `BankSynced`
> on `self.bus` (late-bound via the new `Account.set_bus`, called from
> `TaskEngine.__init__` right after `self.bus` is built -- `Account` is
> constructed and first synced in `main.py` before the engine/bus exist, so
> `self.bus` starts as `None` and the emit is a no-op until `set_bus` runs).
> **TODO task 7 is now done too:** `executor.py`'s `Executor.__init__`
> subscribes to `EquipmentRequested` and `BankSynced` on `engine.bus` and
> reacts by calling `Executor._try_deliver_equipment` only for the order(s)
> implicated by that event (see `executor.py`'s section below) --
> **polling site 2, the old `TaskEngine._delivery_loop`, is now
> event-driven.** The old scan-every-tick loop was kept, per the TODO's
> task-7 open decision, as a much-lower-frequency belt-and-suspenders
> backstop rather than removed outright: renamed
> `TaskEngine._delivery_safety_sweep_loop`, it now sleeps
> `poll_interval * TaskEngine.DELIVERY_SWEEP_MULTIPLIER` (20x) between
> sweeps instead of `poll_interval`, and only re-sweeps orders that
> currently have pending `equip_requests` (same scoping the reactive path
> uses), rather than truly scanning every live order every tick. `executor.py`
> also now emits `EquipmentDelivered` at the point a single request is
> fulfilled (see the table above). **TODO task 8 is now done too:**
> `order_manager.py`'s `OrderManager.__init__` subscribes to `OrderCompleted`
> and `BankSynced` on `engine.bus` (same pattern as `Executor.__init__`) and
> reacts by calling the new `OrderManager._maybe_auto_convert(raw_code)` --
> either for just the one code a completed order implicates, or (for
> `BankSynced`, which carries no code) by sweeping the same bounded
> `_single_use_conversions` dict `refresh_auto_convert_orders` always used,
> never the whole order pool -- see `order_manager.py`'s section below.
> **Polling site 3, the old `TaskEngine._auto_convert_loop`, is now
> event-driven:** renamed `TaskEngine._auto_convert_safety_sweep_loop`, it
> now sleeps `poll_interval * TaskEngine.AUTO_CONVERT_SWEEP_MULTIPLIER`
> (20x) between sweeps instead of `poll_interval`, kept as a backstop per
> the same belt-and-suspenders tradeoff task 7 made for delivery.
> **TODO task 9 is now done too:** `config_watcher.py`'s `ConfigWatcher.loop`
> no longer unconditionally re-reads and re-parses `stock_config.json` every
> ~10x `poll_interval`. It now does a cheap `os.stat().st_mtime` check every
> `ConfigWatcher.MTIME_CHECK_INTERVAL` (2s) — inode metadata only, no file
> content read — and emits `ConfigChanged` on `engine.bus` only when the
> mtime has genuinely moved since the last check (or since
> `load_stock_rules_from_file`'s own baseline). `ConfigWatcher.__init__`
> subscribes its own `_on_config_changed` handler to `ConfigChanged` (same
> subscribe-in-`__init__` pattern as `Scheduler`/`Executor`/`OrderManager`),
> which does the actual reparse (`load_stock_rules_from_file`) +
> `engine.order_manager.refresh_stock_orders()` call — the same two-step
> that used to run unconditionally on every old timer tick, now gated on a
> real change. This is the dependency-free path from the TODO's task-9 open
> decision (no `watchdog` added). **Polling site 4, `ConfigWatcher.loop`, is
> now effectively event-driven too** — the loop still exists (it's what
> performs the cheap mtime check), but the expensive work (JSON parse + full
> `stock_rules` rebuild + `refresh_stock_orders` sweep) only happens
> reactively, in `_on_config_changed`, in response to a real file change.
> **TODO task 10 is now done too:** `order_manager.py`'s `OrderManager.__init__`
> subscribes `_check_stock_thresholds` to both `BankSynced` and `OrderCompleted`
> (the same two events tasks 7/8 already piggyback on) -- between them they
> fire at every point task 10 calls out (deposits, gathers completing, crafts
> completing all route through `Executor`'s deposit-then-`sync_bank` calls,
> which emit `BankSynced`; gathers/crafts finishing also emit
> `OrderCompleted`), so no new emission points were needed in
> `executor.py`/`account.py` themselves. `_check_stock_thresholds` re-sweeps
> `engine.stock_rules` (bounded, same as `refresh_stock_orders` always was)
> and emits `StockBelowMinimum` for anything currently under its floor;
> `OrderManager.__init__` also subscribes `_on_stock_below_minimum` to that
> event, which narrows straight to `_maybe_queue_stock_order(event.code,
> event.minimum)` -- the same per-code logic `refresh_stock_orders` was
> refactored to share (see `order_manager.py`'s section below) -- rather
> than re-sweeping every rule again. `refresh_stock_orders` is therefore no
> longer only-at-startup-or-config-reload: it (or its per-code equivalent)
> now also runs reactively whenever bank/inventory state could plausibly
> have changed. TODO task 11 (TTL-cache audit) and task 12 (concurrency/
> safety pass) are also done -- see their own writeups in `TODO` and, for
> task 12's fixes, the relevant entries in `orders.py`/`executor.py`/
> `task_runner.py`'s sections below. **TODO task 13 is now done too:**
> `tests/test_events.py`, a plain-asyncio smoke test covering all three of
> the task's asks -- see the `tests/test_events.py` section near the end of
> this file. **TODO task 14 (final docstring/map consistency pass) is now
> done too:** `task_runner.py`'s and `config_watcher.py`'s module
> docstrings both already described the event-driven design in full
> (updated incrementally as tasks 7/9/12 landed rather than left
> describing polling until now) -- the only stale wording found was one
> module-boundary bullet in `task_runner.py` undersold `ConfigWatcher`'s
> task-9 mtime-diff design as pushing changes "on a timer"; reworded to
> name the reactive mtime-diff explicitly. This file was read through in
> full for consistency; the "Quick where do I...?" index and every
> collaborator section below were already current. **All 14 TODO tasks
> are now complete.**

---

## `orders.py` — live work-order primitives (`WorkOrder`), `SchedulableOrder` protocol
| Function | Purpose |
|---|---|
| `WorkOrder.base_priority` *(property)* | Int cast of `.priority` |

`SchedulableOrder` (`Protocol`, `runtime_checkable`): the structural "single polymorphic Order class" consolidation — the shared shape (`code`, `node_code`, `skill`, `skill_level`, `target_quantity`, `produces_per_action`) that both `WorkOrder` here and `models.PlanTask` already satisfy field-for-field, without merging their (very different) persistence/concurrency models into one class. See the class docstring for the full rationale. `models.Task` (the API task-board view) deliberately does NOT implement it.

Enums: `OrderKind` (GATHER/CRAFT), `Priority` (DEFAULT/AUTO_CRAFT/KEEP_STOCK/GATHER/CRAFT/EQUIP, ascending). `EQUIP` (40) is the top tier, above `CRAFT` (30) by more than `INERTIA_BONUS` (5), so an equip request always outranks and interrupts whatever a character is currently doing — see `OrderManager.request_equipment`. `AUTO_CRAFT` (5) sits just above `DEFAULT` (0) and below `KEEP_STOCK` (10) — see `OrderManager.refresh_auto_convert_orders`. Constant: `INERTIA_BONUS`.

`WorkOrder._delivering` *(bool, default `False`, TODO task 12)*: non-blocking re-entrancy guard consumed by `executor.Executor._try_deliver_equipment` — see that method's entry in `executor.py`'s section below. Deliberately a plain field rather than an `asyncio.Lock`: the whole point is that a losing concurrent caller returns immediately instead of blocking (see the concurrency-audit note at the bottom of this file).

---

## `roles.py` — naming scheme + skill-role assignment
| Function | Purpose |
|---|---|
| `build_roles(character_names)` | Assigns `ROLE_TEMPLATES` positionally to actual roster names |
| `primary_owner_of(craft_skill, roles=DEFAULT_ROLES)` | Who "owns" a pure craft skill |
| `gather_rank(character_name, skill, roles=DEFAULT_ROLES)` | Tie-break rank for gather-skill preference |
| `ensure_naming_scheme(account, api, names=NAME_SCHEME)` | Renames/creates characters to match `NAME_SCHEME` (best-effort, membership-gated) |

---

## `task_runner.py` — `TaskEngine` (thin orchestrator over 4 collaborator modules)
`task_runner.py` used to be a "God module" doing everything itself. It is
now split by responsibility into four collaborator modules, each taking an
`engine` (the `TaskEngine`) reference at construction and reading/writing
its shared state (`engine.orders`, `engine.stock_rules`,
`engine.default_orders`, `engine._current_order`, etc.):

| Module | `TaskEngine` attribute | Responsibility |
|---|---|---|
| `order_manager.py` | `.order_manager` | Deciding WHAT work should exist: `request_item`/`request_equipment`/`request_upgrades_for`, keep-in-stock rules, auto-convert, default gather tasks, plan verification/debugging |
| `scheduler.py` | `.scheduler` | Deciding WHO works which order right now: `character_eligible`, `_score`, `select_order_for`, `claim`/`release`/`complete`, and `character_loop` |
| `executor.py` | `.executor` | Deciding HOW a claimed order is carried out: `_switch_task`, `_try_deliver_equipment`, `_run_gather_step`, `_craft_batch_size`, `_run_craft_step` |
| `config_watcher.py` | `.config_watcher` | The ONLY module that touches the filesystem: reading/reloading `stock_config.json` into `engine.stock_rules` on a timer |

`TaskEngine` itself keeps: the shared state declared in `__init__`
(including `self.bus`, a per-engine `events.EventBus` built before the four
collaborators below so they can all reach it via `self.engine.bus` with no
constructor changes — see `events.py`'s status note), the trivial
cross-cutting state-query helpers used by all four collaborators (`held`,
`_order_for_code`, `complete`), a thin public facade forwarding each old
method name to the right collaborator (so `main.py`/external callers are
unaffected — see below), and top-level lifecycle.

**Public facade on `TaskEngine`** (same call signatures as before the
split — forwards to whichever collaborator now owns the logic):
`request_item` · `request_equipment` · `request_upgrades_for` · `add_stock_rule` · `load_stock_rules_from_file` · `refresh_stock_orders` · `refresh_auto_convert_orders` · `set_default_gather_task` · `assign_default_gather_tasks` · `character_eligible` · `select_order_for` · `claim` · `release` · `verify` · `print_plan_tree` · `character_loop` · `held` · `complete`

**Lifecycle (stays on `TaskEngine`)**: `initialize()` · `_auto_convert_safety_sweep_loop()` · `_delivery_safety_sweep_loop()` · `run()` · `stop()` (**TODO task 12**: now also calls `order_manager.close()`/`scheduler.close()`/`executor.close()`/`config_watcher.close()` to unsubscribe every bus handler the four collaborators registered in their own `__init__`, in addition to setting `self.running = False`)

**`DELIVERY_SWEEP_MULTIPLIER`** / **`AUTO_CONVERT_SWEEP_MULTIPLIER`**
*(class constants, both = 20)*: how much less often `_delivery_safety_sweep_loop`/`_auto_convert_safety_sweep_loop` sweep vs. the old per-`poll_interval` polling loops they replaced (TODO tasks 7 & 8) -- both loops are now pure backstops behind the reactive `Executor`/`OrderManager` bus subscriptions, not the primary mechanism.

---

### `order_manager.py` — `OrderManager`, `StockRule`
**Order creation / expansion**
| Function | Purpose |
|---|---|
| `request_item(code, quantity, tier=None, requester=None, equip_slot=None, parent_id=None)` | Creates/bumps CRAFT or GATHER orders recursively; checks the bank first and skips straight to `engine.complete()` if bank stock already covers the requested quantity (no live-but-unworkable order left dangling). **The single place `OrderCreated`/`OrderUpdated`/`EquipmentRequested` get emitted on `engine.bus`** (TODO task 3) — new order → `OrderCreated`; bumping an existing order's `target_quantity` → `OrderUpdated`; a `requester`/`equip_slot` pair queued (new or bumped order) → `EquipmentRequested`. Every other method below that creates/bumps orders routes through here rather than emitting anything itself |
| `_bump_ingredients(craft_order, extra_output, tier)` | Cascades a target bump down to ingredient orders (emits nothing directly — via `request_item`) |
| `request_equipment(character_name, code, slot, quantity=1)` | `request_item` + equip-on-completion, forcing `Priority.EQUIP` across the *entire* expansion (top-level order + every recursive ingredient order) so equipping is high-priority and interrupts whatever the character/roster is currently doing (emits nothing directly — via `request_item`) |
| `request_upgrades_for(character)` | Wraps `GearList.for_upgrades`, wires up equip delivery |

**Keep-in-stock**
| Function | Purpose |
|---|---|
| `add_stock_rule(code, minimum)` | Appends one `StockRule` to `engine.stock_rules` |
| `refresh_stock_orders()` | Full (but bounded — only `engine.stock_rules`, never the whole order pool) sweep: calls `_maybe_queue_stock_order` for every registered rule. Used by startup (`TaskEngine.run`), the `ConfigChanged` reactive handler (`ConfigWatcher._on_config_changed`), and (indirectly, one rule at a time) the `StockBelowMinimum` reactive handler below |
| `_maybe_queue_stock_order(code, minimum)` | **TODO task 10**, extracted from `refresh_stock_orders` so a single code can be topped up without re-sweeping every other rule (mirrors `_maybe_auto_convert`'s extraction from `refresh_auto_convert_orders` for task 8). No-ops if `code` is already at/above `minimum` or a live order for it already exists; otherwise queues a `KEEP_STOCK`-tier order via `request_item` for the shortfall (emits nothing directly — via `request_item`) |
| `_check_stock_thresholds(event=None)` | **TODO task 10, reactive.** Subscribed to both `BankSynced` and `OrderCompleted` in `__init__` — the same two events tasks 7/8 already piggyback on, which between them fire at every point task 10 calls out (deposits, gathers completing, crafts completing). Bounded sweep over `engine.stock_rules`; emits `events.StockBelowMinimum(code, current, minimum)` for every rule currently under its floor. Takes an optional/ignored `event` arg so it can be used directly as the handler for either event type |
| `_on_stock_below_minimum(event)` | **TODO task 10, reactive.** Subscribed to `StockBelowMinimum`; narrows straight to `_maybe_queue_stock_order(event.code, event.minimum)` rather than re-sweeping every rule |
| `close()` | **TODO task 12.** Unsubscribes every handler registered in `__init__` (`_on_order_completed`, `_on_bank_synced`, `_check_stock_thresholds` x2, `_on_stock_below_minimum` -- tracked in `self._subscriptions`) from `engine.bus` -- called by `TaskEngine.stop()` |

> File-backed loading of stock rules (`load_stock_rules_from_file`, the periodic reload loop) moved to `config_watcher.py` — see below.

**Auto-convert (single-use gathered raw materials → their sole crafted product)**
| Function | Purpose |
|---|---|
| `_build_single_use_conversions()` | Cached scan of the item catalog: maps each default-gathered raw material's code to the one `Item` that consumes it, but only when it's used by exactly one recipe (e.g. `copper_ore`→`copper_bar`, `raw_chicken`→`cooked_chicken`) |
| `refresh_auto_convert_orders()` | Full (but bounded -- only the cached `_single_use_conversions` dict, never the whole order pool) sweep: calls `_maybe_auto_convert` for every candidate raw material. Used by startup (`TaskEngine.run`), the `BankSynced` reactive handler, and the low-frequency safety-sweep loop |
| `_maybe_auto_convert(raw_code)` | Per-candidate logic (TODO task 8, extracted from `refresh_auto_convert_orders` so a single code can be checked without re-scanning the rest): if `raw_code` is a single-use conversion candidate, queues a `Priority.AUTO_CRAFT` craft order to convert whatever's currently held above its keep-in-stock floor (`StockRule.minimum`, or 100 if unset) into the finished item — never dips below that floor, never duplicates an order already in flight for the target, silently no-ops for a non-candidate code (emits nothing directly — via `request_item`) |
| `_on_order_completed(event)` | **TODO task 8, reactive.** Narrows straight to `_maybe_auto_convert(event.code)` -- if the just-completed order's code isn't a conversion candidate this is a harmless no-op |
| `_on_bank_synced(event)` | **TODO task 8, reactive.** `BankSynced` carries no code, so this re-runs the full (but still bounded) `refresh_auto_convert_orders()` sweep |

**Default (fallback) tasks**
`set_default_gather_task(character_name, resource_code)` · `assign_default_gather_tasks()`

**Plan verification / debugging**
`verify()` · `print_plan_tree()`

---

### `scheduler.py` — `Scheduler`
**Eligibility / scoring**
`_craft_allowed(character, skill)` · `character_eligible(character, order)` · `_score(character, order)` · `_available_for_craft(character, code)` · `_materials_available(character, order)` · `select_order_for(character)`

**Claim / release / complete** (each also emits its matching lifecycle
event on `engine.bus` -- TODO task 4 -- right after updating
`order.claimed_by`/`locked_to`/`done`/`engine._current_order`, so a future
subscriber always sees post-mutation state. TODO task 12 audit: these
emits are safe even though claim/release/complete are typically called
while `character.busy_lock` is held upstream -- `EventBus.emit` is
fire-and-forget, so no subscriber runs synchronously inside that lock)
`claim(character, order)` (emits `OrderClaimed`) · `release(character, order)` (emits `OrderReleased`) · `complete(order)` (emits `OrderCompleted`; also reachable via `TaskEngine.complete`, a thin forward used by `order_manager.py`/`executor.py`)

`close()`: **TODO task 12.** Unsubscribes `_on_order_created`/`_on_order_updated`/`_on_order_released`/`_on_order_completed` from `engine.bus` (tracked in `self._subscriptions`, set at construction) -- called by `TaskEngine.stop()`.

**Event-driven idle wakeups** *(TODO task 5 -- done)*: `Scheduler.__init__`
subscribes to `OrderCreated`/`OrderUpdated`/`OrderReleased`/`OrderCompleted`
on `engine.bus`.
| Function | Purpose |
|---|---|
| `_wake_eligible(order)` | `.set()`s `work_available` on every roster character for whom `character_eligible(character, order)` is true (busy characters included -- harmless, just an extra `select_order_for()` call next time they idle) |
| `_wake_all()` | `.set()`s `work_available` on every roster character, no filtering |
| `_on_order_created(event)` / `_on_order_updated(event)` / `_on_order_released(event)` | Look up `engine.orders[event.order_id]`, call `_wake_eligible` if it still exists |
| `_on_order_completed(event)` | Calls `_wake_all()` -- a completion can free bank materials that unblock a craft order for characters unrelated to the one that just finished, so this over-approximates rather than trying to compute exactly who benefits |

**The live per-character loop**
| Function | Purpose |
|---|---|
| `character_loop(character)` *(async)* | Per-character infinite loop: pick order (`select_order_for`) → switch (`engine.executor._switch_task`) → act (`engine.executor._run_gather_step`/`_run_craft_step`) (holds `character.busy_lock`); when idle (`order is None` or the step raised), `await`s `character.work_available.wait()` bounded by `asyncio.wait_for(..., timeout=engine.poll_interval * Scheduler.IDLE_WAIT_FALLBACK_MULTIPLIER)` instead of unconditionally sleeping `poll_interval`, then clears the event -- TODO task 5 |

**`IDLE_WAIT_FALLBACK_MULTIPLIER`** *(class constant, = 10)*: bounds how
long `character_loop`'s idle wait can block past `poll_interval` if a
wakeup is somehow missed/mis-targeted; the event subscribers above are the
primary wake path, this is only the safety net.

---

### `executor.py` — `Executor`
| Function | Purpose |
|---|---|
| `close()` | **TODO task 12.** Unsubscribes `_on_equipment_requested`/`_on_bank_synced` from `engine.bus` (tracked in `self._subscriptions`, set at construction) -- called by `TaskEngine.stop()` |
| `_switch_task(character, new_order)` *(async)* | Handles claim/release (via `engine.scheduler`) + deposit-on-switch |
| `_try_deliver_equipment(order, *, already_locked=None)` *(async)* | Non-blocking re-entrancy guard (`order._delivering`, TODO task 12) around `_deliver_equipment_queue` -- a losing concurrent call for the same order returns immediately rather than blocking (see the function's own docstring for why blocking here can deadlock) |
| `_deliver_equipment_queue(order, *, already_locked)` *(async)* | **TODO task 12**, extracted from the old `_try_deliver_equipment` body. Pops queued `(character, slot)` requests one at a time, delivering each via `_deliver_one` -- inline (no lock re-acquire) if `char_name == already_locked` (the calling character already holds their own `busy_lock`), else under `async with requester.busy_lock`. Stops (leaves the entry queued) if `_deliver_one` returns `None` (couldn't resolve a bank) |
| `_deliver_one(requester, order, slot, map_db)` *(async)* | **TODO task 12**, extracted from the old `_try_deliver_equipment` body. The actual move-unequip-deposit-withdraw-equip sequence for one request; returns the old item's code (`""` if the slot was empty, `None` if no bank could be resolved -- the two are distinguished so the caller knows whether to stop) |
| `_run_gather_step(character, order)` *(async)* | One gather action + deposit-if-full/if-done; calls `_try_deliver_equipment(order, already_locked=character.name)` on completion (TODO task 12 -- see that param's docstring for why this can't be omitted) |
| `_craft_batch_size(character, item, crafts_needed)` | Bounds a craft batch by inventory space & available materials (via `engine.scheduler._available_for_craft`) |
| `_run_craft_step(character, order)` *(async)* | Withdraw → craft batch → deposit, bank-to-workshop-to-bank; same `already_locked=character.name` call on completion as `_run_gather_step` |

---

### `config_watcher.py` — `ConfigWatcher`
The only module in the scheduler stack that touches the filesystem —
decouples config I/O from `OrderManager`/`Scheduler`/`Executor` per the
refactor recommendation ("instead of having the scheduler poll the
filesystem directly...").

**TODO task 9, done:** `loop()` no longer unconditionally reparses
`stock_config.json` every ~10x `poll_interval`. It now does a cheap
`os.stat().st_mtime` check every `MTIME_CHECK_INTERVAL` seconds and only
emits `events.ConfigChanged` on `engine.bus` when the mtime has genuinely
moved; `ConfigWatcher.__init__` subscribes `_on_config_changed` to that
event, and that handler is the one place that does the actual
reparse-and-refresh work. This is the dependency-free path from the TODO's
task-9 open decision (no `watchdog` added) — see `events.py`'s status note
for the full before/after.

| Function | Purpose |
|---|---|
| `load_stock_rules_from_file(path="stock_config.json")` | Reads a JSON `{item_code: minimum}` object from `path` and REPLACES `engine.stock_rules` with it (additions/edits/removals all picked up on reload); remembers `path` on `self.path`; missing file/bad entries are no-ops/warnings, never a crash; also records the file's current mtime on `self._last_mtime` as the watch-loop baseline (TODO task 9) |
| `_get_mtime(path=None)` | Cheap `os.stat().st_mtime` read of `path` (or `self.path`); `None` if no path set or the file doesn't currently exist |
| `_on_config_changed(event)` | **TODO task 9, reactive.** Subscribed to `ConfigChanged` in `__init__`; does the actual `load_stock_rules_from_file` + `engine.order_manager.refresh_stock_orders()` work in reaction to a real file change |
| `loop()` *(async)* | **TODO task 9.** Sleeps `MTIME_CHECK_INTERVAL` (2s), then does a cheap `os.stat()` mtime check on `self.path`; emits `ConfigChanged` on `engine.bus` only if the mtime moved since the last check. No-ops (just sleeps) if `load_stock_rules_from_file` was never called (wired into `TaskEngine.run()` alongside the other background loops) |
| `close()` | **TODO task 12.** Unsubscribes `_on_config_changed` from `engine.bus` (tracked in `self._subscriptions`, set at construction) -- called by `TaskEngine.stop()` |

**`MTIME_CHECK_INTERVAL`** *(class constant, = 2.0 seconds)*: how often
`loop()` stats `stock_config.json` for a change — much shorter than the old
~10x-`poll_interval` full-reparse cadence is justifiable for, since `stat()`
only touches inode metadata and never reads/parses file content unless the
mtime has actually moved.

---

## `database/base_store.py` — `BaseStore`
Shared by every `*Store`.
| Function | Purpose |
|---|---|
| `_get_connection()` | New sqlite3 connection, row factory set |
| `_configure_database()` | Enables WAL mode |
| `get_metadata(key)` / `set_metadata(key, value, conn=None)` | Generic KV metadata table access |
| `count()` | Row count of `self.table_name` |
| `get_last_updated(key)` / `set_last_updated(key, timestamp, conn=None)` | TTL timestamp helpers |
| `is_cache_expired(last_updated_key)` | Empty table or TTL exceeded -- **TODO task 11, audited, left as-is.** Deliberately still time-based, not event-driven; see the function's own docstring and the "Change local DB caching/TTL" quick-index row below for why |
| `__getstate__` / `__setstate__` | Pickle safety (nulls `api`) |

---

## `database/map_store.py` — `MapStore`
| Function | Purpose |
|---|---|
| `_init_db()` / `_ensure_condition_columns(conn)` | Schema creation/migration |
| `sync_from_api(force=False)` | Pulls all map tiles, paginated |
| `_normalize_location(loc_input, layer=None)` | Coerces Position/Location/tuple/character → `(x, y, layer)` |
| `save_maps(maps_data)` | Upserts tiles incl. access/transition conditions |
| `get_walkable_tiles(layer=None)` | Set of non-blocked tiles |
| `get_transitions()` | `{(x,y,layer): (x,y,layer)}` transition map |
| `get_tile_conditions(location, layer=None)` | Raw access/transition condition lists for a tile |
| `check_conditions(character, conditions)` | Evaluates conditions against character attrs |
| `get_neighbors(current, walkable, transitions)` | Cardinal neighbors + transition target |
| `find_content(content_identifier, layer="overworld")` | First tile matching a content code/type |
| `find_all(content_identifier, layer=None)` | All matching tiles |
| `get_shortest_path(start, goal, layer="overworld")` | A* pathfinding over tiles+transitions |
| `find_closest(from_target, content_identifier, layer=None)` | Nearest matching tile (same-layer first, else pathfind) |

---

## `database/item_store.py` — `ItemStore`
| Function | Purpose |
|---|---|
| `_init_db()` | Schema creation |
| `sync_from_api(force=False)` | Pulls all items, paginated |
| `get_item(code)` | Raw dict by code |
| `get_by_type(item_type)` | All items of a type |
| `get_craftable_by_skill(skill, max_level=100)` | Items craftable with a skill up to a level |
| `get_recipe(code)` | An item's craft recipe dict |
| `get_item_obj(code)` | Typed `Item` |
| `get_all_items_obj()` | All items as typed `Item`s |
| `_conditions_met(character, conditions)` / `meets_conditions(character, conditions)` | Condition evaluation (internal + public wrapper) |
| `get_craftable_for_character(character)` | Items whose craft-skill requirement the character meets |
| `get_equipable_for_character(character)` | Equipment the character's level/conditions currently allow |

---

## `database/monster_store.py` — `MonsterStore`
`_init_db()` · `sync_from_api(force=False)` · `get_monster(code)` · `get_by_level_range(min_level=1, max_level=100)` · `get_monsters_dropping_item(item_code)`

---

## `database/resource_store.py` — `ResourceStore`
`_init_db()` · `sync_from_api(force=False)` · `get_resource(code)` · `get_resource_obj(code)` · `get_by_skill(skill, max_level=100)` · `get_resources_dropping_item(item_code)` · `find_best_for_item(item_code)`

---

## `database/task_store.py` — `TaskStore`
Persists in-progress `GearPlan` tasks (not TTL-cached).
`_init_db()` · `save_plan(plan_id, tasks)` · `load_plan(plan_id)` · `_row_to_task(row)` · `list_open_plans()` · `update_status(plan_id, task_id, status)` · `update_assignment(plan_id, task_id, character_name)` · `delete_plan(plan_id)` · `is_plan_complete(plan_id)`

---

## `database/database.py` — `GameDatabase`
`__init__(db_path, api=None, ttl_seconds=86400)` · `sync_all(force=False, concurrent=False)` · `__getstate__` / `__setstate__`

---

## `tests/test_events.py` — event-driven conversion smoke test (TODO task 13)
No test framework in this project (no pytest/unittest infra) -- this is a
plain `asyncio` script, run directly via `python3 tests/test_events.py`. It
constructs the REAL `Scheduler`/`Executor`/`OrderManager`/`ConfigWatcher`
classes against a minimal `FakeEngine` (only the shared state those four
collaborators actually read/write -- `bus`/`orders`/`stock_rules`/`account`/
`held()`/`_order_for_code()`, no real `Account`/`GameDatabase`/API), so the
reactive wiring under test is the real code; only the heavier bank/API/DB
mechanics each test doesn't care about are stubbed out per-test.

| Function | Purpose |
|---|---|
| `make_character(name, mining_level=5)` | Builds a real `character.Character` from a minimal raw-API-shaped dict (`api=None, map_db=None`) |
| `FakeEngine` | Minimal duck-typed `TaskEngine` stand-in -- see module docstring |
| `gather_emitted(bus, event)` | `await`s every task `EventBus.emit()` schedules (fire-and-forget, see `events.py`) so a test can assert on handler side effects immediately rather than racing them |
| `check(label, cond)` | Tiny assert-and-print helper (`[PASS]`/`[FAIL]`) since there's no test framework to report results |
| `test_scheduler_wakeups()` | TODO task 5 check: `OrderCreated` wakes only the eligible character's `work_available` (not an ineligible one); a timed idle-wait (mirroring `character_loop`'s) resolves promptly on `OrderReleased`, well under the `IDLE_WAIT_FALLBACK_MULTIPLIER` fallback timeout, proving the event path -- not the fallback -- is what woke it |
| `test_executor_reactive_delivery()` | TODO task 7 check: `EquipmentRequested`/`BankSynced` trigger `_try_deliver_equipment` (mocked to isolate the reactive wiring from real bank/API mechanics) only for orders that actually have pending `equip_requests`; an unknown `order_id` or a request-free order triggers nothing |
| `test_order_manager_reactive()` | TODO tasks 8 & 10 check: `OrderCompleted` narrows auto-convert to just the completed code (`_maybe_auto_convert`), `BankSynced` runs the bounded sweep (`refresh_auto_convert_orders`); `_check_stock_thresholds` (real) emits `StockBelowMinimum` only for stock rules currently under their floor, and `_on_stock_below_minimum` narrows to `_maybe_queue_stock_order` per code -- a rule at/above its floor emits nothing |
| `test_config_watcher_reactive()` | TODO task 9 check: a real temporary `stock_config.json`, edited mid-test, propagates via a real `os.stat().st_mtime` diff (`ConfigWatcher.MTIME_CHECK_INTERVAL` shortened for the test) into `ConfigChanged` -> reload + `refresh_stock_orders()`, with no reaction while the file is unchanged and no old-style timer-driven reparse |
| `main()` | Runs all four tests in sequence; any `check()` failure raises `AssertionError` and stops the script |

> Verified passing repeatably (no flakiness across repeated runs) in a
> sandboxed copy of the project before being added here.

---

## Quick "where do I...?" index

| I want to... | Look in |
|---|---|
| Add/change a raw API call | `client.py` |
| Change what happens after an action (state sync) | `CharacterActions.py` (`sync_character_state`) |
| Change movement/pathfinding | `database/map_store.py` (`get_shortest_path`, `find_closest`), `CharacterActions.smart_move` |
| Change equip/unequip payload shape | `CharacterActions.equip` / `.unequip` |
| Change scheduling/priority logic (who gets what order) | `scheduler.py` (`Scheduler.select_order_for`, `_score`, `character_eligible`); tier ordering itself lives in `orders.py` (`Priority` enum, `INERTIA_BONUS`) |
| Change how a claimed order is actually carried out (gather/craft mechanics) | `executor.py` (`Executor._run_gather_step`, `_run_craft_step`, `_craft_batch_size`) |
| Change single-use raw-material auto-conversion (e.g. copper_ore → copper_bar) | `order_manager.py` (`OrderManager._maybe_auto_convert`, `refresh_auto_convert_orders`, `_build_single_use_conversions`, reactive subscribers `_on_order_completed`/`_on_bank_synced`); safety-sweep backstop in `task_runner.py` (`TaskEngine._auto_convert_safety_sweep_loop`); tier value in `orders.py` (`Priority.AUTO_CRAFT`) |
| Change equip-request priority/interrupt behavior | `order_manager.py` (`OrderManager.request_equipment`, passes `tier=Priority.EQUIP`); delivery mechanics in `executor.py` (`Executor._try_deliver_equipment`); tier value in `orders.py` (`Priority.EQUIP`) |
| Change how upgrades are auto-detected | `planning.py` (`GearList.for_upgrades`, `item_score`) |
| Change craft/gather order expansion | `order_manager.py` (`OrderManager.request_item`) or `planning.py` (`GearList.resolve`) |
| Change character naming/roles | `roles.py` |
| Change rate-limit handling | `account.py` (`RateLimiter`, `classify_bucket`) |
| Change local DB caching/TTL | `database/base_store.py` (`is_cache_expired`) + the relevant `*_store.py` (`sync_from_api`). **TODO task 11 audit decision: intentionally left time-based, not converted to event-driven.** This TTL cache (default 24h, checked once at startup via `GameDatabase.sync_all()` in `main.py`) guards the *static game-content catalogs* (items/monsters/resources/maps) -- data that changes only when the game itself patches, unlike the four polling sites this TODO converts (character idling, order delivery, auto-convert, stock-config reload), which were re-deriving live *engine* state that changes every tick. No domain event on `engine.bus` plausibly means "the item catalog changed upstream," so there's nothing to subscribe to; see the docstring on `is_cache_expired` for the full reasoning |
| Set/tune keep-in-stock minimums per item | `stock_config.json` (project root, `{item_code: minimum}`) -- edit and save; picked up within `ConfigWatcher.MTIME_CHECK_INTERVAL` (2s) via an `os.stat().st_mtime` diff that emits `events.ConfigChanged`, no restart needed; loader is `config_watcher.py` (`ConfigWatcher.load_stock_rules_from_file`, triggered reactively by `ConfigWatcher._on_config_changed`); a rule's shortfall is also now caught reactively as inventory/bank state changes (deposits, gathers/crafts completing), not just on reload -- see `order_manager.py` (`OrderManager._check_stock_thresholds`, `_on_stock_below_minimum`, `_maybe_queue_stock_order`) |
| Change bank/pending-item sync | `account.py` (`Account.sync_bank`, `.sync_pending_items`) |
| Add a shared field/behavior across live `WorkOrder`s and persisted `PlanTask`s | `orders.py` (`SchedulableOrder` protocol) — read its docstring first re: why they aren't one class |
| Sanity-check the event-driven wiring after a change | `tests/test_events.py` — `python3 tests/test_events.py` (plain asyncio script, no framework); covers Scheduler wakeups, Executor delivery, OrderManager auto-convert/keep-in-stock, and ConfigWatcher reload, all reactively |
