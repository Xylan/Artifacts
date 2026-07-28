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
from typing import Optional, Set

_id_counter = itertools.count(1)


class OrderKind(IntEnum):
    """The actual action type an order requires. Purely mechanical -- it
    carries no priority meaning by itself (see Priority below)."""
    GATHER = 1
    CRAFT = 2


class Priority(IntEnum):
    """Scheduling tier. Fixed ordering per spec: Crafting > Gathering >
    Keep-in-stock > Default. Assigned per-order (not derived solely from
    `kind`), so e.g. a craft order created only to satisfy a keep-in-stock
    rule is deliberately downgraded to KEEP_STOCK and can be outranked by a
    genuine GATHER request."""
    DEFAULT = 0
    KEEP_STOCK = 10
    GATHER = 20
    CRAFT = 30


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
    requester: Optional[str] = None       # character who wants the finished item delivered + equipped
    equip_slot: Optional[str] = None
    only_for: Optional[str] = None        # restricts claimability to a single character (default tasks)
    locked_to: Optional[str] = None       # CRAFT only: character holding the exclusive claim
    claimed_by: Set[str] = field(default_factory=set)   # GATHER: characters currently working it
    done: bool = False

    @property
    def base_priority(self) -> int:
        return int(self.priority)
