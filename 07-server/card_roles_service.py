"""Keep archive-role progression consistent with the player's owned cards."""
from functools import lru_cache
from copy import deepcopy
import json
from pathlib import Path

from config_codec import app_path
from database import StorageError

DATA = app_path("data")


@lru_cache(maxsize=1)
def card_roles():
    # skins-catalog includes commander and alternate-form templates which are
    # intentionally absent from construction pools.
    rows = json.loads((DATA / "skins-catalog.json").read_text("utf-8"))["cards"]
    rows.update(json.loads((DATA / "gacha-cards.json").read_text("utf-8")))
    rows.update(json.loads((DATA / "admin-role-templates.json").read_text("utf-8")))
    return {int(cfgid): int(cfg["role_id"]) for cfgid, cfg in rows.items()}


@lru_cache(maxsize=1)
def skill_types():
    """sSkillData.type per skill id, copied from cfgskill.lua's main_type field.

    05-protocol/samples/tcp/session1-stream02-s2c-frame0092..0097 are official
    PlayerProto:CardAdd frames: they carry {id, exp, type} and the type equals the client
    table's main_type (500100401->3, 500101304->1, 4500104->2).  The table's own 'type'
    column is a different code and must not be used here.
    """
    payload = json.loads((DATA / "card-skill-types.json").read_text("utf-8"))
    return {int(skill): int(kind) for skill, kind in payload["types"].items()}


def card_skill_type(skill_id):
    """The SkillMainType for one skill id, or None when the client table has no row.

    cfgCardData.lua references a few ids that cfgskill.lua does not contain (for example
    card 30210 沙椤 lists 302100101/302100201/302100301/302101301/4302101, none of which
    exist in the skill table).  Such an entry must not carry a type: the client's
    GetSkillByType would then call Cfgs.skill:GetByID on a missing row and throw.
    """
    return skill_types().get(int(skill_id))


def build_card_skills(skill_ids):
    """Return {'<id>': {id, exp[, type]}} as the official CardAdd payload carries it."""
    entries = {}
    for skill in skill_ids:
        entry = {"id": int(skill), "exp": 0}
        kind = card_skill_type(skill)
        if kind is not None:
            entry["type"] = kind
        entries[str(int(skill))] = entry
    return entries


def synchronize_card_skill_types(state):
    """Fill sSkillData.type on every owned card and return the repaired cards.

    RoleInfo.lua:543 -> CharacterCardsData:GetSkillsForShow -> GetSkillByType compares
    data.skills[].type against SkillMainType, so a card whose skills carry only id/exp shows
    an empty 武装技能 panel and throws at RoleInfo.lua:833 when the skill button is pressed.
    Historical saves written before this repair have exactly that shape.
    """
    changed = []
    for card in state.get("cards", []):
        skills = card.get("skills")
        if not isinstance(skills, dict):
            raise StorageError("Owned card has no skill map")
        touched = False
        for entry in skills.values():
            if not isinstance(entry, dict) or "id" not in entry:
                raise StorageError("Owned card has a malformed skill entry")
            expected = card_skill_type(entry["id"])
            if expected is None:
                # No client row for this id: keep it typeless so the client filters it out
                # instead of indexing a nil config.
                if "type" in entry:
                    entry.pop("type")
                    touched = True
                continue
            if entry.get("type") != expected:
                entry["type"] = expected
                touched = True
        if touched:
            changed.append(card)
    return changed


def synchronize(state):
    """Run every config-derived repair; both are called, never short-circuited."""
    roles = synchronize_break_levels(state)
    skills = synchronize_card_skill_types(state)
    return bool(roles) or bool(skills)


def synchronize_break_levels(state):
    """Mirror each owned role's highest card break into sCardRoleData.b_lv.

    The client treats numeric zero as a real value (Lua's ``0`` is truthy), so
    an owned role with ``b_lv == 0`` fails even the base-skin level-1 check.
    """
    maxima = {}
    mapping = card_roles()
    for card in state.get("cards", []):
        cfgid = card.get("cfgid")
        role_id = mapping.get(cfgid)
        if role_id is None:
            raise StorageError("Owned card has no archive-role mapping")
        level = card.get("break_level", 1)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 7:
            raise StorageError("Owned card has invalid break level")
        maxima[role_id] = max(maxima.get(role_id, 0), level)

    changed = []
    rows = state.setdefault("card_roles", [])
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    for role_id, level in maxima.items():
        row = by_id.get(role_id)
        if row is None or not isinstance(row.get("data"), dict):
            raise StorageError("Owned card has no valid archive-role state")
        current = row["data"].get("b_lv", 0)
        if isinstance(current, bool) or not isinstance(current, int) or not 0 <= current <= 7:
            raise StorageError("Archive role has invalid break level")
        # This is historical unlock progress: repair/advance it, never revoke a
        # skin merely because a particular card instance later disappears.
        if current < level:
            row["data"]["b_lv"] = level
            changed.append(row)
    return changed


def repaired_state(store, uid):
    """Return a synchronized state, writing only when legacy data needs repair."""
    state = store.get_player(uid)
    if not synchronize(state):
        return state
    with store.transaction(uid) as tx:
        synchronize(tx.state)
        return deepcopy(tx.state)
