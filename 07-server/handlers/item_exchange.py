"""Atomic source-configured ordinary and star-source item exchange.

The client supplies only rule IDs and quantities. Costs, rewards, star-source
reserves and capacity limits are all recomputed from local authoritative data.
No exchange path creates cards, advances tasks, or calls the shop handlers.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache

from admin_resources import (INT_MAX, apply_resource, balance, maximum,
                             resource_key, resource_pushes)
from database import StorageError
from equip_service import config_record
from handlers.cards_items import indexed, keyed_config, row
from handlers.shop import catalog
from server_core import Reply, register


def integer(value, label, minimum=1, maximum=INT_MAX):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError(label + " outside supported range")
    return value


def configured_items(value, label):
    if not isinstance(value, list) or not value:
        raise StorageError("Missing configured " + label)
    result = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2:
            raise StorageError("Unsupported configured " + label + " entry")
        cfgid = integer(entry[0], label + " item")
        amount = integer(entry[1], label + " amount")
        resource_key("item:" + str(cfgid))
        result.append((cfgid, amount))
    return tuple(result)


@lru_cache(maxsize=1)
def exchange_rules():
    result = {}
    for identifier, source in catalog("cfgCfgItemExchange.lua").items():
        if not isinstance(source, dict) or integer(source.get("id"), "exchange id") != identifier:
            raise StorageError("Exchange configuration ID mismatch")
        if source.get("key") not in (identifier, str(identifier)):
            raise StorageError("Exchange configuration key mismatch")
        kind = integer(source.get("type"), "exchange type", maximum=2)
        costs = configured_items(source.get("costs"), "exchange cost")
        gets = configured_items(source.get("gets"), "exchange reward")
        gets1 = None
        if kind == 2:
            gets1 = configured_items(source.get("gets1"), "alternate exchange reward")
            if costs != ((identifier, 1),):
                raise StorageError("Star-source rule must consume its own item")
            item = keyed_config("cfgItemInfo.lua", identifier)
            related = item.get("dy_arr")
            if (not isinstance(related, list) or not related or
                    any(isinstance(value, bool) or not isinstance(value, int) or value < 1
                        for value in related)):
                raise StorageError("Star-source item lacks related fighters")
        elif source.get("gets1"):
            raise StorageError("Ordinary exchange cannot define alternate rewards")
        result[identifier] = {"id": identifier, "type": kind, "costs": costs,
                              "gets": gets, "gets1": gets1}
    if not result or any(identifier not in result for identifier in range(1001, 1006)):
        raise StorageError("Required ordinary exchange rules are unavailable")
    if 1006 in result:
        raise StorageError("Unverified legacy exchange rule 1006 must not be enabled")
    return result


def checked_add(total, value, label):
    value = integer(value, label, minimum=0)
    if total > INT_MAX - value:
        raise StorageError(label + " integer overflow")
    return total + value


def aggregate(target, rows, count, label):
    count = integer(count, "exchange quantity")
    for cfgid, per_exchange in rows:
        if per_exchange > INT_MAX // count:
            raise StorageError(label + " integer overflow")
        target[cfgid] = checked_add(target.get(cfgid, 0), per_exchange * count, label)


def progression_cost(infos, current, label):
    if not isinstance(infos, list) or not infos:
        raise StorageError("Missing configured " + label)
    current = integer(current, label + " level", maximum=255)
    total = 0
    while current <= len(infos):
        tier = indexed(infos, current)
        if not isinstance(tier, dict):
            raise StorageError("Malformed configured " + label)
        if "costNum" not in tier:
            break
        amount = integer(tier["costNum"], label + " cost", minimum=0)
        if total > INT_MAX - amount:
            raise StorageError(label + " reserve overflow")
        total += amount
        current += 1
    return total


def main_talent_level(card):
    skills = card.get("skills", {})
    if not isinstance(skills, dict):
        raise StorageError("Persisted fighter skills must be a map")
    levels = []
    for key, data in skills.items():
        if not isinstance(data, dict):
            raise StorageError("Malformed persisted fighter skill")
        identifier = data.get("id", int(key) if str(key).isdigit() else None)
        identifier = integer(identifier, "fighter skill id")
        skill = config_record("cfgskill.lua", identifier)
        if skill.get("main_type") == 2:
            levels.append(integer(skill.get("lv"), "main talent level", maximum=255))
    if len(levels) > 1:
        raise StorageError("Fighter has multiple main-talent skills")
    return levels[0] if levels else 1


def required_reserve(state, cfgid):
    item = keyed_config("cfgItemInfo.lua", cfgid)
    related = item.get("dy_arr")
    if not isinstance(related, list) or not related:
        raise StorageError("Star-source item lacks related fighters")
    related = set(related)
    reserve = 0
    for card in state.get("cards", []):
        if not isinstance(card, dict):
            raise StorageError("Malformed persisted fighter")
        if card.get("cfgid") not in related:
            continue
        fighter = keyed_config("cfgCardData.lua", card["cfgid"])
        quality = integer(fighter.get("quality"), "fighter quality", maximum=255)
        mix = card.get("mix_data") or {}
        if not isinstance(mix, dict):
            raise StorageError("Persisted fighter mix_data must be a map")
        core = progression_cost(row("cfgCfgCardCoreLv.lua", quality)["infos"],
                                mix.get("cl", 1), "core")
        talent = progression_cost(row("cfgCfgMainTalentSkillUpgrade.lua", quality)["infos"],
                                  main_talent_level(card), "main talent")
        reserve = checked_add(reserve, checked_add(core, talent, "fighter reserve"),
                              "star-source reserve")
    return reserve


def request_plan(fields):
    if not isinstance(fields, dict) or not set(fields).issubset({"exchanges", "card_pool_id", "ty"}):
        raise StorageError("Unexpected ExchangeItem fields")
    exchanges = fields.get("exchanges")
    if not isinstance(exchanges, list) or not exchanges:
        raise StorageError("ExchangeItem requires at least one exchange")
    rules = exchange_rules()
    selected, seen = [], set()
    for request in exchanges:
        if (not isinstance(request, dict) or
                not set(request).issubset({"id", "num", "type", "c_id", "eSkills"})):
            raise StorageError("Unexpected exchange reward fields")
        identifier = integer(request.get("id"), "exchange id")
        count = integer(request.get("num"), "exchange quantity")
        if request.get("type", 2) != 2:
            raise StorageError("Exchange reward type must be ITEM")
        if request.get("c_id", 0) != 0 or request.get("eSkills", []) != []:
            raise StorageError("Exchange request contains unsupported object fields")
        if identifier in seen:
            raise StorageError("Duplicate exchange rule")
        rule = rules.get(identifier)
        if rule is None:
            raise StorageError("Unknown exchange rule")
        seen.add(identifier)
        selected.append((rule, count))
    kinds = {rule["type"] for rule, _ in selected}
    if len(kinds) != 1:
        raise StorageError("Mixed exchange types are not allowed")
    kind = kinds.pop()
    card_pool = fields.get("card_pool_id")
    ty = fields.get("ty")
    if kind == 1:
        if len(selected) != 1:
            raise StorageError("Ordinary exchange accepts exactly one rule")
        if ty not in (None, 0):
            raise StorageError("Ordinary exchange cannot select a star-source reward type")
        if card_pool is not None:
            card_pool = integer(card_pool, "card pool id", minimum=0)
        reward_key = "gets"
    else:
        if card_pool not in (None, 0):
            raise StorageError("Star-source exchange cannot target a card pool")
        ty = integer(ty, "star-source exchange type", maximum=2)
        reward_key = "gets" if ty == 1 else "gets1"
    costs, rewards = {}, {}
    for rule, count in selected:
        aggregate(costs, rule["costs"], count, "exchange cost")
        aggregate(rewards, rule[reward_key], count, "exchange reward")
    return kind, card_pool, ty, costs, rewards


def preflight(state, costs, rewards):
    net = {}
    for cfgid in set(costs) | set(rewards):
        canonical, canonical_id = resource_key("item:" + str(cfgid))
        before = balance(state, canonical, canonical_id)
        cost = costs.get(cfgid, 0)
        if cost > before:
            raise StorageError("Insufficient items for exchange")
        after = before - cost + rewards.get(cfgid, 0)
        if after < 0 or after > maximum(state, canonical, canonical_id):
            raise StorageError("Exchange reward exceeds resource capacity")
        if canonical in net and net[canonical] != after:
            raise StorageError("Conflicting exchange resource mirror")
        net[canonical] = after


@register("ClientProto:ExchangeItem")
async def exchange_item(ctx, fields):
    uid = ctx.require_login()
    kind, card_pool, ty, costs, rewards = request_plan(fields)
    with ctx.store.transaction(uid) as tx:
        if kind == 2:
            for cfgid, amount in costs.items():
                available = int(tx.state["inventory"].get(str(cfgid), 0)) - required_reserve(tx.state, cfgid)
                if amount > available:
                    raise StorageError("Requested star-source quantity exceeds expendable balance")
        preflight(tx.state, costs, rewards)
        changed = []
        for cfgid, amount in costs.items():
            changed.append(apply_resource(tx, "item:" + str(cfgid), "add", -amount))
        for cfgid, amount in rewards.items():
            changed.append(apply_resource(tx, "item:" + str(cfgid), "add", amount))
        rendered = [{"id": cfgid, "num": amount, "type": 2}
                    for cfgid, amount in rewards.items()]
        result = resource_pushes(tx.state, changed)
        response = {"rewards": deepcopy(rendered)}
        if kind == 1 and card_pool is not None:
            response["card_pool_id"] = card_pool
        if kind == 2:
            response["ty"] = ty
        result.append(Reply("ClientProto:ExchangeItemRet", response))
    return result


# Fail startup if any source rule is malformed or references unsupported items.
exchange_rules()
