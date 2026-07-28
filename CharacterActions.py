#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Jul 22 23:06:01 2026

@author: xylan
"""
import functools
from contextlib import asynccontextmanager
from typing import Union, Tuple, Optional, Callable, Any, AsyncGenerator, List

from models import Position, Location
from database import MapStore


def sync_character_state(func):
    """Decorator that automatically updates character state after an action API call."""
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        data = await func(self, *args, **kwargs)
        if isinstance(data, dict):
            self.character.update_from_dict(data)
        return data
    return wrapper


class CharacterActions:
    """Per-character action set. Created directly by Character.__init__ (one
    instance per character, bound to it, api, and map_db) so callers can do
    `xylan.actions.rest()` -- or `xylan.rest()` via Character's __getattr__
    fallback -- with no separate bind() step."""

    def __init__(self, character, api, map_db: Optional[MapStore] = None):
        self.character = character
        self.api = api
        self.map_db = map_db

    def __getstate__(self):
        """Prevents Spyder/pickle from crashing on the non-picklable api client.
        self.map_db (a MapStore) already nulls its own api reference via
        BaseStore.__getstate__, so this only needs to handle our own self.api."""
        state = self.__dict__.copy()
        state["api"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)

    def _normalize_target(
        self,
        target: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object]
    ) -> Tuple[int, int]:
        """Extracts (x, y) coordinates from various position/location types."""
        if hasattr(target, "position"):
            return target.position.x, target.position.y
        if hasattr(target, "x") and hasattr(target, "y"):
            return target.x, target.y
        if isinstance(target, (tuple, list)):
            return target[0], target[1]
        raise ValueError(f"Unrecognized target type: {type(target)}")

    def get_closest_bank(self, map_db: Optional[MapStore] = None) -> Optional[Tuple[int, int, str]]:
        """Uses find_closest to locate the nearest bank relative to the character."""
        active_db = map_db or self.map_db
        if not active_db:
            return None
        return active_db.find_closest(self.character, "bank")

    def is_at_bank(self, map_db: Optional[MapStore] = None) -> bool:
        """Checks if character's current position matches the closest bank position."""
        closest_bank = self.get_closest_bank(map_db)
        if not closest_bank:
            return False

        current_x = self.character.location.position.x
        current_y = self.character.location.position.y
        bank_x, bank_y = closest_bank[0], closest_bank[1]

        return current_x == bank_x and current_y == bank_y

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    @sync_character_state
    async def move_to(
        self,
        target: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object]
    ) -> dict:
        """Handles movement logic, API calling, and local state sync."""
        target_x, target_y = self._normalize_target(target)
        target_pos = Position(target_x, target_y)

        if self.character.is_at(target_pos):
            print(f"[{self.character.name}] Already at ({target_x}, {target_y}).")
            return {}

        return await self.api.move(self.character, target_x, target_y)

    @sync_character_state
    async def transition(self) -> dict:
        """Fires the transition action (doors, boats, caves, cross-layer moves, etc.).

        The character must already be standing on the tile that defines
        interactions.transition; the endpoint takes no body and uses whatever
        transition is attached to the character's current tile."""
        return await self.api.transition(self.character)

    # ------------------------------------------------------------------
    # Combat / rest
    # ------------------------------------------------------------------

    async def fight(
        self,
        target: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Executes a fight action for this character.

        If `target` is given (e.g. "chicken"), first resolves the closest
        matching tile via MapStore.find_closest and moves there via
        smart_move before firing the fight action. Without `target`,
        behaves exactly as before: fights on whatever tile the character
        currently occupies.
        """
        active_db = await self._navigate_to_content(target, map_db)
        if target and active_db is None:
            return {}
        return await self._execute_fight()

    @sync_character_state
    async def _execute_fight(self) -> dict:
        """Internal helper that fires the raw fight API call (wrapped with state sync)."""
        return await self.api.fight(self.character)

    @sync_character_state
    async def rest(self) -> dict:
        """Executes a rest action for this character."""
        return await self.api.rest(self.character)

    # ------------------------------------------------------------------
    # Bank: items (deposit/withdraw), gold, expansion
    # ------------------------------------------------------------------

    @sync_character_state
    async def _execute_deposit(self, items: list[dict]) -> dict:
        """Internal helper to fire the raw deposit API call (wrapped with state sync)."""
        return await self.api.bank_deposit_item(self.character, items)

    async def deposit_items(
        self,
        items: list[dict],
        map_db: Optional[MapStore] = None
    ) -> dict:
        """Deposits items. If not at closest bank, auto-relocates to nearest bank via find_closest and returns after."""
        if not items:
            return {}

        active_db = map_db or self.map_db

        # Case 1: Already at nearest bank — deposit immediately
        if self.is_at_bank(active_db):
            return await self._execute_deposit(items)

        # Case 2: Away from bank — move to closest bank, deposit, then return to starting position
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.character.name}] Unable to resolve bank position using find_closest!")
            return {}

        print(f"[{self.character.name}] Not at bank. Moving to nearest bank {closest_bank} to deposit and returning...")
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_deposit(items)

    async def deposit_all(self, map_db: Optional[MapStore] = None) -> dict:
        """Deposits all items currently in inventory (auto-relocates via find_closest and returns if not at bank)."""
        items_to_deposit = [
            {"code": item.code, "quantity": item.quantity}
            for item in self.character.inventory
            if item and item.code and item.quantity > 0
        ]

        if not items_to_deposit:
            print(f"[{self.character.name}] Inventory is already empty!")
            return {}

        print(f"[{self.character.name}] Depositing {len(items_to_deposit)} item types into the bank...")
        return await self.deposit_items(items_to_deposit, map_db=map_db)

    @sync_character_state
    async def _execute_deposit_gold(self, quantity: int) -> dict:
        return await self.api.bank_deposit_gold(self.character, quantity)

    async def deposit_gold(self, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Deposits gold. Auto-relocates to nearest bank and returns after, same as deposit_items."""
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_deposit_gold(quantity)
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_deposit_gold(quantity)

    @sync_character_state
    async def _execute_withdraw_items(self, items: list[dict]) -> dict:
        return await self.api.bank_withdraw_item(self.character, items)

    async def withdraw_items(
        self,
        items: list[dict],
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Withdraws items from the bank. items: [{'code':..., 'quantity':...}, ...].
        Auto-relocates to nearest bank and returns after, same as deposit_items."""
        if not items:
            return {}
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_withdraw_items(items)
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_withdraw_items(items)

    @sync_character_state
    async def _execute_withdraw_gold(self, quantity: int) -> dict:
        return await self.api.bank_withdraw_gold(self.character, quantity)

    async def withdraw_gold(self, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Withdraws gold. Auto-relocates to nearest bank and returns after."""
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_withdraw_gold(quantity)
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_withdraw_gold(quantity)

    @sync_character_state
    async def _execute_buy_bank_expansion(self) -> dict:
        return await self.api.bank_buy_expansion(self.character)

    async def buy_bank_expansion(self, map_db: Optional[MapStore] = None) -> dict:
        """Buys a 20-slot bank expansion. Auto-relocates to nearest bank and returns after."""
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_buy_bank_expansion()
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_buy_bank_expansion()

    # ------------------------------------------------------------------
    # Navigation helpers (used by fight/gather/craft/npc/GE/tasks)
    # ------------------------------------------------------------------

    async def smart_move(
        self,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        map_db: Optional[MapStore] = None
    ) -> dict:
        """Navigates character to destination using shortest path in MapStore."""
        active_db = map_db or self.map_db
        if not active_db:
            raise ValueError("No MapStore provided to smart_move!")

        target_x, target_y = self._normalize_target(destination)

        # Already there -- skip pathfinding entirely. get_shortest_path()
        # returns an empty path both when there's genuinely no route AND
        # when start == goal (A* terminates on the first pop with nothing in
        # came_from), so without this check "already at the destination"
        # was being logged and treated identically to "no route exists".
        current_x = self.character.location.position.x
        current_y = self.character.location.position.y
        if current_x == target_x and current_y == target_y:
            return {}

        path = active_db.get_shortest_path(self.character.location, destination)

        if not path:
            print(f"[{self.character.name}] No valid route found to ({target_x}, {target_y})!")
            return {}

        transitions = active_db.get_transitions()
        prev_node = (
            self.character.location.position.x,
            self.character.location.position.y,
            self.character.location.layer,
        )

        orders: List[Tuple[str, Tuple[int, int, str]]] = []
        for i, node in enumerate(path):
            is_last = i == len(path) - 1
            if transitions.get(prev_node) == node:
                orders.append(("move", prev_node))
                orders.append(("transition", prev_node))
            elif is_last:
                orders.append(("move", node))
            prev_node = node

        print(f"[{self.character.name}] Route planned ({len(orders)} orders) to target ({target_x}, {target_y}).")

        last_result: dict = {}
        for kind, node in orders:
            if kind == "move":
                last_result = await self.move_to(node)
            else:
                conditions = active_db.get_tile_conditions(node)["transition"]
                if not active_db.check_conditions(self.character, conditions):
                    print(
                        f"[{self.character.name}] Does not meet transition conditions at "
                        f"{node}: {conditions}. Aborting route."
                    )
                    return last_result
                print(f"[{self.character.name}] Using transition at {node}...")
                last_result = await self.transition()

        return last_result

    @asynccontextmanager
    async def temporary_relocate(
        self,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        map_db: Optional[MapStore] = None
    ) -> AsyncGenerator[None, None]:
        """Context manager: Remembers starting spot, moves to destination, yields, then returns back."""
        active_db = map_db or self.map_db
        starting_pos = (
            self.character.location.position.x,
            self.character.location.position.y,
            self.character.location.layer
        )

        try:
            await self.smart_move(destination=destination, map_db=active_db)
            yield
        finally:
            print(f"[{self.character.name}] Task complete. Returning to origin {starting_pos}...")
            await self.smart_move(destination=starting_pos, map_db=active_db)

    async def run_and_return(
        self,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        action_coro: Callable[..., Any],
        *args,
        map_db: Optional[MapStore] = None,
        **kwargs
    ) -> Any:
        """Helper to run a single async function at destination and return back to origin."""
        async with self.temporary_relocate(destination, map_db=map_db):
            return await action_coro(*args, **kwargs)

    async def _navigate_to_content(
        self,
        content_identifier: Optional[str],
        map_db: Optional[MapStore] = None,
    ) -> Optional[MapStore]:
        """Resolves the closest tile matching content_identifier and moves there via smart_move."""
        active_db = map_db or self.map_db

        if not content_identifier:
            return active_db

        if not active_db:
            raise ValueError("No MapStore provided for target resolution!")

        destination = active_db.find_closest(self.character, content_identifier)
        if not destination:
            print(f"[{self.character.name}] Could not resolve '{content_identifier}' via find_closest!")
            return None

        await self.smart_move(destination, map_db=active_db)
        return active_db

    # ------------------------------------------------------------------
    # Gathering / Crafting / Recycling
    # ------------------------------------------------------------------

    async def gather(
        self,
        resource: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Harvests the resource node on the character's current tile."""
        active_db = await self._navigate_to_content(resource, map_db)
        if resource and active_db is None:
            return {}
        return await self._execute_gather()

    @sync_character_state
    async def _execute_gather(self) -> dict:
        return await self.api.gathering(self.character)

    async def craft(
        self,
        code: str,
        quantity: int = 1,
        workshop: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Crafts `code` x `quantity`."""
        active_db = await self._navigate_to_content(workshop, map_db)
        if workshop and active_db is None:
            return {}
        return await self._execute_craft(code, quantity)

    @sync_character_state
    async def _execute_craft(self, code: str, quantity: int = 1) -> dict:
        return await self.api.crafting(self.character, code, quantity)

    async def recycle(
        self,
        code: str,
        quantity: int = 1,
        enhanced: bool = False,
        workshop: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Recycles equipment/weapons back into resources."""
        active_db = await self._navigate_to_content(workshop, map_db)
        if workshop and active_db is None:
            return {}
        return await self._execute_recycle(code, quantity, enhanced)

    @sync_character_state
    async def _execute_recycle(self, code: str, quantity: int = 1, enhanced: bool = False) -> dict:
        return await self.api.recycling(self.character, code, quantity, enhanced)

    # ------------------------------------------------------------------
    # Equipment
    # ------------------------------------------------------------------

    @sync_character_state
    async def equip_items(self, items: List[dict]) -> dict:
        """Equips multiple items at once.
        items: [{'code': ..., 'slot': ..., 'quantity': 1}, ...]"""
        return await self.api.equip(self.character, items)

    async def equip(self, code: str, slot: str, quantity: int = 1) -> dict:
        """Convenience wrapper for equipping a single item."""
        payload = {"code": code, "slot": slot, "quantity": quantity}
        return await self.equip_items([payload])

    @sync_character_state
    async def unequip_items(self, slots: List[dict]) -> dict:
        """Unequips multiple slots at once."""
        return await self.api.unequip(self.character, slots)

    async def unequip(self, slot: str, quantity: int = 1) -> dict:
        """Convenience wrapper for unequipping a single slot."""
        payload = {"slot": slot, "quantity": quantity}
        return await self.unequip_items([payload])

    @sync_character_state
    async def use_item(self, code: str, quantity: int = 1) -> dict:
        """Uses a consumable item (e.g. food, potions) from inventory."""
        return await self.api.use(self.character, code, quantity)

    # ------------------------------------------------------------------
    # NPC trading
    # ------------------------------------------------------------------

    async def npc_buy(
        self,
        code: str,
        quantity: int,
        npc: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Buys an item from an NPC on the character's map."""
        active_db = await self._navigate_to_content(npc, map_db)
        if npc and active_db is None:
            return {}
        return await self._execute_npc_buy(code, quantity)

    @sync_character_state
    async def _execute_npc_buy(self, code: str, quantity: int) -> dict:
        return await self.api.npc_buy(self.character, code, quantity)

    async def npc_sell(
        self,
        code: str,
        quantity: int,
        npc: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Sells an item to an NPC on the character's map."""
        active_db = await self._navigate_to_content(npc, map_db)
        if npc and active_db is None:
            return {}
        return await self._execute_npc_sell(code, quantity)

    @sync_character_state
    async def _execute_npc_sell(self, code: str, quantity: int) -> dict:
        return await self.api.npc_sell(self.character, code, quantity)

    # ------------------------------------------------------------------
    # Grand Exchange
    # ------------------------------------------------------------------

    async def _at_grand_exchange(self, map_db: Optional[MapStore] = None) -> Optional[MapStore]:
        return await self._navigate_to_content("grand_exchange", map_db)

    async def ge_buy(self, order_id: str, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Buys (partially or fully) from an existing sell order. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_buy(order_id, quantity)

    @sync_character_state
    async def _execute_ge_buy(self, order_id: str, quantity: int) -> dict:
        return await self.api.ge_buy(self.character, order_id, quantity)

    async def ge_create_sell_order(
        self, code: str, quantity: int, price: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Creates a sell order at the Grand Exchange. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_create_sell_order(code, quantity, price)

    @sync_character_state
    async def _execute_ge_create_sell_order(self, code: str, quantity: int, price: int) -> dict:
        return await self.api.ge_create_sell_order(self.character, code, quantity, price)

    async def ge_create_buy_order(
        self, code: str, quantity: int, price: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Creates a buy order at the Grand Exchange. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_create_buy_order(code, quantity, price)

    @sync_character_state
    async def _execute_ge_create_buy_order(self, code: str, quantity: int, price: int) -> dict:
        return await self.api.ge_create_buy_order(self.character, code, quantity, price)

    async def ge_fill_buy_order(self, order_id: str, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Sells items directly into someone else's buy order. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_fill_buy_order(order_id, quantity)

    @sync_character_state
    async def _execute_ge_fill_buy_order(self, order_id: str, quantity: int) -> dict:
        return await self.api.ge_fill_buy_order(self.character, order_id, quantity)

    async def ge_cancel_order(self, order_id: str, map_db: Optional[MapStore] = None) -> dict:
        """Cancels one of your own orders (sell or buy). Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_cancel_order(order_id)

    @sync_character_state
    async def _execute_ge_cancel_order(self, order_id: str) -> dict:
        return await self.api.ge_cancel_order(self.character, order_id)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def _at_tasks_master(
        self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> Optional[MapStore]:
        return await self._navigate_to_content(tasks_master, map_db)

    async def task_new(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Accepts a new task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_new()

    @sync_character_state
    async def _execute_task_new(self) -> dict:
        return await self.api.task_new(self.character)

    async def task_complete(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Completes the character's current (finished) task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_complete()

    @sync_character_state
    async def _execute_task_complete(self) -> dict:
        return await self.api.task_complete(self.character)

    async def task_cancel(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Cancels the current task for 1 task coin. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_cancel()

    @sync_character_state
    async def _execute_task_cancel(self) -> dict:
        return await self.api.task_cancel(self.character)

    async def task_exchange(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Exchanges 6 task coins for a random reward. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_exchange()

    @sync_character_state
    async def _execute_task_exchange(self) -> dict:
        return await self.api.task_exchange(self.character)

    async def task_trade(
        self,
        code: str,
        quantity: int,
        tasks_master: str = "tasks_master",
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Trades items toward the character's current item-type task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_trade(code, quantity)

    @sync_character_state
    async def _execute_task_trade(self, code: str, quantity: int) -> dict:
        return await self.api.task_trade(self.character, code, quantity)

    # ------------------------------------------------------------------
    # Give (character-to-character, same tile), pending items, misc
    # ------------------------------------------------------------------

    @sync_character_state
    async def give_gold(self, quantity: int, to_character: str) -> dict:
        """Gives gold to another of your characters standing on the same tile."""
        return await self.api.give_gold(self.character, quantity, to_character)

    @sync_character_state
    async def give_items(self, items: list[dict], to_character: str) -> dict:
        """Gives items to another of your characters standing on the same tile."""
        return await self.api.give_items(self.character, items, to_character)

    @sync_character_state
    async def claim_pending_item(self, pending_item_id: str) -> dict:
        """Claims a pending item (achievement/GE/event reward) into this character's inventory."""
        return await self.api.claim_item(self.character, pending_item_id)

    @sync_character_state
    async def delete_item(self, code: str, quantity: int) -> dict:
        """Permanently deletes an item from inventory. No confirmation -- use with care."""
        return await self.api.delete_item(self.character, code, quantity)

    @sync_character_state
    async def change_skin(self, skin: str) -> dict:
        """Changes the character's skin to one the account owns."""
        return await self.api.change_skin(self.character, skin)

    @sync_character_state
    async def rename(self, new_name: str) -> dict:
        """Renames the character (members only)."""
        return await self.api.rename_character(self.character, new_name)