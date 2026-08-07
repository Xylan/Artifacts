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


# Role templates, in assignment order -- position in this list (not any
# particular name string) is what determines who gets what. Applied to
# whatever roster names actually exist via build_roles() below, since
# renaming to NAME_SCHEME is best-effort (requires an active membership --
# see ensure_naming_scheme) and silently no-ops when it fails, leaving
# characters under their original names. Keying a roles dict to the literal
# NAME_SCHEME strings instead would silently break role lookups (and thus
# gather/craft assignment) whenever a rename doesn't go through.
ROLE_TEMPLATES: List[CharacterRole] = [
    CharacterRole("", "weaponcrafting", ["mining", "woodcutting", "fishing", "alchemy"]),
    CharacterRole("", "gearcrafting", ["woodcutting", "fishing", "alchemy", "mining"]),
    CharacterRole("", "jewelrycrafting", ["fishing", "alchemy", "mining", "woodcutting"]),
    CharacterRole("", "cooking", ["alchemy", "mining", "woodcutting", "fishing"]),
    CharacterRole("", None, ["mining", "fishing", "woodcutting", "alchemy"]),
]


def build_roles(character_names: List[str]) -> Dict[str, CharacterRole]:
    """Assigns ROLE_TEMPLATES positionally (sorted for determinism) to
    whatever character names actually exist on the account right now --
    the roster's *actual* names, whether or not ensure_naming_scheme
    managed to rename them. Call this after ensure_naming_scheme() (or
    after account.sync_characters(), if skipping the naming scheme
    entirely) and pass the result to TaskEngine(roles=...). Re-call and
    rebuild if the roster changes (character created/deleted/renamed) --
    don't hang onto a roles dict built from a stale roster."""
    roles: Dict[str, CharacterRole] = {}
    for i, name in enumerate(sorted(character_names)):
        template = ROLE_TEMPLATES[i % len(ROLE_TEMPLATES)]
        roles[name] = CharacterRole(name, template.primary_craft, list(template.gather_priority))
    return roles


# Default 5-character layout: one dedicated crafter per pure craft skill,
# plus a floater (Xylan5) who's eligible to help any craft skill from the
# start -- useful both before allowance opens things up for everyone else,
# and to fill in the leftover slot (4 pure craft skills, 5 characters).
# Kept for callers that want a roles dict before a live roster exists
# (docs/tests/etc). Live code should prefer build_roles(actual_names).
DEFAULT_ROLES: Dict[str, CharacterRole] = {
    name: CharacterRole(name, t.primary_craft, list(t.gather_priority))
    for name, t in zip(NAME_SCHEME, ROLE_TEMPLATES)
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
