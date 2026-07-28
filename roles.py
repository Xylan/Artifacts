#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
roles.py: Character naming scheme + skill-role distribution for
task_runner.TaskEngine.

Skill counts verified against openapi.json:
  - CraftSkill enum (Item.craft.skill): weaponcrafting, gearcrafting,
    jewelrycrafting, cooking, woodcutting, mining, alchemy   (7 values)
  - GatheringSkill enum (Resource.skill): mining, woodcutting, fishing,
    alchemy                                                   (4 values)
  - Skill enum (Task.skill, /tasks/list): weaponcrafting, gearcrafting,
    jewelrycrafting, cooking, woodcutting, mining, alchemy, fishing
    (8 values -- the union of the two above)

So there are 4 "pure" crafting skills with no gathering counterpart
(weaponcrafting, gearcrafting, jewelrycrafting, cooking) and 4 gathering
skills (mining, woodcutting, fishing, alchemy) -- three of which
(mining/woodcutting/alchemy) also double as CraftSkill values, since
ore/logs/herbs are refined into bars/planks/potions using that same skill.
Combat skills are intentionally excluded from role assignment for now; see
COMBAT_SKILLS below for where fight-based tasks should plug in once a
win-probability predictor exists for POST /simulation/fight.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Deliberately empty placeholder -- combat isn't a "skill" with a level in
# the same sense (it's character.level), and drops require a win-probability
# check against /simulation/fight before a monster can be safely tasked.
# Wire combat orders in here once that predictor exists.
COMBAT_SKILLS: tuple = ()

PURE_CRAFT_SKILLS = ["weaponcrafting", "gearcrafting", "jewelrycrafting", "cooking"]
GATHER_SKILLS = ["mining", "woodcutting", "fishing", "alchemy"]

NAME_SCHEME = ["Xylan1", "Xylan2", "Xylan3", "Xylan4", "Xylan5"]

# Skill level of the *primary* owner of a pure craft skill at which OTHER
# characters are also allowed to start leveling that same skill (the
# "allowance system"). Tune freely.
CRAFT_ALLOWANCE_LEVEL = 20


@dataclass
class CharacterRole:
    name: str
    primary_craft: Optional[str] = None                       # one of PURE_CRAFT_SKILLS, or None (floater)
    gather_priority: List[str] = field(default_factory=list)  # cascading order, best-first


# Default 5-character layout: one dedicated crafter per pure craft skill,
# plus a floater (Xylan5) who's eligible to help any craft skill from the
# start -- useful both before allowance opens things up for everyone else,
# and to fill in the leftover slot (4 pure craft skills, 5 characters).
DEFAULT_ROLES: Dict[str, CharacterRole] = {
    "Xylan1": CharacterRole("Xylan1", "weaponcrafting", ["mining", "woodcutting", "fishing", "alchemy"]),
    "Xylan2": CharacterRole("Xylan2", "gearcrafting", ["woodcutting", "fishing", "alchemy", "mining"]),
    "Xylan3": CharacterRole("Xylan3", "jewelrycrafting", ["fishing", "alchemy", "mining", "woodcutting"]),
    "Xylan4": CharacterRole("Xylan4", "cooking", ["alchemy", "mining", "woodcutting", "fishing"]),
    "Xylan5": CharacterRole("Xylan5", None, ["mining", "fishing", "woodcutting", "alchemy"]),
}


def primary_owner_of(craft_skill: str, roles: Dict[str, CharacterRole] = DEFAULT_ROLES) -> Optional[str]:
    """Name of the character who 'owns' a pure craft skill, or None if
    nobody's been assigned it (in which case it's open to whoever qualifies)."""
    for role in roles.values():
        if role.primary_craft == craft_skill:
            return role.name
    return None


def gather_rank(character_name: str, skill: str, roles: Dict[str, CharacterRole] = DEFAULT_ROLES) -> int:
    """Lower is better. Used only as a tie-breaker between multiple
    equally-eligible idle characters for the *same* gather order -- overlap
    itself is always allowed (any character meeting the level requirement
    can help), this just decides who "should" prefer it."""
    role = roles.get(character_name)
    if not role or skill not in role.gather_priority:
        return len(GATHER_SKILLS)
    return role.gather_priority.index(skill)


async def ensure_naming_scheme(account, api, names: List[str] = NAME_SCHEME) -> None:
    """Renames/creates characters so account.characters keys match `names`.
    Matches existing characters to wanted names positionally (fine for a
    small fixed roster). POST rename requires an active membership and
    451s otherwise -- rather than firing each rename and catching that per
    character, we check account.details.member up front and skip the whole
    rename loop (logged once) when the account isn't a member, since every
    attempt would 451 anyway. Character creation for missing names still
    proceeds regardless of membership."""
    from client import APIError

    existing = sorted(account.characters.keys())
    wanted = list(names)

    is_member = bool(account.details and account.details.member)
    if not is_member:
        needs_rename = any(old != new for old, new in zip(existing, wanted))
        if needs_rename:
            print("[roles] Account is not a member; skipping rename attempts (keeping original names).")
    else:
        for old_name, new_name in zip(existing, wanted):
            if old_name == new_name:
                continue
            character = account.characters[old_name]
            try:
                print(f"[roles] Renaming '{old_name}' -> '{new_name}'...")
                await api.rename_character(character, new_name)
                character.name = new_name
                account.characters[new_name] = account.characters.pop(old_name)
            except APIError as e:
                print(f"[roles] Could not rename '{old_name}' -> '{new_name}' ({e}); keeping original name.")

    missing = wanted[len(existing):]
    if missing:
        skin = "men1"
        try:
            skins_res = await api.get_skins(size=100)
            skin_list = skins_res.get("data", []) if isinstance(skins_res, dict) else skins_res
            default_skin = next((s["code"] for s in skin_list if s.get("default")), None)
            skin = default_skin or (skin_list[0]["code"] if skin_list else skin)
        except Exception as e:
            print(f"[roles] Could not fetch skin catalog ({e}); defaulting to skin={skin!r}.")

        for name in missing:
            try:
                print(f"[roles] Creating missing character '{name}'...")
                await api.create_character(name, skin)
            except APIError as e:
                print(f"[roles] Could not create '{name}' ({e}).")

        await account.sync_characters()
