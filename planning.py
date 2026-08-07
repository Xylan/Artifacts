#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
planning.py: Character-agnostic gear planning.

GearPlan/PlanTask (models.py) are pure data -- no live Character reference
until assign()/auto_assign() is called, and even then only a character name
is stored, so a plan can be built, persisted (database.TaskStore), and
handed to any character roster at execution time.

Flow is bank-first: gather tasks deposit everything into the bank (as soon
as inventory fills, and again once the task's target is reached); craft
tasks always withdraw ingredients from the bank right before crafting, and
deposit finished goods back afterward. Nothing is assumed to already be
sitting in a character's inventory. Task completion is judged by total
holdings (every character's inventory + the bank) vs. each task's
target_quantity, not a fixed action count, since gather yields are random.
"""
import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from account import Account
from database import GameDatabase, TaskStore
from models import PlanTask, TaskType, TaskStatus, Item

# ItemType values that occupy a gear slot and map 1:1 to a
# Character.equipment "{type}_slot" attribute. Rings (ring1/ring2) are
# excluded for now -- picking which of two equipped rings is "worse" needs
# slot-pair logic beyond a single equipped-vs-candidate compare.
ARMOR_WEAPON_TYPES = {"weapon", "shield", "helmet", "body_armor", "leg_armor", "boots"}


def item_score(item: Optional[Item]) -> int:
    """Naive upgrade heuristic: sum of all numeric effect values (attack,
    dmg, hp, res, etc. all weighted equally). Good enough to catch obvious
    upgrades over an empty slot or a clearly worse item; doesn't know your
    build priorities (crit vs raw dmg vs res). Swap this out (e.g. take a
    weights dict) if it starts recommending odd trades."""
    if item is None:
        return 0
    return sum(e.value for e in item.effects)


class GearList:
    """Wishlist of items you want, e.g. gear_list.add('copper_armor')."""

    def __init__(self):
        self.wants: Dict[str, int] = {}

    def add(self, code: str, quantity: int = 1) -> "GearList":
        self.wants[code] = self.wants.get(code, 0) + quantity
        return self

    def remove(self, code: str) -> "GearList":
        self.wants.pop(code, None)
        return self

    @classmethod
    def for_upgrades(cls, character, db: GameDatabase) -> "GearList":
        """Builds a wishlist automatically: for each equipment slot, the
        single best craftable weapon/armor piece the character's current
        skill levels allow (and whose other conditions -- level, etc. --
        they meet) that scores higher than whatever's currently equipped
        in that slot. An empty slot scores 0, so any eligible craftable
        item fills it.

        Only the highest-scoring candidate per slot is kept -- e.g. a
        weaponcrafter who can craft both a dagger and a wooden_staff will
        only get whichever one scores higher queued, since weapon_slot can
        only hold one item. Previously every item that individually beat
        the equipped baseline was added, so unrelated weapons (or unrelated
        pieces of armor for the same slot) ended up queued together even
        though only one could ever actually be equipped."""
        gear_list = cls()
        craftable = db.items.get_craftable_for_character(character)

        best_by_slot: Dict[str, Item] = {}

        for item in craftable:
            if item.type not in ARMOR_WEAPON_TYPES:
                continue
            if not db.items.meets_conditions(character, item.conditions):
                continue

            slot_attr = f"{item.type}_slot"
            equipped_code = getattr(character.equipment, slot_attr, "")
            if equipped_code == item.code:
                continue  # already wearing this exact item

            equipped_item = db.items.get_item_obj(equipped_code) if equipped_code else None
            if item_score(item) <= item_score(equipped_item):
                continue  # doesn't beat what's currently equipped

            current_best = best_by_slot.get(slot_attr)
            if current_best is None or item_score(item) > item_score(current_best):
                best_by_slot[slot_attr] = item

        for item in best_by_slot.values():
            gear_list.add(item.code)

        return gear_list

    def resolve(self, db: GameDatabase, have: Optional[Dict[str, int]] = None) -> "GearPlan":
        """Expands the wishlist into a dependency-linked GearPlan. Each
        task's target_quantity is the total amount of `code` that needs to
        exist (across characters+bank) -- it's a target for the runner's
        while-loop, not a precomputed action count. Craftable intermediates
        (bars used in a weapon recipe, etc.) recurse; anything left that's
        gatherable resolves to a gather task; anything neither craftable
        nor gatherable is still recorded (skill='') as a reminder to buy it
        from the GE/an NPC."""
        have = dict(have or {})
        tasks: List[PlanTask] = []
        tasks_by_id: Dict[int, PlanTask] = {}
        produced_by: Dict[str, int] = {}
        next_id = [0]

        def new_id() -> int:
            next_id[0] += 1
            return next_id[0]

        def get_item(code: str) -> Optional[Item]:
            return db.items.get_item_obj(code)

        def resolve_need(code: str, needed: int) -> List[int]:
            if needed <= 0:
                return []

            used = min(have.get(code, 0), needed)
            if used:
                have[code] -= used
            needed -= used
            if needed <= 0:
                return [produced_by[code]] if code in produced_by else []

            if code in produced_by:
                # Same ingredient needed by more than one branch of the plan --
                # bump the existing task's target and cascade the extra amount
                # to its own dependencies rather than creating a duplicate task.
                existing = tasks_by_id[produced_by[code]]
                existing.target_quantity += needed
                if existing.type == TaskType.CRAFT:
                    item = get_item(code)
                    extra_crafts = -(-needed // existing.produces_per_action)
                    for ing in item.craft.items:
                        resolve_need(ing.code, ing.quantity * extra_crafts)
                return [existing.id]

            item = get_item(code)
            if item and item.craft:
                produces = max(1, item.craft.quantity)
                crafts_estimate = -(-needed // produces)  # ceil, just to size ingredient needs
                dep_ids: List[int] = []
                for ing in item.craft.items:
                    dep_ids.extend(resolve_need(ing.code, ing.quantity * crafts_estimate))

                task = PlanTask(
                    id=new_id(), type=TaskType.CRAFT, code=code, target_quantity=needed,
                    skill=item.craft.skill, skill_level=item.craft.level,
                    produces_per_action=produces, depends_on=dep_ids,
                )
                tasks.append(task)
                tasks_by_id[task.id] = task
                produced_by[code] = task.id
                return [task.id]

            resource = db.resources.find_best_for_item(code)
            if resource:
                drop = next((d for d in resource["drops"] if d["code"] == code), {})
                avg_yield = max(1, (drop.get("min_quantity", 1) + drop.get("max_quantity", 1)) / 2)

                task = PlanTask(
                    id=new_id(), type=TaskType.GATHER, code=code, target_quantity=needed,
                    node_code=resource["code"], skill=resource["skill"],
                    skill_level=resource["level"], produces_per_action=int(avg_yield),
                )
                tasks.append(task)
                tasks_by_id[task.id] = task
                produced_by[code] = task.id
                return [task.id]

            # Neither craftable nor gatherable -- flag for manual GE/NPC purchase.
            task = PlanTask(id=new_id(), type=TaskType.GATHER, code=code, target_quantity=needed)
            tasks.append(task)
            tasks_by_id[task.id] = task
            produced_by[code] = task.id
            return [task.id]

        for code, qty in self.wants.items():
            resolve_need(code, qty)

        return GearPlan(tasks=tasks)


@dataclass
class GearPlan:
    tasks: List[PlanTask] = field(default_factory=list)
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def gather_tasks(self) -> List[PlanTask]:
        return [t for t in self.tasks if t.type == TaskType.GATHER]

    @property
    def craft_tasks(self) -> List[PlanTask]:
        return [t for t in self.tasks if t.type == TaskType.CRAFT]

    @property
    def is_complete(self) -> bool:
        return all(t.is_done for t in self.tasks)

    def get(self, task_id: int) -> Optional[PlanTask]:
        return next((t for t in self.tasks if t.id == task_id), None)

    def assign(self, task_id: int, character_name: str) -> None:
        task = self.get(task_id)
        if task:
            task.assigned_to = character_name

    def auto_assign(self, characters: Dict[str, "Character"]) -> List[PlanTask]:
        """Greedily assigns unassigned tasks to the least-loaded eligible
        character (skill level requirement met). Returns tasks nobody qualifies for."""
        load: Dict[str, int] = defaultdict(int)
        unassignable: List[PlanTask] = []

        for task in self.tasks:
            if task.is_assigned or task.is_done:
                if task.assigned_to:
                    load[task.assigned_to] += 1
                continue

            eligible = [
                name for name, char in characters.items()
                if getattr(char.skills, f"{task.skill}_level", 0) >= task.skill_level
            ] if task.skill else list(characters.keys())

            if not eligible:
                unassignable.append(task)
                continue

            chosen = min(eligible, key=lambda n: load[n])
            task.assigned_to = chosen
            load[chosen] += 1

        return unassignable

    def summary(self) -> str:
        lines = []
        for t in self.tasks:
            who = t.assigned_to or "UNASSIGNED"
            lines.append(
                f"[{t.id}] {t.status.value:11} {t.type.value:6} target={t.target_quantity:<4} "
                f"{t.code:<20} (needs {t.skill or '-'} {t.skill_level}) -> {who}"
            )
        return "\n".join(lines)


def held_snapshot(account: Account) -> Dict[str, int]:
    """Total quantity of every item code currently sitting in any
    character's inventory OR the bank -- pass this as `have` to
    GearList.resolve() so the plan doesn't re-task materials you already own."""
    snapshot: Dict[str, int] = defaultdict(int)
    for character in account.characters.values():
        for item in character.inventory:
            snapshot[item.code] += item.quantity
    for item in account.bank.items:
        snapshot[item.code] += item.quantity
    return dict(snapshot)


def load_open_plans(task_store: TaskStore) -> Dict[str, GearPlan]:
    """Rehydrates every not-yet-finished plan found in the TaskStore, e.g.
    after a restart."""
    return {
        plan_id: GearPlan(plan_id=plan_id, tasks=task_store.load_plan(plan_id))
        for plan_id in task_store.list_open_plans()
    }


class PlanRunner:
    """Executes a GearPlan against a live account/roster (account.characters).
    Bank-first: gather tasks deposit as inventory fills (and at the end);
    craft tasks withdraw ingredients from the bank right before crafting and
    deposit output afterward. Completion is judged by total holdings (all
    inventories + bank) vs. each task's target_quantity. Progress is written
    to TaskStore after every status change so an interrupted run can be
    resumed via load_open_plans() instead of re-resolved from scratch.
    """

    def __init__(self, account: Account, db: GameDatabase, task_store: TaskStore):
        self.account = account
        self.db = db
        self.task_store = task_store

    @property
    def characters(self) -> Dict[str, "Character"]:
        return self.account.characters

    def _held(self, code: str) -> int:
        total = sum(
            item.quantity for c in self.characters.values() for item in c.inventory
            if item.code == code
        )
        total += sum(item.quantity for item in self.account.bank.items if item.code == code)
        return total

    async def _deposit_and_sync(self, character) -> None:
        await character.deposit_all()
        await self.account.sync_bank()

    async def _withdraw_for_craft(self, character, task: PlanTask, crafts_needed: int) -> None:
        """Pulls exactly what's needed for `crafts_needed` more craft
        actions out of the bank. Bank-first model assumes the character's
        inventory doesn't already hold ingredients -- if it does, this just
        withdraws a little less than the recipe calls for."""
        recipe = self.db.items.get_recipe(task.code)
        if not recipe or crafts_needed <= 0:
            return

        to_withdraw = []
        for ing in recipe.get("items", []):
            needed_qty = ing["quantity"] * crafts_needed
            have_qty = next((i.quantity for i in character.inventory if i.code == ing["code"]), 0)
            shortfall = needed_qty - have_qty
            if shortfall <= 0:
                continue
            bank_qty = next((i.quantity for i in self.account.bank.items if i.code == ing["code"]), 0)
            withdraw_qty = min(shortfall, bank_qty)
            if withdraw_qty > 0:
                to_withdraw.append({"code": ing["code"], "quantity": withdraw_qty})

        if to_withdraw:
            await character.withdraw_items(to_withdraw)
            await self.account.sync_bank()

    async def run(self, plan: GearPlan) -> None:
        pending = [t for t in plan.tasks if not t.is_done]
        unassigned = [t for t in pending if not t.is_assigned]
        if unassigned:
            raise ValueError(
                f"Plan has {len(unassigned)} unassigned task(s); "
                f"call plan.auto_assign() or plan.assign() first."
            )

        self.task_store.save_plan(plan.plan_id, plan.tasks)

        events = {t.id: asyncio.Event() for t in plan.tasks}
        for t in plan.tasks:
            if t.is_done:
                events[t.id].set()

        by_character: Dict[str, List[PlanTask]] = defaultdict(list)
        for t in pending:
            by_character[t.assigned_to].append(t)

        async def run_task(task: PlanTask):
            for dep_id in task.depends_on:
                await events[dep_id].wait()

            character = self.characters[task.assigned_to]
            task.status = TaskStatus.IN_PROGRESS
            self.task_store.update_status(plan.plan_id, task.id, task.status)

            if task.type == TaskType.GATHER and task.skill:
                while self._held(task.code) < task.target_quantity:
                    print(f"[{character.name}] Gathering {task.node_code} for {task.code} "
                          f"({self._held(task.code)}/{task.target_quantity})...")
                    await character.gather(resource=task.node_code)
                    if character.is_inventory_full:
                        await self._deposit_and_sync(character)
                await self._deposit_and_sync(character)  # flush whatever's left

            elif task.type == TaskType.CRAFT:
                while self._held(task.code) < task.target_quantity:
                    remaining = task.target_quantity - self._held(task.code)
                    crafts_needed = -(-remaining // task.produces_per_action)  # ceil
                    await self._withdraw_for_craft(character, task, crafts_needed)

                    print(f"[{character.name}] Crafting {task.code} "
                          f"({self._held(task.code)}/{task.target_quantity})...")
                    await character.craft(task.code, workshop=task.skill)

                    if character.is_inventory_full:
                        await self._deposit_and_sync(character)
                await self._deposit_and_sync(character)  # crafted goods -> bank

            else:
                print(f"[{character.name}] '{task.code}' has no resource node -- "
                      f"buy {task.target_quantity} from GE/NPC manually. Skipping.")

            task.status = TaskStatus.DONE
            self.task_store.update_status(plan.plan_id, task.id, task.status)
            events[task.id].set()

        async def run_character_queue(tasks: List[PlanTask]):
            for t in tasks:
                await run_task(t)

        await asyncio.gather(*(run_character_queue(ts) for ts in by_character.values()))

        if plan.is_complete:
            self.task_store.delete_plan(plan.plan_id)

    async def deposit_all(self) -> None:
        """Tells every character on the account to deposit their full
        inventory into the bank, concurrently, then refreshes bank state."""
        await asyncio.gather(*(c.deposit_all() for c in self.characters.values()))
        await self.account.sync_bank()
