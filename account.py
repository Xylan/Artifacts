#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Account: live/volatile account-level state, shared across every character on
the account -- rate limit windows, bank contents, pending items, active world
events, and account details (membership, gems, badges). Also the home for the
character roster: Account owns the one shared CharacterActions instance and
attaches a bound `.actions` to each Character it builds, so callers can do
`xylan.actions.rest()` (or `xylan.rest()` via Character's __getattr__ fallback)
instead of threading `actions`/`character` through everywhere by hand.

Unlike database/*Store, this is NOT TTL-cached; it reflects the live state of
the account and should be re-synced whenever it's read for anything
decision-critical.

Field names below are pulled directly from api.artifactsmmo.com/openapi.json
(MyAccountDetails, BankSchema, RateLimitsDataSchema, PendingItemSchema,
ActiveEventSchema).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from character import Character
from models import InventoryItem, Event, parse_reset


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

@dataclass
class RateLimitWindow:
    """One bucket's current state, e.g. the 'action' bucket shared by every
    /my/{name}/action/* call across every character on the account."""
    limit: int = 0
    remaining: int = 1  # optimistic default so a cold-start doesn't self-block
    reset: Optional[datetime] = None

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def seconds_until_reset(self) -> float:
        if not self.reset:
            return 0.0
        return max(0.0, (self.reset - datetime.now(self.reset.tzinfo)).total_seconds())


# Endpoints billed against the 'account' bucket rather than 'data' or
# 'action'. Per https://docs.artifactsmmo.com/api_guide/rate_limits/
# NOTE: this list is maintained by hand against the docs and may drift as
# the API evolves -- worth re-checking against openapi.json periodically.
_ACCOUNT_BUCKET_ENDPOINTS = {
    "/accounts/create",
    "/accounts/forgot_password",
    "/accounts/reset_password",
    "/characters/create",
    "/characters/delete",
    "/token",
    "/my/change_password",
    "/my/change_email",
    "/my/buy_subscription",
    "/my/subscribe/stripe",
    "/my/subscribe/member_token",
    "/my/subscribe/cancel",
    "/my/buy_gems",
    "/my/rates",
    "/gems_shop/skin",
    "/gems_shop/spawn_event",
    "/gems_shop/subscription",
    "/game_assistant/ask",
}


def classify_bucket(method: str, endpoint: str) -> str:
    """Maps an HTTP method + endpoint to the rate-limit bucket it's billed
    against, so the RateLimiter can track/throttle the right window."""
    path = endpoint.split("?", 1)[0]

    if "/action/" in path and path.startswith("/my/"):
        return "action"
    if path == "/simulation/fight":
        return "simulation"
    if path in _ACCOUNT_BUCKET_ENDPOINTS:
        return "account"
    return "data"


@dataclass
class RateLimiter:
    """Owned by ArtifactsAPI (one instance shared across every request the
    process makes) and referenced by Account. Because character actions all
    draw from the SAME 'action' bucket regardless of which character fired
    them, this is what keeps concurrent multi-character runs from hammering
    into 429s once orchestration is added."""
    account: RateLimitWindow = field(default_factory=RateLimitWindow)
    data: RateLimitWindow = field(default_factory=RateLimitWindow)
    action: RateLimitWindow = field(default_factory=RateLimitWindow)
    simulation: RateLimitWindow = field(default_factory=RateLimitWindow)
    assistant: Optional[RateLimitWindow] = None  # members only; stays None otherwise

    def update_from_headers(self, bucket: str, headers: Dict[str, str]) -> None:
        """Called by ArtifactsAPI.request() after every response."""
        limit = headers.get("x-ratelimit-limit")
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")

        if limit is None and remaining is None and reset is None:
            return  # server didn't send rate-limit headers on this response

        window: RateLimitWindow = getattr(self, bucket, self.data)
        if limit is not None:
            window.limit = int(limit)
        if remaining is not None:
            window.remaining = int(remaining)
        if reset is not None:
            window.reset = parse_reset(reset)

    def update_from_rates_payload(self, data: Dict[str, Any]) -> None:
        """Called after GET /my/rates -- refreshes every bucket at once."""
        for bucket_name in ("account", "data", "action", "simulation", "assistant"):
            payload = data.get(bucket_name)
            if not payload:
                continue
            window = RateLimitWindow(
                limit=payload.get("limit", 0),
                remaining=payload.get("remaining", 0),
                reset=parse_reset(payload.get("reset")),
            )
            setattr(self, bucket_name, window)

    async def wait_if_needed(self, bucket: str) -> None:
        """Pre-emptively sleeps if the given bucket is exhausted, instead of
        firing a request we already know will 429."""
        import asyncio

        window: RateLimitWindow = getattr(self, bucket, None)
        if window and window.is_exhausted:
            wait = window.seconds_until_reset
            if wait > 0:
                print(f"[RateLimiter] '{bucket}' bucket exhausted ({window.remaining}/{window.limit}). Waiting {wait:.1f}s...")
                await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Account details / bank / pending items
# ---------------------------------------------------------------------------

@dataclass
class AccountDetails:
    """Mirrors MyAccountDetails from GET /my/details."""
    username: str = ""
    email: str = ""
    member: bool = False
    member_expiration: Optional[datetime] = None
    status: str = ""
    badges: List[str] = field(default_factory=list)
    skins: List[str] = field(default_factory=list)
    gems: int = 0
    member_token: int = 0
    achievements_points: int = 0
    banned: bool = False
    ban_reason: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountDetails":
        return cls(
            username=data.get("username", ""),
            email=data.get("email", ""),
            member=data.get("member", False),
            member_expiration=parse_reset(data.get("member_expiration")),
            status=data.get("status", ""),
            badges=data.get("badges", []) or [],
            skins=data.get("skins", []) or [],
            gems=data.get("gems", 0),
            member_token=data.get("member_token", 0),
            achievements_points=data.get("achievements_points", 0),
            banned=data.get("banned", False),
            ban_reason=data.get("ban_reason", ""),
        )


@dataclass
class Bank:
    """Mirrors BankSchema from GET /my/bank. Shared across every character
    on the account. Item contents come separately from GET /my/bank/items
    (paginated) -- see Account.sync_bank()."""
    slots: int = 0
    expansions: int = 0
    next_expansion_cost: int = 0
    gold: int = 0
    items: List[InventoryItem] = field(default_factory=list)

    @property
    def used_slots(self) -> int:
        return len(self.items)

    @property
    def is_full(self) -> bool:
        return self.slots > 0 and self.used_slots >= self.slots


@dataclass
class PendingItem:
    """Mirrors PendingItemSchema -- rewards (achievements, GE buy-order
    fills, events) waiting to be claimed by any character on the account via
    POST /my/{name}/action/claim_item/{id}."""
    id: str
    source: str
    description: str
    gold: int = 0
    items: List[InventoryItem] = field(default_factory=list)
    created_at: Optional[datetime] = None
    claimed_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingItem":
        return cls(
            id=data["id"],
            source=data.get("source", ""),
            description=data.get("description", ""),
            gold=data.get("gold", 0),
            items=[
                InventoryItem(slot=-1, code=i["code"], quantity=i["quantity"])
                for i in data.get("items", [])
                if i and i.get("code")
            ],
            created_at=parse_reset(data.get("created_at")),
            claimed_at=parse_reset(data.get("claimed_at")),
        )


class Account:
    """Top-level container: live account state + the character roster.
    One Account per API token/process. Characters live here rather than as
    a loose list in main.py, since they share this account's bank, pending
    items, rate-limit budget -- and now also a single CharacterActions
    instance, bound onto each character as `.actions`."""

    def __init__(self, api, map_db=None):
        self.api = api
        self.map_db = map_db
        self.details: Optional[AccountDetails] = None
        self.bank: Bank = Bank()
        self.pending_items: List[PendingItem] = []
        self.active_events: List[Event] = []
        self.characters: Dict[str, Character] = {}

        # Local import avoids a circular top-level import (CharacterActions
        # imports Character, which doesn't import CharacterActions -- but
        # keeping the import here makes the intentional layering explicit:
        # Account is what wires api + map_db into a shared actions instance).
        from CharacterActions import CharacterActions
        self.actions = CharacterActions(api, map_db)

    def set_map_db(self, map_db) -> None:
        """Call if map_db (e.g. db.maps) wasn't available yet at construction time.
        Updates it here and propagates to every character already built."""
        self.map_db = map_db
        for character in self.characters.values():
            character.actions.map_db = map_db

    @property
    def rate_limiter(self) -> RateLimiter:
        """The limiter actually lives on ArtifactsAPI (it sees every request
        regardless of whether an Account has been built yet); exposed here
        for convenience so callers can do account.rate_limiter.action, etc."""
        return self.api.rate_limiter

    async def sync_details(self) -> None:
        data = await self.api.get_my_details()
        self.details = AccountDetails.from_dict(data)

    async def sync_bank(self) -> None:
        bank_data = await self.api.get_my_bank()
        self.bank.slots = bank_data.get("slots", 0)
        self.bank.expansions = bank_data.get("expansions", 0)
        self.bank.next_expansion_cost = bank_data.get("next_expansion_cost", 0)
        self.bank.gold = bank_data.get("gold", 0)

        items: List[InventoryItem] = []
        page = 1
        while True:
            res = await self.api.get_my_bank_items(page=page, size=100)
            page_items = res.get("data", [])
            items.extend(
                InventoryItem(slot=-1, code=i["code"], quantity=i["quantity"])
                for i in page_items
                if i and i.get("code")
            )
            if page >= res.get("pages", page) or len(page_items) < 100:
                break
            page += 1
        self.bank.items = items

    async def sync_pending_items(self) -> None:
        pending: List[PendingItem] = []
        page = 1
        while True:
            res = await self.api.get_my_pending_items(page=page, size=100)
            page_data = res.get("data", [])
            pending.extend(PendingItem.from_dict(p) for p in page_data)
            if page >= res.get("pages", page) or len(page_data) < 100:
                break
            page += 1
        self.pending_items = pending

    async def sync_active_events(self) -> None:
        """Refreshes currently-live world events (bonus nodes, invasions,
        roaming merchants, etc) from GET /events/active."""
        events: List[Event] = []
        page = 1
        while True:
            res = await self.api.get_events_active(page=page, size=100)
            page_data = res.get("data", [])
            events.extend(Event.from_dict(e) for e in page_data)
            if page >= res.get("pages", page) or len(page_data) < 100:
                break
            page += 1
        self.active_events = events

    async def sync_characters(self) -> None:
        raw_characters = await self.api.get_my_characters()
        for raw in raw_characters:
            name = raw["name"]
            if name in self.characters:
                self.characters[name].update_from_dict(raw)
            else:
                self.characters[name] = Character(raw, api=self.api, map_db=self.map_db)

    async def sync_rate_limits(self) -> None:
        """Optional: refresh all buckets in one call instead of waiting for
        organic header updates from other requests."""
        data = await self.api.get_my_rates()
        self.rate_limiter.update_from_rates_payload(data)

    async def sync(self) -> None:
        """Full live-state refresh. Call after login and periodically --
        this is NOT TTL-cached like the database/*Store classes."""
        await self.sync_details()
        await self.sync_bank()
        await self.sync_pending_items()
        await self.sync_active_events()
        await self.sync_characters()

    def get_character(self, name: str) -> Optional[Character]:
        return self.characters.get(name)

    def __repr__(self) -> str:
        member = "member" if self.details and self.details.member else "free"
        return f"<Account username={self.details.username if self.details else '?'!r} ({member}) characters={list(self.characters)}>"
