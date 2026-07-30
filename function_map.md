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
| `__init__(raw_data, api=None, map_db=None)` | Parses raw API dict into `skills`/`stats`/`equipment`/`location`, builds `.actions` (`CharacterActions`), `action_lock`, `busy_lock` |
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
| `__getstate__` / `__setstate__` | Pickle safety (nulls `action_lock`/`busy_lock`, recreated on unpickle) |
| `__getattr__(name)` | Falls through to `self.actions.<name>` (e.g. `character.rest()`) |
| `__repr__` | Debug string |

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
| `sync_bank()` | `GET /my/bank` + paginated `/my/bank/items` |
| `sync_pending_items()` | Paginated `/my/pending_items` |
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

## `orders.py` — live work-order primitives (`WorkOrder`)
| Function | Purpose |
|---|---|
| `WorkOrder.base_priority` *(property)* | Int cast of `.priority` |

Enums: `OrderKind` (GATHER/CRAFT), `Priority` (DEFAULT/AUTO_CRAFT/KEEP_STOCK/GATHER/CRAFT/EQUIP, ascending). `EQUIP` (40) is the top tier, above `CRAFT` (30) by more than `INERTIA_BONUS` (5), so an equip request always outranks and interrupts whatever a character is currently doing — see `TaskEngine.request_equipment`. `AUTO_CRAFT` (5) sits just above `DEFAULT` (0) and below `KEEP_STOCK` (10) — see `TaskEngine.refresh_auto_convert_orders`. Constant: `INERTIA_BONUS`.

---

## `roles.py` — naming scheme + skill-role assignment
| Function | Purpose |
|---|---|
| `build_roles(character_names)` | Assigns `ROLE_TEMPLATES` positionally to actual roster names |
| `primary_owner_of(craft_skill, roles=DEFAULT_ROLES)` | Who "owns" a pure craft skill |
| `gather_rank(character_name, skill, roles=DEFAULT_ROLES)` | Tie-break rank for gather-skill preference |
| `ensure_naming_scheme(account, api, names=NAME_SCHEME)` | Renames/creates characters to match `NAME_SCHEME` (best-effort, membership-gated) |

---

## `task_runner.py` — `TaskEngine` (the live scheduler)
This is the biggest file; functions grouped by what they do.

**Holdings**
`held(code)` · `_order_for_code(code)`

**Order creation / expansion**
| Function | Purpose |
|---|---|
| `request_item(code, quantity, tier=None, requester=None, equip_slot=None, parent_id=None)` | Creates/bumps CRAFT or GATHER orders recursively; checks the bank first and skips straight to `complete()` if bank stock already covers the requested quantity (no live-but-unworkable order left dangling) |
| `_bump_ingredients(craft_order, extra_output, tier)` | Cascades a target bump down to ingredient orders |
| `request_equipment(character_name, code, slot, quantity=1)` | `request_item` + equip-on-completion, forcing `Priority.EQUIP` across the *entire* expansion (top-level order + every recursive ingredient order) so equipping is high-priority and interrupts whatever the character/roster is currently doing |
| `request_upgrades_for(character)` | Wraps `GearList.for_upgrades`, wires up equip delivery |

**Keep-in-stock**
| Function | Purpose |
|---|---|
| `add_stock_rule(code, minimum)` | Appends one `StockRule` in code |
| `load_stock_rules_from_file(path="stock_config.json")` | Reads a JSON `{item_code: minimum}` object from `path` and REPLACES `self.stock_rules` with it (additions/edits/removals all picked up on reload); remembers `path` on `self._stock_config_path`; missing file/bad entries are no-ops/warnings, never a crash |
| `refresh_stock_orders()` | Queues `KEEP_STOCK`-tier orders for whatever's currently below each rule's minimum |
| `_stock_config_loop()` *(async)* | Re-runs `load_stock_rules_from_file` + `refresh_stock_orders` on a timer (~10x `poll_interval`) while running, so editing `stock_config.json` takes effect without a restart; harmless no-op if the file was never loaded (wired into `run()` alongside `_delivery_loop`/`_auto_convert_loop`) |

**Auto-convert (single-use gathered raw materials → their sole crafted product)**
| Function | Purpose |
|---|---|
| `_build_single_use_conversions()` | Cached scan of the item catalog: maps each default-gathered raw material's code to the one `Item` that consumes it, but only when it's used by exactly one recipe (e.g. `copper_ore`→`copper_bar`, `raw_chicken`→`cooked_chicken`) |
| `refresh_auto_convert_orders()` | For each such raw material, queues a `Priority.AUTO_CRAFT` craft order to convert whatever's currently held above its keep-in-stock floor (`StockRule.minimum`, or 100 if unset) into the finished item — never dips below that floor, never duplicates an order already in flight for the target |
| `_auto_convert_loop()` *(async)* | Calls `refresh_auto_convert_orders()` once per `poll_interval` while running, so newly accumulated surplus keeps getting picked up (wired into `run()` alongside `_delivery_loop()`) |

**Default (fallback) tasks**
`set_default_gather_task(character_name, resource_code)` · `assign_default_gather_tasks()`

**Eligibility / scoring**
`_craft_allowed(character, skill)` · `character_eligible(character, order)` · `_score(character, order)` · `_available_for_craft(character, code)` · `_materials_available(character, order)` · `select_order_for(character)`

**Claim / release / complete**
`claim(character, order)` · `release(character, order)` · `complete(order)`

**Verification / debugging**
`verify()` · `print_plan_tree()`

**Action execution**
| Function | Purpose |
|---|---|
| `_switch_task(character, new_order)` | Handles claim/release + deposit-on-switch |
| `_try_deliver_equipment(order)` | Sends each queued recipient to the bank, unequips+deposits whatever's currently in that slot (if anything), withdraws the new item, and equips it there -- as bank stock allows (holds `requester.busy_lock`) |
| `_run_gather_step(character, order)` | One gather action + deposit-if-full/if-done |
| `_craft_batch_size(character, item, crafts_needed)` | Bounds a craft batch by inventory space & available materials |
| `_run_craft_step(character, order)` | Withdraw → craft batch → deposit, bank-to-workshop-to-bank |
| `character_loop(character)` | Per-character infinite loop: pick order → switch → act (holds `character.busy_lock`) |

**Lifecycle**
`initialize()` · `_delivery_loop()` · `_auto_convert_loop()` · `_stock_config_loop()` · `run()` · `stop()`

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
| `is_cache_expired(last_updated_key)` | Empty table or TTL exceeded |
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

## Quick "where do I...?" index

| I want to... | Look in |
|---|---|
| Add/change a raw API call | `client.py` |
| Change what happens after an action (state sync) | `CharacterActions.py` (`sync_character_state`) |
| Change movement/pathfinding | `database/map_store.py` (`get_shortest_path`, `find_closest`), `CharacterActions.smart_move` |
| Change equip/unequip payload shape | `CharacterActions.equip` / `.unequip` |
| Change scheduling/priority logic | `task_runner.py` (`select_order_for`, `_score`, `character_eligible`); tier ordering itself lives in `orders.py` (`Priority` enum, `INERTIA_BONUS`) |
| Change single-use raw-material auto-conversion (e.g. copper_ore → copper_bar) | `task_runner.py` (`TaskEngine.refresh_auto_convert_orders`, `_build_single_use_conversions`, `_auto_convert_loop`); tier value in `orders.py` (`Priority.AUTO_CRAFT`) |
| Change equip-request priority/interrupt behavior | `task_runner.py` (`TaskEngine.request_equipment`, passes `tier=Priority.EQUIP`); tier value in `orders.py` (`Priority.EQUIP`) |
| Change how upgrades are auto-detected | `planning.py` (`GearList.for_upgrades`, `item_score`) |
| Change craft/gather order expansion | `task_runner.py` (`request_item`) or `planning.py` (`GearList.resolve`) |
| Change character naming/roles | `roles.py` |
| Change rate-limit handling | `account.py` (`RateLimiter`, `classify_bucket`) |
| Change local DB caching/TTL | `database/base_store.py` + the relevant `*_store.py` |
| Set/tune keep-in-stock minimums per item | `stock_config.json` (project root, `{item_code: minimum}`) -- edit and it's picked up on the next `_stock_config_loop` tick, no restart needed; loader is `task_runner.py` (`TaskEngine.load_stock_rules_from_file`) |
| Change bank/pending-item sync | `account.py` (`Account.sync_bank`, `.sync_pending_items`) |
