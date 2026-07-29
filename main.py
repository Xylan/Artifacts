#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Jul 21 18:55:04 2026

@author: xylan
"""

import asyncio
import nest_asyncio
from client import ArtifactsAPI, InventoryFullError
from database import GameDatabase
from account import Account
from roles import ensure_naming_scheme, build_roles
from task_runner import TaskEngine

nest_asyncio.apply()

    
async def main():
    async with ArtifactsAPI() as api:  # starts everything with API access
        # loading
        db = GameDatabase(api=api)
        account = Account(api, map_db=db.maps)  # live account state + character roster + shared actions
        await db.sync_all()
        await account.sync()  # pulls /my/details, /my/bank, /my/pending_items, /events/active, /my/characters

        if not account.characters:
            print("No characters found on this account!")
            return account, db, api

        # Requirement #6: enforce the Xylan1..Xylan5 naming scheme (renames
        # existing characters / creates any missing ones, best-effort).
        await ensure_naming_scheme(account, api)

        print(f"Loading finished. {account!r}")

        # Build roles against the roster's ACTUAL names (rename above is
        # best-effort and silently no-ops without an active membership --
        # see ensure_naming_scheme), not the aspirational Xylan1..Xylan5
        # scheme, so role/skill assignment always lines up with real names.
        roles = build_roles(list(account.characters.keys()))
        engine = TaskEngine(account, db, roles=roles)   

        # Requirement #4, "Clean Slate": deposit everyone's gold/inventory
        # into the bank before the scheduler starts handing out work.
        await engine.initialize()

        # Requirement #3/#4: seed each character's auto-detected gear
        # upgrades as CRAFT-tier work orders, wired to auto-equip once ready.
        for character in account.characters.values():
            engine.request_upgrades_for(character)

        # Requirement #5, keep-in-stock example -- tune/add freely:
        # engine.add_stock_rule("cooked_chicken", 20)

        # Requirement #5, default-task example -- zero inertia, only used
        # when nothing else is claimable for that character:
        # engine.set_default_gather_task("Xylan5", "copper_rocks")

        await engine.run()  # runs indefinitely; verifies + prints the plan tree first

        return account, db, api


if __name__ == "__main__":
    account, db, api = asyncio.run(main())
    