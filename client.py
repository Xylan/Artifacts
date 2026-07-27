#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Jul 21 18:56:43 2026

@author: xylan
"""

import httpx
from typing import List, Optional
from config import BASE_URL, TOKEN
from account import RateLimiter, classify_bucket


class APIError(Exception):
    """Base exception for all API errors."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"API Error [{code}]: {message}")

# Specific sub-exceptions for clean try/except blocks
class CharacterAlreadyAtDestinationError(APIError):
    """Code 490"""
    pass

class CharacterInCooldownError(APIError):
    """Code 499"""
    pass

class InventoryFullError(APIError):
    """Code 497"""
    pass

# Map error codes to explicit exception classes
ERROR_CODE_MAP = {
    490: CharacterAlreadyAtDestinationError,
    497: InventoryFullError,
    499: CharacterInCooldownError,
}


class ArtifactsAPI:
    def __init__(self):
        # httpx.AsyncClient handles connection pooling for async HTTP requests
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            timeout=30.0
        )
        # Shared across every request this process makes, regardless of which
        # character fired it -- the API's rate-limit buckets are per
        # account/IP, not per character (see account.py: RateLimiter).
        self.rate_limiter = RateLimiter()

    async def request(self, method: str, endpoint: str, character=None, payload=None, params=None, return_full: bool = False) -> dict:
        """Generic, low-level HTTP request wrapper for Artifacts API.

        This stays the single place that actually talks to httpx -- it owns
        cooldown-waiting, rate-limit bucket tracking, and error translation.
        Everything below it (get_*/action methods) is a thin, typed wrapper
        that builds the right endpoint string/payload and calls this.

        If return_full is True, returns the entire response envelope
        (including pagination fields like "page", "pages", "total")
        instead of just the "data" field. Needed for paginated list
        endpoints (e.g. /maps, /items, /monsters) so callers can tell
        how many pages remain.
        """
        if character:
            await character.wait_cooldown()

        bucket = classify_bucket(method, endpoint)
        await self.rate_limiter.wait_if_needed(bucket)

        # Pass both payload (as json) and params (as query params) to httpx/aiohttp
        response = await self.client.request(
            method,
            endpoint,
            json=payload,
            params=params
        )
        self.rate_limiter.update_from_headers(bucket, response.headers)
        res_data = response.json()

        # Artifacts API returns errors inside the JSON payload under "error"
        if not response.is_success or "error" in res_data:
            err_info = res_data.get("error", {})
            code = err_info.get("code", response.status_code)
            message = err_info.get("message", "Unknown API Error")

            # Raise specific exception if mapped, otherwise default to base APIError
            exception_class = ERROR_CODE_MAP.get(code, APIError)
            raise exception_class(code, message)

        # Extract returned data payload
        data_payload = res_data.get("data")

        return res_data if return_full else data_payload

    # ------------------------------------------------------------------
    # Character actions (/my/{name}/action/*)
    # `character` is the Character object -- used both for the {name} in the
    # URL and so request() can wait out its cooldown automatically.
    # ------------------------------------------------------------------

    async def move(self, character, x: int, y: int) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/move", character, {"x": x, "y": y})

    async def transition(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/transition", character)

    async def fight(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/fight", character)

    async def rest(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/rest", character)

    async def gathering(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/gathering", character)

    async def crafting(self, character, code: str, quantity: int = 1) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/crafting", character,
            {"code": code, "quantity": quantity},
        )

    async def recycling(self, character, code: str, quantity: int = 1, enhanced: bool = False) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/recycling", character,
            {"code": code, "quantity": quantity, "enhanced": enhanced},
        )

    async def equip(self, character, items: List[dict]) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/equip", character, items)

    async def unequip(self, character, slots: List[dict]) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/unequip", character, slots)

    async def use(self, character, code: str, quantity: int = 1) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/use", character,
            {"code": code, "quantity": quantity},
        )

    async def bank_deposit_item(self, character, items: List[dict]) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/bank/deposit/item", character, items)

    async def bank_deposit_gold(self, character, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/bank/deposit/gold", character,
            {"quantity": quantity},
        )

    async def bank_withdraw_item(self, character, items: List[dict]) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/bank/withdraw/item", character, items)

    async def bank_withdraw_gold(self, character, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/bank/withdraw/gold", character,
            {"quantity": quantity},
        )

    async def bank_buy_expansion(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/bank/buy_expansion", character)

    async def npc_buy(self, character, code: str, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/npc/buy", character,
            {"code": code, "quantity": quantity},
        )

    async def npc_sell(self, character, code: str, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/npc/sell", character,
            {"code": code, "quantity": quantity},
        )

    async def ge_buy(self, character, order_id: str, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/grandexchange/buy", character,
            {"id": order_id, "quantity": quantity},
        )

    async def ge_create_sell_order(self, character, code: str, quantity: int, price: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/grandexchange/create_sell_order", character,
            {"code": code, "quantity": quantity, "price": price},
        )

    async def ge_create_buy_order(self, character, code: str, quantity: int, price: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/grandexchange/create_buy_order", character,
            {"code": code, "quantity": quantity, "price": price},
        )

    async def ge_fill_buy_order(self, character, order_id: str, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/grandexchange/fill", character,
            {"id": order_id, "quantity": quantity},
        )

    async def ge_cancel_order(self, character, order_id: str) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/grandexchange/cancel", character,
            {"id": order_id},
        )

    async def task_new(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/task/new", character)

    async def task_complete(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/task/complete", character)

    async def task_cancel(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/task/cancel", character)

    async def task_exchange(self, character) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/task/exchange", character)

    async def task_trade(self, character, code: str, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/task/trade", character,
            {"code": code, "quantity": quantity},
        )

    async def give_gold(self, character, quantity: int, to_character: str) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/give/gold", character,
            {"quantity": quantity, "character": to_character},
        )

    async def give_items(self, character, items: List[dict], to_character: str) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/give/item", character,
            {"items": items, "character": to_character},
        )

    async def claim_item(self, character, pending_item_id: str) -> dict:
        return await self.request("POST", f"/my/{character.name}/action/claim_item/{pending_item_id}", character)

    async def delete_item(self, character, code: str, quantity: int) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/delete", character,
            {"code": code, "quantity": quantity},
        )

    async def change_skin(self, character, skin: str) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/change_skin", character, {"skin": skin}
        )

    async def rename_character(self, character, new_name: str) -> dict:
        return await self.request(
            "POST", f"/my/{character.name}/action/rename", character, {"name": new_name}
        )

    # ------------------------------------------------------------------
    # Data / catalog listing endpoints (used by database/*_store.py)
    # All paginated; return_full=True so callers can read "page"/"pages".
    # ------------------------------------------------------------------

    async def get_items(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/items?page={page}&size={size}", return_full=True)

    async def get_monsters(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/monsters?page={page}&size={size}", return_full=True)

    async def get_resources(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/resources?page={page}&size={size}", return_full=True)

    async def get_maps(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/maps?page={page}&size={size}", return_full=True)

    async def get_events(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/events?page={page}&size={size}", return_full=True)

    async def get_events_active(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/events/active?page={page}&size={size}", return_full=True)

    # ------------------------------------------------------------------
    # Account-level endpoints (used by account.py -- live, not TTL-cached)
    # ------------------------------------------------------------------

    async def get_my_details(self) -> dict:
        return await self.request("GET", "/my/details")

    async def get_my_characters(self) -> dict:
        return await self.request("GET", "/my/characters")

    async def get_my_bank(self) -> dict:
        return await self.request("GET", "/my/bank")

    async def get_my_bank_items(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/my/bank/items?page={page}&size={size}", return_full=True)

    async def get_my_pending_items(self, page: int = 1, size: int = 100) -> dict:
        return await self.request("GET", f"/my/pending_items?page={page}&size={size}", return_full=True)

    async def get_my_rates(self) -> dict:
        return await self.request("GET", "/my/rates")

    async def get_account(self, account_name: str) -> dict:
        return await self.request("GET", f"/accounts/{account_name}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        """Cleanly closes the underlying HTTP connections."""
        await self.client.aclose()
