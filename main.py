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
from planning import GearList, PlanRunner, held_snapshot

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

        print(f"Loading finished. {account!r}")
        
        Xylan = account.get_character("Xylan") or next(iter(account.characters.values()))
        await Xylan.deposit_all()
        wishlist = GearList.for_upgrades(Xylan, db)
        print(wishlist.wants)
        
        plan =  wishlist.resolve(db, have=held_snapshot(account))
        plan.auto_assign(account.characters)
        print(plan.summary())
        runner = PlanRunner(account, db, db.tasks)
        await runner.run(plan)
        await runner.deposit_all()
        
        
        
        # for count in range(1000):
        #     while Xylan.is_inventory_full == False:
        #         await Xylan.actions.rest()
        #         try:
        #             await Xylan.actions.fight(target="chicken")
        #         except InventoryFullError:
        #             break
        #     await Xylan.actions.deposit_all()
        #     await Xylan.actions.deposit_gold(Xylan.gold)

        # return account, db, api


if __name__ == "__main__":
    account, db, api = asyncio.run(main())
    