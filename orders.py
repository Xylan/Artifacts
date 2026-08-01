#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orders.py: Dynamic work-order primitives for task_runner.TaskEngine.

Distinct from planning.PlanTask/GearPlan (a one-off wishlist resolved into a
static DAG and run to completion via PlanRunner, still used for that
purpose): WorkOrders live in a shared, continuously-updated pool that every
character repeatedly re-evaluates against at scheduling breakpoints -- see
task_runner.TaskEngine.select_order_for().
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Protocol, Set, Tuple, runtime_checkable

_id_counter = itertools.count(1)


@runtime_checkable
class SchedulableOrder(Protocol):
    """Structural (duck-typed) common shape shared by WorkOrder (below) and
    models.PlanTask -- the two representations of "gather/craft N of item X"
    used by task_runner.TaskEngine and planning.PlanRunner respectively.

    This is the "single polymorphic Order class" consolidation called for in
    the refactor notes, implemented as a Protocol rather than a shared base
    dataclass on purpose: WorkOrder and PlanTask are persisted/compared very
    differently -- WorkOrder is transient, in-memory-only, and tracks live
    claim/lock state for concurrent characters (locked_to, claimed_by,
    equip_requests); PlanTask is serialized row-for-row into
    database.TaskStore and tracks a static dependency DAG (depends_on,
    assigned_to, status). Forcing them onto one dataclass would mean either
    dragging TaskEngine-only fields into every persisted PlanTask row, or
    dragging PlanTask-only fields into every live WorkOrder -- neither is a
    real simplification, and PlanTask's on-disk schema (TaskStore) would
    have to migrate for no behavioral gain. A Protocol instead lets any code
    that only cares about the shared "what item, how much, via what
    skill/node" shape (e.g. a future shared verify()/summary() helper)
    accept either type interchangeably, without touching either class's
    persistence or concurrency model. Both already satisfy this shape
    field-for-field with no changes required (see PlanTask in models.py) --
    that's what made it worth pulling out at all.

    Note: models.Task (also in models.py) is a different, unrelated concept
    -- a read-only view over the Artifacts API's own task-board fields on a
    live Character (character.current_task) -- and does NOT implement this
    protocol; it isn't one of "our" schedulable orders."""
    code: str
    node_code: str
    skill: str
    skill_level: int
    target_quantity: int
    produces_per_action: int


class OrderKind(IntEnum):
    """The actual action type an order requires. Purely mechanical -- it
    carries no priority meaning by itself (see Priority below)."""
    GATHER = 1
    CRAFT = 2


class Priority(IntEnum):
    """Scheduling tier. Fixed ordering per spec: Equip > Crafting > Gathering
    > Keep-in-stock > Auto-convert > Default. Assigned per-order (not derived
    solely from `kind`), so e.g. a craft order created only to satisfy a
    keep-in-stock rule is deliberately downgraded to KEEP_STOCK and can be
    outranked by a genuine GATHER request.

    EQUIP sits above CRAFT (and above CRAFT + INERTIA_BONUS, so it always
    wins the inertia comparison in TaskEngine.select_order_for too) because
    an equip request is a character explicitly waiting on gear that's ready
    -- see TaskEngine.request_equipment(), which forces this tier across
    the whole craft/gather expansion chain (not just the top-level order)
    so getting someone equipped interrupts whatever they were doing rather
    than queueing behind it.

    AUTO_CRAFT sits just above DEFAULT (busywork) and just below KEEP_STOCK:
    it's for automatically converting surplus of a "pure" single-use
    default-gathered raw material (one that's only ever an ingredient in
    exactly one recipe -- e.g. copper_ore -> copper_bar, raw_chicken ->
    cooked_chicken) into its finished item. It must never outrank an actual
    keep-in-stock or gather/craft request for that same raw material, but it
    should still preempt characters who'd otherwise be sitting on DEFAULT
    busywork. See TaskEngine.refresh_auto_convert_orders()."""
    DEFAULT = 0
    AUTO_CRAFT = 5
    KEEP_STOCK = 10
    GATHER = 20
    CRAFT = 30
    EQUIP = 40


# Effective-priority bonus given to whatever order a character is CURRENTLY
# working, so an order of equal-or-slightly-higher priority doesn't cause
# constant thrashing (requirement: "slight bias toward continuing the
# current task"). DEFAULT-tier orders never receive this bonus (zero
# inertia), so anything else preempts them immediately.
INERTIA_BONUS = 5


@dataclass
class WorkOrder:
    id: int = field(default_factory=lambda: next(_id_counter))
    kind: OrderKind = OrderKind.GATHER
    priority: Priority = Priority.GATHER
    code: str = ""                        # item code this order accumulates
    node_code: str = ""                   # GATHER: resource node code. CRAFT: workshop skill (== skill)
    skill: str = ""
    skill_level: int = 1
    target_quantity: int = 0
    produces_per_action: int = 1
    parent_id: Optional[int] = None       # order this ingredient serves, if any (for plan-tree/verify)
    # Queue of (character_name, equip_slot) pairs -- one entry per unit of
    # `code` that some character wants delivered from the bank and equipped
    # once available. Supports multiple recipients wanting the same item:
    # 5 characters all needing a copper_boots upgrade collapse into ONE
    # order (target_quantity=5) with 5 queued equip_requests, rather than
    # each getting their own duplicate order. This used to be a single
    # requester/equip_slot pair, which meant only the FIRST character to
    # request an item ever got it delivered -- everyone else's share just
    # sat in the bank forever once the order was marked done. See
    # TaskEngine._try_deliver_equipment / _delivery_loop.
    equip_requests: List[Tuple[str, str]] = field(default_factory=list)
    only_for: Optional[str] = None        # restricts claimability to a single character (default tasks)
    locked_to: Optional[str] = None       # CRAFT only: character holding the exclusive claim
    claimed_by: Set[str] = field(default_factory=set)   # GATHER: characters currently working it
    done: bool = False
    # Non-blocking re-entrancy guard for executor.Executor._try_deliver_equipment
    # (TODO task 12): delivery can now be reached concurrently from several
    # independent triggers for the SAME order (EquipmentRequested/BankSynced
    # bus subscribers, the safety-sweep loop, AND a direct call from
    # _run_gather_step/_run_craft_step) -- this flag serializes actual work
    # on the order's equip_requests queue to at most one caller at a time.
    # Deliberately a plain bool, not an asyncio.Lock: a losing concurrent
    # call is meant to return immediately rather than block, since blocking
    # here can deadlock against a character's busy_lock -- see
    # Executor._try_deliver_equipment's docstring for the full reasoning.
    _delivering: bool = field(default=False, repr=False)

    @property
    def base_priority(self) -> int:
        return int(self.priority)
