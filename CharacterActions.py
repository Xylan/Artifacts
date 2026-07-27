#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Jul 22 23:06:01 2026

@author: xylan
"""
import functools
from contextlib import asynccontextmanager
from typing import Union, Tuple, Optional, Callable, Any, AsyncGenerator, List

from character import Character
from models import Position, Location
from database import MapStore


def sync_character_state(func):
    """Decorator that automatically updates character state after an action API call."""
    @functools.wraps(func)
    async def wrapper(self, character, *args, **kwargs):
        # 1. Run the original action method (e.g. move_to, fight, rest)
        data = await func(self, character, *args, **kwargs)

        # 2. Automatically sync state if response data was returned
        if isinstance(data, dict):
            character.update_from_dict(data)

        return data
    return wrapper


class BoundActions:
    """Per-character view over a shared CharacterActions instance, so callers
    can do `xylan.actions.rest()` instead of `actions.rest(xylan)`. Built via
    CharacterActions.bind(character) -- see Account.sync_characters().

    Generic on purpose: forwards any CharacterActions method by partially
    applying `character` as the first argument, so it never needs updating
    when new action methods are added."""

    def __init__(self, character: Character, shared_actions: "CharacterActions"):
        object.__setattr__(self, "_character", character)
        object.__setattr__(self, "_shared", shared_actions)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._shared, name)
        if not callable(attr):
            return attr
        return functools.partial(attr, self._character)

    def __repr__(self) -> str:
        return f"<BoundActions character={self._character.name!r}>"


class CharacterActions:

    def __init__(self, api, map_db: Optional[MapStore] = None):
        self.api = api
        self.map_db = map_db

    def bind(self, character: Character) -> BoundActions:
        """Returns a per-character proxy: bound.rest() == self.rest(character)."""
        return BoundActions(character, self)

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

    def get_closest_bank(
        self,
        character: Character,
        map_db: Optional[MapStore] = None
    ) -> Optional[Tuple[int, int, str]]:
        """Uses find_closest to locate the nearest bank relative to the character."""
        active_db = map_db or self.map_db
        if not active_db:
            return None
        return active_db.find_closest(character, "bank")

    def is_at_bank(self, character: Character, map_db: Optional[MapStore] = None) -> bool:
        """Checks if character's current position matches the closest bank position."""
        closest_bank = self.get_closest_bank(character, map_db)
        if not closest_bank:
            return False

        current_x = character.location.position.x
        current_y = character.location.position.y
        bank_x, bank_y = closest_bank[0], closest_bank[1]

        return current_x == bank_x and current_y == bank_y

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    @sync_character_state
    async def move_to(
        self,
        character: Character,
        target: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object]
    ) -> dict:
        """Handles movement logic, API calling, and local state sync."""
        target_x, target_y = self._normalize_target(target)
        target_pos = Position(target_x, target_y)

        if character.is_at(target_pos):
            print(f"[{character.name}] Already at ({target_x}, {target_y}).")
            return {}

        return await self.api.move(character, target_x, target_y)

    @sync_character_state
    async def transition(self, character: Character) -> dict:
        """Fires the transition action (doors, boats, caves, cross-layer moves, etc.).

        The character must already be standing on the tile that defines
        interactions.transition; the endpoint takes no body and uses whatever
        transition is attached to the character's current tile."""
        return await self.api.transition(character)

    # ------------------------------------------------------------------
    # Combat / rest
    # ------------------------------------------------------------------

    async def fight(
        self,
        character: Character,
        target: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Executes a fight action for the given character.

        If `target` is given (e.g. "chicken"), first resolves the closest
        matching tile via MapStore.find_closest and moves there via
        smart_move before firing the fight action. Without `target`,
        behaves exactly as before: fights on whatever tile the character
        currently occupies.
        """
        active_db = await self._navigate_to_content(character, target, map_db)
        if target and active_db is None:
            return {}
        return await self._execute_fight(character)

    @sync_character_state
    async def _execute_fight(self, character: Character) -> dict:
        """Internal helper that fires the raw fight API call (wrapped with state sync)."""
        return await self.api.fight(character)

    @sync_character_state
    async def rest(self, character: Character) -> dict:
        """Executes a rest action for the given character."""
        return await self.api.rest(character)

    # ------------------------------------------------------------------
    # Bank: items (deposit/withdraw), gold, expansion
    # ------------------------------------------------------------------

    @sync_character_state
    async def _execute_deposit(self, character: Character, items: list[dict]) -> dict:
        """Internal helper to fire the raw deposit API call (wrapped with state sync)."""
        return await self.api.bank_deposit_item(character, items)

    async def deposit_items(
        self,
        character: Character,
        items: list[dict],
        map_db: Optional[MapStore] = None
    ) -> dict:
        """Deposits items. If not at closest bank, auto-relocates to nearest bank via find_closest and returns after."""
        if not items:
            return {}

        active_db = map_db or self.map_db
        closest_bank = self.get_closest_bank(character, active_db)

        # Case 1: Already at nearest bank — deposit immediately
        if self.is_at_bank(character, active_db):
            return await self._execute_deposit(character, items)

        # Case 2: Away from bank — move to closest bank, deposit, then return to starting position
        if not closest_bank:
            print(f"[{character.name}] Unable to resolve bank position using find_closest!")
            return {}

        print(f"[{character.name}] Not at bank. Moving to nearest bank {closest_bank} to deposit and returning...")
        async with self.temporary_relocate(character, destination=closest_bank, map_db=active_db):
            return await self._execute_deposit(character, items)

    async def deposit_all(
        self,
        character: Character,
        map_db: Optional[MapStore] = None
    ) -> dict:
        """Deposits all items currently in inventory (auto-relocates via find_closest and returns if not at bank)."""
        items_to_deposit = [
            {"code": item.code, "quantity": item.quantity}
            for item in character.inventory
            if item and item.code and item.quantity > 0
        ]

        if not items_to_deposit:
            print(f"[{character.name}] Inventory is already empty!")
            return {}

        print(f"[{character.name}] Depositing {len(items_to_deposit)} item types into the bank...")
        return await self.deposit_items(character, items_to_deposit, map_db=map_db)

    @sync_character_state
    async def _execute_deposit_gold(self, character: Character, quantity: int) -> dict:
        return await self.api.bank_deposit_gold(character, quantity)

    async def deposit_gold(
        self, character: Character, quantity: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Deposits gold. Auto-relocates to nearest bank and returns after, same as deposit_items."""
        active_db = map_db or self.map_db
        if self.is_at_bank(character, active_db):
            return await self._execute_deposit_gold(character, quantity)
        closest_bank = self.get_closest_bank(character, active_db)
        if not closest_bank:
            print(f"[{character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(character, destination=closest_bank, map_db=active_db):
            return await self._execute_deposit_gold(character, quantity)

    @sync_character_state
    async def _execute_withdraw_items(self, character: Character, items: list[dict]) -> dict:
        return await self.api.bank_withdraw_item(character, items)

    async def withdraw_items(
        self,
        character: Character,
        items: list[dict],
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Withdraws items from the bank. items: [{'code':..., 'quantity':...}, ...].
        Auto-relocates to nearest bank and returns after, same as deposit_items."""
        if not items:
            return {}
        active_db = map_db or self.map_db
        if self.is_at_bank(character, active_db):
            return await self._execute_withdraw_items(character, items)
        closest_bank = self.get_closest_bank(character, active_db)
        if not closest_bank:
            print(f"[{character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(character, destination=closest_bank, map_db=active_db):
            return await self._execute_withdraw_items(character, items)

    @sync_character_state
    async def _execute_withdraw_gold(self, character: Character, quantity: int) -> dict:
        return await self.api.bank_withdraw_gold(character, quantity)

    async def withdraw_gold(
        self, character: Character, quantity: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Withdraws gold. Auto-relocates to nearest bank and returns after."""
        active_db = map_db or self.map_db
        if self.is_at_bank(character, active_db):
            return await self._execute_withdraw_gold(character, quantity)
        closest_bank = self.get_closest_bank(character, active_db)
        if not closest_bank:
            print(f"[{character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(character, destination=closest_bank, map_db=active_db):
            return await self._execute_withdraw_gold(character, quantity)

    @sync_character_state
    async def _execute_buy_bank_expansion(self, character: Character) -> dict:
        return await self.api.bank_buy_expansion(character)

    async def buy_bank_expansion(
        self, character: Character, map_db: Optional[MapStore] = None
    ) -> dict:
        """Buys a 20-slot bank expansion. Auto-relocates to nearest bank and returns after."""
        active_db = map_db or self.map_db
        if self.is_at_bank(character, active_db):
            return await self._execute_buy_bank_expansion(character)
        closest_bank = self.get_closest_bank(character, active_db)
        if not closest_bank:
            print(f"[{character.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(character, destination=closest_bank, map_db=active_db):
            return await self._execute_buy_bank_expansion(character)

    # ------------------------------------------------------------------
    # Navigation helpers (used by fight/gather/craft/npc/GE/tasks)
    # ------------------------------------------------------------------

    async def smart_move(
        self,
        character: Character,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        map_db: Optional[MapStore] = None
    ) -> dict:
        """Navigates character to destination using shortest path in MapStore.

        move_to() moves directly to any (x, y), so there's no need to call it once
        per cardinal tile in the path. Only two kinds of stops actually require an
        API call: transition-source tiles (the character must be physically standing
        on one to fire it) and the final destination. Plain cardinal tiles in between
        are skipped and covered implicitly by the next real move_to() call.
        """
        active_db = map_db or self.map_db
        if not active_db:
            raise ValueError("No MapStore provided to smart_move!")

        target_x, target_y = self._normalize_target(destination)

        path = active_db.get_shortest_path(character.location, destination)

        if not path:
            print(f"[{character.name}] No valid route found to ({target_x}, {target_y})!")
            return {}

        transitions = active_db.get_transitions()
        prev_node = (
            character.location.position.x,
            character.location.position.y,
            character.location.layer,
        )

        # Reduce the raw tile-by-tile path down to the orders that actually need to
        # be issued: a move onto each transition-source tile plus the transition
        # itself, and one final move to the destination. Plain cardinal tiles that
        # aren't a transition or the destination require no order at all.
        orders: List[Tuple[str, Tuple[int, int, str]]] = []
        for i, node in enumerate(path):
            is_last = i == len(path) - 1
            if transitions.get(prev_node) == node:
                orders.append(("move", prev_node))
                orders.append(("transition", prev_node))
            elif is_last:
                orders.append(("move", node))
            prev_node = node

        print(f"[{character.name}] Route planned ({len(orders)} orders) to target ({target_x}, {target_y}).")

        last_result: dict = {}
        for kind, node in orders:
            if kind == "move":
                last_result = await self.move_to(character, node)
            else:
                conditions = active_db.get_tile_conditions(node)["transition"]
                if not active_db.check_conditions(character, conditions):
                    print(
                        f"[{character.name}] Does not meet transition conditions at "
                        f"{node}: {conditions}. Aborting route."
                    )
                    return last_result
                print(f"[{character.name}] Using transition at {node}...")
                last_result = await self.transition(character)

        return last_result

    @asynccontextmanager
    async def temporary_relocate(
        self,
        character: Character,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        map_db: Optional[MapStore] = None
    ) -> AsyncGenerator[None, None]:
        """Context manager: Remembers starting spot, moves to destination, yields, then returns back."""
        active_db = map_db or self.map_db
        starting_pos = (
            character.location.position.x,
            character.location.position.y,
            character.location.layer
        )

        try:
            await self.smart_move(character, destination=destination, map_db=active_db)
            yield
        finally:
            print(f"[{character.name}] Task complete. Returning to origin {starting_pos}...")
            await self.smart_move(character, destination=starting_pos, map_db=active_db)

    async def run_and_return(
        self,
        character: Character,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        action_coro: Callable[..., Any],
        *args,
        map_db: Optional[MapStore] = None,
        **kwargs
    ) -> Any:
        """Helper to run a single async function at destination and return back to origin."""
        async with self.temporary_relocate(character, destination, map_db=map_db):
            return await action_coro(*args, **kwargs)

    async def _navigate_to_content(
        self,
        character: Character,
        content_identifier: Optional[str],
        map_db: Optional[MapStore] = None,
    ) -> Optional[MapStore]:
        """Resolves the closest tile matching content_identifier (content_code
        or content_type) and moves the character there via smart_move.
        Returns the active MapStore used (for chaining), or None if
        navigation failed. If content_identifier is falsy, this is a no-op
        and assumes the character is already positioned correctly."""
        active_db = map_db or self.map_db

        if not content_identifier:
            return active_db

        if not active_db:
            raise ValueError("No MapStore provided for target resolution!")

        destination = active_db.find_closest(character, content_identifier)
        if not destination:
            print(f"[{character.name}] Could not resolve '{content_identifier}' via find_closest!")
            return None

        await self.smart_move(character, destination, map_db=active_db)
        return active_db

    # ------------------------------------------------------------------
    # Gathering / Crafting / Recycling
    # ------------------------------------------------------------------

    async def gather(
        self,
        character: Character,
        resource: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Harvests the resource node on the character's current tile.
        If `resource` is given (a resource code like 'copper_rocks' or a
        skill name like 'mining'), moves to the closest matching tile first."""
        active_db = await self._navigate_to_content(character, resource, map_db)
        if resource and active_db is None:
            return {}
        return await self._execute_gather(character)

    @sync_character_state
    async def _execute_gather(self, character: Character) -> dict:
        return await self.api.gathering(character)

    async def craft(
        self,
        character: Character,
        code: str,
        quantity: int = 1,
        workshop: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Crafts `code` x `quantity`. The character must be on the matching
        workshop tile -- pass `workshop` (e.g. the craft's skill name, like
        'weaponcrafting') to auto-navigate there first, or omit it if the
        character is already positioned."""
        active_db = await self._navigate_to_content(character, workshop, map_db)
        if workshop and active_db is None:
            return {}
        return await self._execute_craft(character, code, quantity)

    @sync_character_state
    async def _execute_craft(self, character: Character, code: str, quantity: int = 1) -> dict:
        return await self.api.crafting(character, code, quantity)

    async def recycle(
        self,
        character: Character,
        code: str,
        quantity: int = 1,
        enhanced: bool = False,
        workshop: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Recycles equipment/weapons back into resources. Requires a
        workshop tile, same as craft()."""
        active_db = await self._navigate_to_content(character, workshop, map_db)
        if workshop and active_db is None:
            return {}
        return await self._execute_recycle(character, code, quantity, enhanced)

    @sync_character_state
    async def _execute_recycle(
        self, character: Character, code: str, quantity: int = 1, enhanced: bool = False
    ) -> dict:
        return await self.api.recycling(character, code, quantity, enhanced)

    # ------------------------------------------------------------------
    # Equipment
    # ------------------------------------------------------------------
    # Valid slots (ItemSlot enum): weapon, shield, helmet, body_armor,
    # leg_armor, boots, ring1, ring2, amulet, artifact1, artifact2,
    # artifact3, utility1, utility2, bag, rune

    @sync_character_state
    async def equip_items(self, character: Character, items: List[dict]) -> dict:
        """Equips multiple items at once.
        items: [{'code': ..., 'slot': ..., 'quantity': 1}, ...]
        ('quantity' only matters for utility slots.)"""
        return await self.api.equip(character, items)

    async def equip(self, character: Character, code: str, slot: str, quantity: int = 1) -> dict:
        """Convenience wrapper for equipping a single item."""
        payload = {"code": code, "slot": slot, "quantity": quantity}
        return await self.equip_items(character, [payload])

    @sync_character_state
    async def unequip_items(self, character: Character, slots: List[dict]) -> dict:
        """Unequips multiple slots at once.
        slots: [{'slot': ..., 'quantity': 1}, ...]"""
        return await self.api.unequip(character, slots)

    async def unequip(self, character: Character, slot: str, quantity: int = 1) -> dict:
        """Convenience wrapper for unequipping a single slot."""
        payload = {"slot": slot, "quantity": quantity}
        return await self.unequip_items(character, [payload])

    @sync_character_state
    async def use_item(self, character: Character, code: str, quantity: int = 1) -> dict:
        """Uses a consumable item (e.g. food, potions) from inventory."""
        return await self.api.use(character, code, quantity)

    # ------------------------------------------------------------------
    # NPC trading
    # ------------------------------------------------------------------

    async def npc_buy(
        self,
        character: Character,
        code: str,
        quantity: int,
        npc: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Buys an item from an NPC on the character's map. Pass `npc` (an
        NPC code, e.g. 'nomadic_merchant') to auto-navigate there first."""
        active_db = await self._navigate_to_content(character, npc, map_db)
        if npc and active_db is None:
            return {}
        return await self._execute_npc_buy(character, code, quantity)

    @sync_character_state
    async def _execute_npc_buy(self, character: Character, code: str, quantity: int) -> dict:
        return await self.api.npc_buy(character, code, quantity)

    async def npc_sell(
        self,
        character: Character,
        code: str,
        quantity: int,
        npc: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Sells an item to an NPC on the character's map. Pass `npc` to auto-navigate first."""
        active_db = await self._navigate_to_content(character, npc, map_db)
        if npc and active_db is None:
            return {}
        return await self._execute_npc_sell(character, code, quantity)

    @sync_character_state
    async def _execute_npc_sell(self, character: Character, code: str, quantity: int) -> dict:
        return await self.api.npc_sell(character, code, quantity)

    # ------------------------------------------------------------------
    # Grand Exchange
    # ------------------------------------------------------------------

    async def _at_grand_exchange(
        self, character: Character, map_db: Optional[MapStore] = None
    ) -> Optional[MapStore]:
        return await self._navigate_to_content(character, "grand_exchange", map_db)

    async def ge_buy(
        self, character: Character, order_id: str, quantity: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Buys (partially or fully) from an existing sell order. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(character, map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_buy(character, order_id, quantity)

    @sync_character_state
    async def _execute_ge_buy(self, character: Character, order_id: str, quantity: int) -> dict:
        return await self.api.ge_buy(character, order_id, quantity)

    async def ge_create_sell_order(
        self,
        character: Character,
        code: str,
        quantity: int,
        price: int,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Creates a sell order at the Grand Exchange. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(character, map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_create_sell_order(character, code, quantity, price)

    @sync_character_state
    async def _execute_ge_create_sell_order(
        self, character: Character, code: str, quantity: int, price: int
    ) -> dict:
        return await self.api.ge_create_sell_order(character, code, quantity, price)

    async def ge_create_buy_order(
        self,
        character: Character,
        code: str,
        quantity: int,
        price: int,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Creates a buy order at the Grand Exchange (locks price*quantity gold
        immediately). Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(character, map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_create_buy_order(character, code, quantity, price)

    @sync_character_state
    async def _execute_ge_create_buy_order(
        self, character: Character, code: str, quantity: int, price: int
    ) -> dict:
        return await self.api.ge_create_buy_order(character, code, quantity, price)

    async def ge_fill_buy_order(
        self, character: Character, order_id: str, quantity: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Sells items directly into someone else's buy order. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(character, map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_fill_buy_order(character, order_id, quantity)

    @sync_character_state
    async def _execute_ge_fill_buy_order(
        self, character: Character, order_id: str, quantity: int
    ) -> dict:
        return await self.api.ge_fill_buy_order(character, order_id, quantity)

    async def ge_cancel_order(
        self, character: Character, order_id: str, map_db: Optional[MapStore] = None
    ) -> dict:
        """Cancels one of your own orders (sell or buy). Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(character, map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_cancel_order(character, order_id)

    @sync_character_state
    async def _execute_ge_cancel_order(self, character: Character, order_id: str) -> dict:
        return await self.api.ge_cancel_order(character, order_id)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    # Defaults to the 'tasks_master' content_type; pass e.g. 'monsters_task_master'
    # or 'items_task_master' explicitly if your map has more than one kind.

    async def _at_tasks_master(
        self, character: Character, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> Optional[MapStore]:
        return await self._navigate_to_content(character, tasks_master, map_db)

    async def task_new(
        self, character: Character, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> dict:
        """Accepts a new task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(character, tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_new(character)

    @sync_character_state
    async def _execute_task_new(self, character: Character) -> dict:
        return await self.api.task_new(character)

    async def task_complete(
        self, character: Character, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> dict:
        """Completes the character's current (finished) task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(character, tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_complete(character)

    @sync_character_state
    async def _execute_task_complete(self, character: Character) -> dict:
        return await self.api.task_complete(character)

    async def task_cancel(
        self, character: Character, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> dict:
        """Cancels the current task for 1 task coin. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(character, tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_cancel(character)

    @sync_character_state
    async def _execute_task_cancel(self, character: Character) -> dict:
        return await self.api.task_cancel(character)

    async def task_exchange(
        self, character: Character, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> dict:
        """Exchanges 6 task coins for a random reward. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(character, tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_exchange(character)

    @sync_character_state
    async def _execute_task_exchange(self, character: Character) -> dict:
        return await self.api.task_exchange(character)

    async def task_trade(
        self,
        character: Character,
        code: str,
        quantity: int,
        tasks_master: str = "tasks_master",
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Trades items toward the character's current item-type task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(character, tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_trade(character, code, quantity)

    @sync_character_state
    async def _execute_task_trade(self, character: Character, code: str, quantity: int) -> dict:
        return await self.api.task_trade(character, code, quantity)

    # ------------------------------------------------------------------
    # Give (character-to-character, same tile), pending items, misc
    # ------------------------------------------------------------------

    @sync_character_state
    async def give_gold(self, character: Character, quantity: int, to_character: str) -> dict:
        """Gives gold to another of your characters standing on the same tile."""
        return await self.api.give_gold(character, quantity, to_character)

    @sync_character_state
    async def give_items(self, character: Character, items: list[dict], to_character: str) -> dict:
        """Gives items to another of your characters standing on the same tile.
        items: [{'code':..., 'quantity':...}, ...]"""
        return await self.api.give_items(character, items, to_character)

    @sync_character_state
    async def claim_pending_item(self, character: Character, pending_item_id: str) -> dict:
        """Claims a pending item (achievement/GE/event reward) into this character's inventory."""
        return await self.api.claim_item(character, pending_item_id)

    @sync_character_state
    async def delete_item(self, character: Character, code: str, quantity: int) -> dict:
        """Permanently deletes an item from inventory. No confirmation -- use with care."""
        return await self.api.delete_item(character, code, quantity)

    @sync_character_state
    async def change_skin(self, character: Character, skin: str) -> dict:
        """Changes the character's skin to one the account owns."""
        return await self.api.change_skin(character, skin)

    @sync_character_state
    async def rename(self, character: Character, new_name: str) -> dict:
        """Renames the character (members only). Uses the character's current
        name for the URL, then syncs the new name back onto the object."""
        return await self.api.rename_character(character, new_name)
