"""副天赋（SubTalent）服务端实现。

权威（只读）
  opcode：03-unpack/lua/device-luascripts/GMsgNo.lua:440-442
    PlayerProto:UpgradeSubTalent 2620 / Ret 2621；PlayerProto:SetUseSubTalent 2622（无 Ret）
  线格式：GameMsg.lua:2193-2207
    UpgradeSubTalent{cid:uint, index:byte} → Ret{cid:uint, index:byte}
    SetUseSubTalent{cid:uint, indexs:array|uint}（无 Ret，两者都靠卡牌更新推送生效）
  客户端消费：PlayerProto.lua:765-790（Ret 只弹提示，卡数据靠 CardUpdate）、
    RoleTalent.lua:82-141（升级按钮/材料显示）、RoleCenter.lua:249-353（槽位/装备）、
    CharacterCardsData.lua:582-585（GetDeputyTalent）、861-868、1167-1187
  配置：cfgCfgSubTalentOpenCnt / SkillPool / Skill / Material / TypeEnu，
    卡→天赋池为客户端 cfgCardData.lua 的 CardData[cfgid].subTfSkills[1]
    （客户端 556 条为准，admin 模板只作核对）

数据表：07-server/data/sub-talent.json（02-tools/scripts/sub-talent-build.py 生成）

本地策略，无官服样本
  * 槽位物化：break_level=B 时把 had[n] 补成 pool.ids[n].id（n=1..cnt(B)）；只补空缺，
    绝不覆盖已学会的 id（避免抹掉玩家升级进度）。
  * 升级 2620：扣 CfgSubTalentMaterial[costId].costs（costAdds 存在时同样并入），
    had[index] = next_id，use 中等于旧 id 的项改写为新 id。
  * 装备 2622：校验每个元素 ∈ had∪{0}、长度 ≤ 4、非零不重复、非零个数 ≤ cnt。
  * 失败一律抛 StorageError → server_core/error_policy 转成 SystemProto:Tips 且保持会话。
  * 查不到 pool 的卡（当前 81 个 pool id 在池表里不存在，例如主角卡 71020）静默跳过，
    不物化槽位、不报错。

RandSubTalent / SetReplaceSubTalent / OpenSubTalentSlot 在本客户端 GMsgNo.lua 里没有 opcode，
属死代码，本模块按规格不实现。
"""
from __future__ import annotations

from copy import deepcopy
import json
from functools import lru_cache
from pathlib import Path

from access_policy import require_feature
from config_codec import app_path
from database import StorageError
from server_core import Reply, register
from handlers.cards_items import (card_update_replies, costs_from_rows, debit, integer,
                                  owned_card, resource_replies)

DATA = app_path('data', 'sub-talent.json')
UPGRADE_REQUEST = 'PlayerProto:UpgradeSubTalent'      # GMsgNo.lua:440 (2620)
UPGRADE_REPLY = 'PlayerProto:UpgradeSubTalentRet'     # GMsgNo.lua:441 (2621)
USE_REQUEST = 'PlayerProto:SetUseSubTalent'           # GMsgNo.lua:442 (2622)
# 本地策略推断（审计 F4）：客户端从不断言 use 长度、协议 array|uint 也不限长；4 只是
# CfgSubTalentOpenCnt 的上限 max(cnt)，用于给 use 补 0 占位。had 恰好相反，绝不能补 0（F1）。
USE_SLOTS = 4
CATALOG_KEYS = ('openCnt', 'pools', 'skills', 'materials', 'cardPools')


@lru_cache(maxsize=1)
def catalog() -> dict:
    """Load the generated table on demand; a missing file must never break import."""
    try:
        payload = json.loads(DATA.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise StorageError('副天赋数据表缺失或损坏：' + DATA.name) from error
    if not isinstance(payload, dict) or any(key not in payload for key in CATALOG_KEYS):
        raise StorageError('副天赋数据表结构不完整：' + DATA.name)
    return payload


def open_slots(break_level) -> int:
    """CfgSubTalentOpenCnt ladder: break 1..7 -> 0,1,2,3,4,4,4 (spec §3)."""
    try:
        level = int(break_level)
    except (TypeError, ValueError):
        return 0
    return int(catalog()['openCnt'].get(str(level), 0))


def pool_id(cfg):
    """CardData[cfgid].subTfSkills[1]; the client table is the authority (spec §3.1)."""
    skills = cfg.get('subTfSkills') if isinstance(cfg, dict) else None
    if not isinstance(skills, list) or not skills:
        return None
    try:
        return int(skills[0])
    except (TypeError, ValueError):
        return None


def starting_ids(pool):
    """The pool's four starting talent ids, ordered by their declared index."""
    if pool is None:
        return None
    entry = catalog()['pools'].get(str(int(pool)))
    if not isinstance(entry, dict) or not isinstance(entry.get('ids'), list):
        return None
    rows = sorted(entry['ids'], key=lambda row: int(row['index']))
    return [int(row['id']) for row in rows if 'id' in row]


def entry(table: str, key):
    return catalog()[table].get(str(int(key)))


def clean(values):
    """Keep only real non-negative ints; booleans and junk become 0 (never crash on save data)."""
    return [int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
            for value in values]

def active_ids(card) -> list:
    """已装备的副天赋 id（use 的非零项）。

    收敛口径：未装备（use 全 0）没有任何属性影响 →
    升级/跃升/出战必须放行；已装备才有影响 → 必须按 CardCalculator.lua:410-443 精确计算。
    cards_items.recalculate_bare_hp、battle.team_cards 与 equipment_stats 共用这一判定。
    依据：CardCalculator.lua:413-414（for pairs(useSubTalents) do if tId > 0 then）。
    """
    data = card.get('sub_talent')
    if not isinstance(data, dict):
        return []
    return [int(value) for value in clean(data.get('use', [])) if value]


def slots_of(card) -> dict:
    """The card's {'had': [...], 'use': [...]}; created in place when absent."""
    data = card.get('sub_talent')
    if not isinstance(data, dict):
        data = {}
        card['sub_talent'] = data
    if not isinstance(data.get('had'), list):
        data['had'] = []
    if not isinstance(data.get('use'), list):
        data['use'] = []
    return data


def ensure_slots(card, cfg) -> bool:
    """物化副天赋槽位（本地策略，无官服样本，spec §4.1/§3.1）。

    break_level=B 时把 had[n] 补成 pool.ids[n].id（n=1..cnt(B)），use 补足到 4 且丢弃不再
    合法的 id。只补空缺，已学会的 id 永远保留；查不到 pool（cfg 无 subTfSkills 或池表里没有
    该 id）时整个函数不做任何写入并返回 False——不物化、不报错。
    """
    pool = pool_id(cfg)
    starting = starting_ids(pool)
    if not starting:
        return False
    data = slots_of(card)
    before = (list(data['had']), list(data['use']))
    count = open_slots(card.get('break_level', 1))
    limit = min(count, len(starting))
    # 审计 F1：had 是「位置 = 槽下标」的真值数组，绝不能含 0 —— 客户端 RoleCenter.lua:262
    # 用 cnt >= n and had[n] 判可见，而 Lua 里 0 是真值，会被当成「已解锁、id=0」，
    # RoleInfoTalentItem2.lua:34-35 随即对 nil 配置取 cfg.icon 直接报错。
    # 脏值、负数、越界项一律丢弃；只有 use 允许补 0 占位。
    slots = clean(data['had'])[:limit]
    if len(slots) < limit:
        slots.extend([0] * (limit - len(slots)))
    for position in range(1, limit + 1):
        if not slots[position - 1]:
            slots[position - 1] = starting[position - 1]
    had = [int(value) for value in slots if value]
    learned = set(had)
    use = clean(data['use'])[:USE_SLOTS]
    use = [value if value in learned else 0 for value in use]
    use.extend([0] * (USE_SLOTS - len(use)))
    data['had'], data['use'] = had, use
    return (had, use) != before


def material_costs(skill) -> dict:
    """CfgSubTalentMaterial[costId].costs plus the optional costAdds coin (RoleTalent.lua:101-110)."""
    material = entry('materials', skill.get('costId'))
    if material is None:
        raise StorageError('副天赋材料配置缺失')
    costs = costs_from_rows(material.get('costs', []))
    if material.get('costAdds'):
        for cfgid, amount in costs_from_rows(material['costAdds']).items():
            costs[cfgid] = costs.get(cfgid, 0) + amount
    return costs


def payable(tx, costs):
    for cfgid, amount in costs.items():
        if amount and tx.item_count(cfgid) < amount:
            raise StorageError('副天赋升级材料不足')


@register(UPGRADE_REQUEST)
async def upgrade_sub_talent(ctx, fields):
    """2620 升级：had[index] → CfgSubTalentSkill[had[index]].next_id，扣材料并改写 use。"""
    uid = ctx.require_login()
    identifier = integer(fields.get('cid'))
    index = integer(fields.get('index'), minimum=1, maximum=255)
    with ctx.store.transaction(uid) as tx:
        # 审计 F6：客户端升级前置门槛是 MenuMgr:CheckModelOpen(special,"special20")
        # （CharacterCardsData.lua:1169），与 cards_items.py:378/475 的主天赋线保持一致。
        require_feature(tx.state, 'special20')
        card, cfg = owned_card(tx.state, identifier)
        ensure_slots(card, cfg)
        data = slots_of(card)
        if index > open_slots(card.get('break_level', 1)):
            raise StorageError('该副天赋槽位尚未开放')
        had = data['had']
        current = int(had[index - 1]) if index <= len(had) else 0
        if not current:
            raise StorageError('该副天赋槽位尚未解锁')
        skill = entry('skills', current)
        if skill is None:
            raise StorageError('副天赋技能配置缺失')
        advanced = skill.get('next_id')
        if advanced is None:
            raise StorageError('该副天赋已满级')
        if entry('skills', advanced) is None:
            raise StorageError('副天赋升级目标配置缺失')
        costs = material_costs(skill)
        payable(tx, costs)
        debit(tx, costs)
        had[index - 1] = int(advanced)
        data['use'] = [int(advanced) if value == current else value for value in data['use']]
        replies = resource_replies(tx.state, {key: -value for key, value in costs.items()})
        replies.append(Reply(UPGRADE_REPLY, {'cid': card['cid'], 'index': index}))
        replies.extend(card_update_replies(ctx, [deepcopy(card)], int(tx.state.get('store_exp', 0))))
    return replies


@register(USE_REQUEST)
async def set_use_sub_talent(ctx, fields):
    """2622 装备：校验后写回 use；无 Ret，客户端靠卡牌更新刷新。"""
    uid = ctx.require_login()
    identifier = integer(fields.get('cid'))
    indexs = fields.get('indexs')
    if not isinstance(indexs, list) or len(indexs) > USE_SLOTS:
        raise StorageError('副天赋装备列表不合法')
    values = []
    for value in indexs:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StorageError('副天赋装备列表不合法')
        values.append(value)
    with ctx.store.transaction(uid) as tx:
        require_feature(tx.state, 'special20')   # 审计 F6，同 2620
        card, cfg = owned_card(tx.state, identifier)
        ensure_slots(card, cfg)
        data = slots_of(card)
        learned = {value for value in clean(data['had']) if value}
        equipped = [value for value in values if value]
        if any(value not in learned for value in equipped):
            raise StorageError('只能装备已解锁的副天赋')
        if len(set(equipped)) != len(equipped):
            raise StorageError('副天赋不能重复装备')
        if len(equipped) > open_slots(card.get('break_level', 1)):
            raise StorageError('装备数量超过已开放槽位')
        values.extend([0] * (USE_SLOTS - len(values)))
        data['use'] = values
        replies = card_update_replies(ctx, [deepcopy(card)], int(tx.state.get('store_exp', 0)))
    return replies
