"""Owned-card progression and supported consumables from local Lua config.

No remote fallback, arbitrary rewards, or client-supplied stat/cost authority.
Source rules and remaining limits are documented in this module.
"""
from copy import deepcopy
from functools import lru_cache
import math
import re
import sys
import time

import reply_chunks
from database import StorageError
from access_policy import require_feature
from card_roles_service import card_skill_type
from server_core import Reply, register
from seed_generator import LUA_DIR, balanced_table, parse_lua_table, python_data, selected_record


@lru_cache(maxsize=16)
def array_config(filename):
    text = (LUA_DIR / filename).read_text('utf-8-sig')
    match = re.search(r'_G\[[^\]]+\]\s*=\s*\{', text)
    if match is None:
        raise StorageError('Missing local configuration table: ' + filename)
    result = python_data(parse_lua_table(balanced_table(text, match.end() - 1)))
    if not isinstance(result, list) or not result:
        raise StorageError('Expected local array configuration: ' + filename)
    return {int(row['id']): row for row in result}


@lru_cache(maxsize=16)
def config_text(filename):
    """One per-process copy of a local Lua config file, like array_config's cache.

    keyed_config used to call selected_record, which re-reads and SHA-256s the
    whole file for every uncached id. cfgskill.lua is 6.1 MB, so one lookup cost
    ~15 ms and the one-key batch spent 6.79 s of its 6.85 s inside 1174
    keyed_config calls (measured on a copy of live save uid 900000002 with 127
    fighters). keyed_config already discards selected_record's evidence tuple,
    so caching the text keeps the exact same regex/balanced_table/
    parse_lua_table path for the selected record and only drops the repeated
    6 MB read and digest. No validation is relaxed.
    """
    return (LUA_DIR / filename).read_text('utf-8-sig')


@lru_cache(maxsize=1024)
def keyed_config(filename, key):
    try:
        key = int(key)
        text = config_text(filename)
        match = re.search(r'\[' + re.escape(str(key)) + r'\]\s*=\s*\{', text)
        if match is None:
            raise ValueError('Missing configuration record ' + filename + ':' + str(key))
        record = python_data(parse_lua_table(balanced_table(text, match.end() - 1)))
        if not isinstance(record, dict):
            raise ValueError('Expected a named-field configuration record: ' + filename)
    except (ValueError, OSError) as error:
        raise StorageError('Required local configuration unavailable: ' + filename) from error
    return record


def row(filename, key):
    value = array_config(filename).get(int(key))
    if value is None:
        raise StorageError('Configuration index unavailable: ' + filename)
    return value


def integer(value, minimum=1, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError('Integer outside supported range')
    return value


def owned_card(state, cid):
    cid = integer(cid)
    card = next((c for c in state['cards'] if c['cid'] == cid), None)
    if card is None:
        raise StorageError('Fighter is not owned')
    if int(card.get('break_level', 1)) < 1 or int(card.get('intensify_level', 1)) < 1:
        raise StorageError('Invalid card baseline; migrate pre-fix local seed indices')
    return card, keyed_config('cfgCardData.lua', card['cfgid'])


def indexed(container, index):
    if isinstance(container, list) and 1 <= index <= len(container):
        return container[index - 1]
    if isinstance(container, dict):
        value = container.get(index, container.get(str(index)))
        if value is not None:
            return value
    raise StorageError('Configuration progression index unavailable')


def costs_from_rows(source):
    if not isinstance(source, list):
        raise StorageError('Unsupported configured cost representation')
    costs = {}
    for entry in source:
        if not isinstance(entry, list) or len(entry) not in (2, 3):
            raise StorageError('Unsupported configured cost entry')
        if len(entry) == 3 and entry[2] != 2:  # RandRewardType.ITEM in GEnum.lua.
            raise StorageError('Non-item resource cost is not implemented')
        cfgid, amount = integer(entry[0]), integer(entry[1], 0)
        costs[cfgid] = costs.get(cfgid, 0) + amount
    return costs


def debit(tx, costs):
    for cfgid, amount in costs.items():
        if amount:
            tx.add_item(cfgid, -amount)


def resource_replies(state, deltas):
    updates = [{'id': cfgid, 'add': change, 'num': int(state['inventory'].get(str(cfgid), 0)),
                'time': 0, 'ix': 0, 'expiry': 0, 'get_infos': {}}
               for cfgid, change in deltas.items() if change]
    result = [Reply('PlayerProto:ItemUpdate', {'data': updates})] if updates else []
    if any(cfgid in (10001, 10002) for cfgid in deltas):
        result.append(Reply('LoginProto:PlrUpdate', {'infos': deepcopy(state['player'])}))
    return result


def task_updates(state, event, amount=1):
    """Advance only when the task domain is loaded, inside the same transaction."""
    module = sys.modules.get('handlers.tasks')
    return module.advance_tasks(state, event, amount) if module is not None else []


def recalculate_bare_hp(card, cfg):
    # Calculator base formula is exact for cards without equipment/weapon/talent
    # stat modifiers. Refuse the transaction until the full stat port exists.
    mix = card.get('mix_data')
    if mix is None:
        mix = {}
    if not isinstance(mix, dict):
        raise StorageError('Invalid persisted card mix_data')
    # 收敛口径：未装备的副天赋（use 全 0）没有任何属性影响，
    # 必须放行；已装备才会改变 maxhp，而这里的裸公式不含该乘区，故仍然拒绝。
    from handlers import sub_talent
    if card.get('equips') or card.get('equip_ids') or mix.get('weaponLv', 0) or sub_talent.active_ids(card):
        raise StorageError('Level/jump HP calculation with equipment, weapon or active secondary talent '
                           'is not yet implemented')
    level = row('cfgCardLevel.lua', card['level'])
    jump = row('cfgCardBreak.lua', card.get('break_level', 1))
    card['hp'] = math.floor(cfg['maxhp'] * level['maxhp'] * jump['maxhp'])


class UpgradeDenied(Exception):
    """A verified business rejection; its transaction must still roll back."""
    def __init__(self, key, *params):
        self.key, self.params = key, params
        super().__init__(key)


def upgrade_tip(error, op_id=2544, op_name='PlayerProto:CardUpgrade'):
    # GShowTipor.lua:5: OnlyParm=0; EmptyParm=1 would erase the value.
    # opId is the triggering request opcode (GMsgNo.lua:364, :1072) and strId
    # is a cfgCfgTipsSimpleChinese key, e.g. itemNumNotEnough
    # (cfgCfgTipsSimpleChinese.lua:25) whose two {} slots take the item name
    # and the required count.
    return Reply('SystemProto:Tips', {'strId': error.key, 'opId': op_id,
                 'opName': op_name,
                 'args': [{'type': 0, 'param': str(value)} for value in error.params]})


@register('PlayerProto:SetCardInfo')
async def set_card_info(ctx, fields):
    # GameMsg.lua:2318-2326 permits only the viewed/new flag. RoleInfo sends
    # this after a full card refresh; echoing CardUpdate would retrigger it.
    uid = ctx.require_login()
    if not isinstance(fields, dict) or set(fields) != {'cid', 'is_new'}:
        raise StorageError('SetCardInfo requires only cid and is_new')
    if not isinstance(fields['is_new'], bool):
        raise StorageError('SetCardInfo is_new must be a boolean')
    with ctx.store.transaction(uid) as tx:
        card, _ = owned_card(tx.state, fields['cid'])
        card['is_new'] = fields['is_new']
        result = {'cid': card['cid'], 'is_new': card['is_new']}
    return [Reply('PlayerProto:SetCardInfoRet', result)]


@register('PlayerProto:CardUpgrade')
async def upgrade(ctx, fields):
    try:
        return upgrade_transaction(ctx, fields)
    except UpgradeDenied as error:
        return [upgrade_tip(error)]


def upgrade_transaction(ctx, fields):
    uid = ctx.require_login()
    amount = integer(fields.get('use_store_exp'))
    with ctx.store.transaction(uid) as tx:
        card, cfg = owned_card(tx.state, fields.get('cid'))
        level = integer(card['level'], maximum=90)
        original_level = level
        jump = integer(card.get('break_level', 1), maximum=7)
        limits = array_config('cfgCfgCardBreakLimitLv.lua')
        maximum = limits[jump]['limitLv'] if jump in limits else limits[max(limits)]['MaxLv']
        if level >= maximum:
            raise UpgradeDenied('reachMaxLvl')
        stored = integer(tx.state.get('store_exp', 0), 0, 4294967295)
        if stored < amount:
            raise UpgradeDenied('notEnoughStoreExp', stored)
        capacity = sum(row('cfgCardLevel.lua', n)['exp'] for n in range(level, maximum)) - int(card.get('exp', 0))
        if amount > capacity:
            raise StorageError('Requested experience exceeds current level cap')
        config_costs = row('cfgCardLevel.lua', level)['costs']
        divisor, cost = config_costs
        if divisor <= 0 or len(cost) != 3 or cost[2] != 2:
            raise StorageError('Unsupported level-up cost configuration')
        debit_amount = math.floor(amount / divisor * cost[1])
        costs = {integer(cost[0]): integer(debit_amount, 0)}
        if tx.item_count(cost[0]) < debit_amount:
            item = keyed_config('cfgItemInfo.lua', cost[0])
            raise UpgradeDenied('itemNumNotEnough', item.get('name', str(cost[0])), debit_amount)
        debit(tx, costs)
        tx.state['store_exp'] -= amount
        experience = int(card.get('exp', 0)) + amount
        while level < maximum and experience >= row('cfgCardLevel.lua', level)['exp']:
            experience -= row('cfgCardLevel.lua', level)['exp']
            level += 1
        card.update(level=level, exp=experience)
        recalculate_bare_hp(card, cfg)
        result = resource_replies(tx.state, {key: -value for key, value in costs.items()})
        result.extend(task_updates(tx.state, 'card_upgrade', int(level > original_level)))
        result.append(Reply('PlayerProto:CardUpgradeRet', {'card': deepcopy(card), 'store_exp': tx.state['store_exp']}))
    return result


@register('PlayerProto:CardBreak')
async def card_break(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        card, cfg = owned_card(tx.state, fields.get('cid'))
        jump = integer(card.get('break_level', 1), maximum=7)
        if jump >= len(array_config('cfgCardBreak.lua')):
            raise StorageError('Maximum jump level reached')
        limit = row('cfgCfgCardBreakLimitLv.lua', jump)
        if card['level'] < limit['limitLv']:
            raise StorageError('Fighter has not reached jump level requirement')
        if jump < 5:
            material = keyed_config('cfgCardBreakMaterial.lua', cfg['break_id'] + jump - 1)
        else:
            material = indexed(row('cfgCardBreakMaterial2.lua', cfg['quality'])['infos'], jump)
        costs = costs_from_rows(material.get('materials', []))
        costs[10001] = costs.get(10001, 0) + integer(material.get('gold', 0), 0)
        debit(tx, costs)
        card['break_level'] = jump + 1
        recalculate_bare_hp(card, cfg)
        # 跃升后物化副天赋槽位（本地策略，无官服样本）。
        # 必须排在 recalculate_bare_hp 之后：那个函数会拒绝带 sub_talent 的卡（本文件:142）。
        from handlers import sub_talent
        sub_talent.ensure_slots(card, cfg)
        from card_roles_service import synchronize_break_levels
        changed_roles = synchronize_break_levels(tx.state)
        result = resource_replies(tx.state, {key: -value for key, value in costs.items()})
        result.extend(task_updates(tx.state, 'card_break'))
        result.append(Reply('PlayerProto:CardBreakRet', {'card': deepcopy(card), 'gold': tx.currency('gold')}))
        if changed_roles:
            result.append(Reply('PlayerProto:UpdateCardRole',
                                {'roles': deepcopy(changed_roles), 'is_finish': True}))
    return result


def alternative_cost(cfg, tier, mode):
    if mode == 'costNum':
        return {integer(cfg['coreItemId']): integer(tier.get('costNum', 0), 0)}
    if mode == 'costArr':
        match = next((entry for entry in tier.get('costArr', []) if entry[0] == cfg['nClass']), None)
        if match is None or len(match) != 2:
            raise StorageError('No alternate cost configured for this camp')
        return costs_from_rows([match[1]])
    raise StorageError('Unknown alternative resource selection')


@register('PlayerProto:CardCoreLv')
async def core_level(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        require_feature(tx.state, 'special1')
        card, cfg = owned_card(tx.state, fields.get('cid'))
        mix = card.get('mix_data')
        if mix is None:
            mix = card['mix_data'] = {}
        if not isinstance(mix, dict):
            raise StorageError('Invalid persisted card mix_data')
        current = integer(mix.get('cl', 1), maximum=255)
        tiers = row('cfgCfgCardCoreLv.lua', cfg['quality'])['infos']
        if current >= len(tiers):
            raise StorageError('Maximum core level reached')
        costs = alternative_cost(cfg, indexed(tiers, current), fields.get('uf'))
        debit(tx, costs)
        mix['cl'] = current + 1
        result = resource_replies(tx.state, {key: -value for key, value in costs.items()})
        result.extend(task_updates(tx.state, 'state_changed'))
        result.extend([Reply('PlayerProto:CardUpdate', {'cards': [deepcopy(card)],
                                'store_exp': int(tx.state.get('store_exp', 0))}),
                       Reply('PlayerProto:CardCoreLvRet', {'cid': card['cid'], 'cl': mix['cl']})])
    return result


def skill_record(card, skill_id, maximum_ok=False):
    skill_id = integer(skill_id)
    skills = card.get('skills', {})
    old = next((key for key, data in skills.items() if data.get('id') == skill_id), None)
    if old is None:
        raise StorageError('Current fighter does not own this skill version')
    config = keyed_config('cfgskill.lua', skill_id)
    if not config.get('next_id'):
        if maximum_ok:
            # The one-key loop stops at the end of the configured chain exactly
            # like the client's PassiveCanUpTo loop (CharacterCardsData.lua:1144)
            # instead of rejecting the whole batch.
            return old, config, None
        raise StorageError('Maximum skill level reached')
    target = keyed_config('cfgskill.lua', config['next_id'])
    if target.get('group') != config.get('group') or target['lv'] != config['lv'] + 1:
        raise StorageError('Invalid configured skill progression')
    return old, config, target


def apply_skill(card, old_key, target):
    card['skills'].pop(old_key)
    # sSkillData.type is the client's SkillMainType; omitting it hides the skill entirely.
    entry = {'id': target['id'], 'exp': 0}
    kind = card_skill_type(target['id'])
    if kind is not None:
        entry['type'] = kind
    card['skills'][str(target['id'])] = entry


def talent_step(card, cfg, skill_id, mode, maximum_ok=False):
    """Validate one passive-talent step and price it from the shared tables.

    PlayerProto:MainTalentUpgrade and PlayerProto:OneKeyMainTalentUpgrade run
    through this single rule set, so neither path can drift into its own cost.
    A configured chain end returns target=None when maximum_ok is set.
    """
    old, skill, target = skill_record(card, skill_id, maximum_ok)
    if target is None:
        return old, skill, None, {}
    if skill.get('main_type') != 2:
        raise StorageError('Only passive card talent uses this operation')
    tier = indexed(row('cfgCfgMainTalentSkillUpgrade.lua', cfg['quality'])['infos'], skill['lv'])
    return old, skill, target, alternative_cost(cfg, tier, mode)


@register('PlayerProto:CardSkillUpgrade')
async def skill_upgrade(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        require_feature(tx.state, 'special4')
        card, cfg = owned_card(tx.state, fields.get('cid'))
        old, skill, target = skill_record(card, fields.get('skill_id'))
        if skill.get('main_type') == 2:
            raise StorageError('Passive card talent requires MainTalentUpgrade')
        tier = indexed(row('cfgCardSkillExp.lua', cfg['quality'])['arr'], skill['lv'])
        if tier.get('seconds', 0) != 0:
            raise StorageError('Timed skill training is not yet implemented')
        costs = costs_from_rows(tier.get('costs', []) + tier.get('costAdds', []))
        debit(tx, costs)
        apply_skill(card, old, target)
        now = int(time.time())
        result = resource_replies(tx.state, {key: -value for key, value in costs.items()})
        result.extend(task_updates(tx.state, 'skill_upgrade'))
        result.extend([Reply('PlayerProto:CardSkillUpgradeRet', {'cid': card['cid'], 'info': {'id': skill['id'], 't_start': now, 't_end': now}}),
                       Reply('PlayerProto:CardSkillUpgradeFinishRet', {'infos': {str(card['cid']): {
                           'cid': card['cid'], 'card': deepcopy(card), 'ids': [target['id'], skill['id']]}}})])
    return result


@register('PlayerProto:MainTalentUpgrade')
async def talent_upgrade(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        require_feature(tx.state, 'special20')
        card, cfg = owned_card(tx.state, fields.get('cid'))
        old, skill, target, costs = talent_step(card, cfg, fields.get('skill_id'), fields.get('uf'))
        if target is None:
            raise StorageError('Maximum skill level reached')
        debit(tx, costs)
        apply_skill(card, old, target)
        result = resource_replies(tx.state, {key: -value for key, value in costs.items()})
        result.extend(task_updates(tx.state, 'talent_upgrade'))
        result.extend([Reply('PlayerProto:CardUpdate', {'cards': [deepcopy(card)],
                                'store_exp': int(tx.state.get('store_exp', 0))}),
                       Reply('PlayerProto:MainTalentUpgradeRet', {'cid': card['cid'], 'skill_id': skill['id'],
                           'new_skill_id': target['id'], 'uf': fields['uf']})])
    return result


def one_key_talent_steps(tx, card, cfg, skill_id):
    """Raise one selected fighter's talent as far as its own core item allows.

    The client only offers fighters whose chip balance already covers the next
    step (CharacterCardsData.lua:1109-1131) and previews the reachable level
    with the same chip-only loop (CharacterCardsData.lua:1134-1155), while the
    popup promises not to spend star dust (cfgCfgLanguage.lua:361003). The wire
    request carries no target level (GameMsg.lua:5348-5357), so the server
    repeats the shared single-step rule while the configured costNum is
    affordable and stops on the first step the player cannot pay for.
    本地策略，无官服样本: the loop, its stop-on-shortage rule and the per-card
    summary are local decisions; no official OneKey frame exists
    (05-protocol/endpoints.json:57327-57379 has empty observed_samples).
    """
    if 'coreItemId' not in cfg:
        # The client excludes such fighters from the selection
        # (CharacterCardsData.lua:1121-1123), so this is never a valid request.
        raise StorageError('Fighter has no configured talent material')
    steps, pending, current = [], None, integer(skill_id)
    while True:
        old, skill, target, costs = talent_step(card, cfg, current, 'costNum', True)
        if target is None:
            break
        if pending is None:
            pending = costs
        if any(tx.item_count(key) < amount for key, amount in costs.items()):
            break
        debit(tx, costs)
        apply_skill(card, old, target)
        steps.append((skill['id'], target['id'], costs))
        current = target['id']
        pending = None
    if not steps:
        if pending is None:
            raise StorageError('Maximum skill level reached')
        key, amount = next(iter(pending.items()))
        item = keyed_config('cfgItemInfo.lua', key)
        raise UpgradeDenied('itemNumNotEnough', item.get('name', str(key)), amount)
    return steps


def card_update_replies(ctx, cards, store_exp):
    """One PlayerProto:CardUpdate carrying every changed fighter.

    RoleMgr:CardUpdate merges the list into the owned cards and dispatches one
    CardUpdateType.DataUpdate event per frame (PlayerProto.lua:262-263,
    RoleMgr.lua:530-546), so the whole batch must stay in a single frame. That
    was measured with the project codec against a copy of live save uid
    900000002 (134 fighters, max_card_size 150): the 127-fighter batch encodes
    to 21184 B including the 4 B header, and the largest observed fighter record
    is 190 B, so even a full 150-fighter bag stays at 28513 B. The codec frame
    limit is 32767 B (02-tools/scripts/protocol_codec.py:42, server_core.py:236),
    which is the real ceiling; a longer frame would raise during server_core's
    pre-encode and close the connection with close_reason='frame_encode_failed'.
    A payload that cannot fit therefore falls back to the existing chunking
    helper instead of killing the session; every frame still merges
    incrementally and only that last-resort path pays an extra DataUpdate event.
    """
    fields = {'cards': cards, 'store_exp': store_exp}
    codec = getattr(getattr(ctx, 'server', None), 'codec', None)
    if codec is not None:
        body = codec.encode_struct('PlayerProto:CardUpdate', fields)
        if len(body) + 4 > codec.config.max_frame_size:
            return [Reply('PlayerProto:CardUpdate', frame) for frame in reply_chunks.pack_frames(
                codec, 'PlayerProto:CardUpdate', 'sCardsData', 'cards', cards,
                {'store_exp': store_exp})]
    return [Reply('PlayerProto:CardUpdate', fields)]


@register('PlayerProto:OneKeyMainTalentUpgrade')
async def one_key_talent_upgrade(ctx, fields):
    uid = ctx.require_login()
    # GameMsg.lua:5353-5357 defines exactly one field, the infoArr list of
    # sOneKeyMainTalentUpgradeInfo {cid, skill_id} built at RoleListTX.lua:78-91.
    if not isinstance(fields, dict) or set(fields) != {'infoArr'}:
        raise StorageError('OneKeyMainTalentUpgrade requires only infoArr')
    items = fields.get('infoArr')
    if not isinstance(items, list):
        raise StorageError('Invalid one-key talent batch')
    try:
        with ctx.store.transaction(uid) as tx:
            require_feature(tx.state, 'special20')
            # The client selects owned fighters only, so a batch longer than the
            # card bag can hold is never a valid request.
            if not 1 <= len(items) <= len(tx.state['cards']):
                raise StorageError('Invalid one-key talent batch size')
            deltas, changed, seen, requested = {}, [], set(), []
            # Validate the whole batch before touching any fighter so a malformed
            # entry is a structural rejection, not a business tip mid-way.
            for item in items:
                if not isinstance(item, dict) or set(item) - {'cid', 'skill_id'}:
                    raise StorageError('Unexpected one-key talent fields')
                cid = integer(item.get('cid'))
                if cid in seen:
                    raise StorageError('Duplicate fighter in one-key talent request')
                seen.add(cid)
                requested.append((cid, integer(item.get('skill_id'))))
            for cid, original in requested:
                card, cfg = owned_card(tx.state, cid)
                steps = one_key_talent_steps(tx, card, cfg, original)
                for _, _, costs in steps:
                    for key, amount in costs.items():
                        deltas[key] = deltas.get(key, 0) + amount
                changed.append(card)
            result = resource_replies(tx.state, {key: -value for key, value in deltas.items()})
            # cfgCfgTaskFinishVal.lua:7885 counts "提升任意1名队员的特性" for
            # condition 22026, so one completed tally per upgraded fighter is
            # advanced instead of one per level. 本地策略，无官服样本.
            result.extend(task_updates(tx.state, 'talent_upgrade', len(changed)))
            # PlayerProto:CardUpdate carries the authoritative per-card skill
            # change; the client merges it in RoleMgr:CardUpdate
            # (PlayerProto.lua:262-263, RoleMgr.lua:530-546).
            result.extend(card_update_replies(
                ctx, [deepcopy(card) for card in changed],
                int(tx.state.get('store_exp', 0))))
            # 逐卡 MainTalentUpgradeRet 扇出已移除. GMsgNo.lua:1072 only names the
            # answer and no field table links the one-key request (opcode 3703) to
            # it, so the per-fighter rets were invented here. The client registers
            # exactly one handler for this request, PlayerProto.lua:1840-1843
            # (RoleMgr.OnBagUpdate2(1) + EventType.MainTalent_Upgrade), while
            # PlayerProto:MainTalentUpgradeRet drives the single-fighter event
            # RoleSkillMgr.lua:207-209 -> RoleMgr:UpdateCardEvent
            # (CardUpdateType.MainTalentUpgradeRet, ...). On 2026-10-05 12:19:59 the
            # device answered one request with 127 of those frames, i.e. 127 extra
            # single-card UI events, and the client UI broke.
            # GameMsg.lua:5358-5362 (no fields) and PlayerProto.lua:1840-1843:
            # this is the reply the client handler waits for; it refreshes the
            # bag and dispatches EventType.MainTalent_Upgrade (GMsgNo.lua:1073).
            result.append(Reply('PlayerProto:OneKeyMainTalentUpgradeRet', {}))
    except UpgradeDenied as error:
        return [upgrade_tip(error, 3703, 'PlayerProto:OneKeyMainTalentUpgrade')]
    return result


class ItemUseDenied(Exception):
    def __init__(self, key, *params):
        self.key, self.params = key, params
        super().__init__(key)


@lru_cache(maxsize=256)
def reward_record(reward_id):
    # RewardInfo is split across seven source files. Select one bounded data
    # record with the existing safe parser; never execute Lua expressions.
    for suffix in ('', '1', '2', '3', '4', '5', '6'):
        try:
            return selected_record('cfgRewardInfo' + suffix + '.lua', reward_id)[0]
        except ValueError:
            continue
    raise StorageError('Local selectable reward template unavailable')


def selection_award(tx, selection, count):
    cfgid = integer(selection.get('id'))
    quantity = integer(selection.get('count', 1)) * count
    integer(quantity)
    kind = integer(selection.get('type'))
    if kind == 2:
        from admin_resources import award_items, item_allowed, maximum
        if not item_allowed(cfgid):
            raise ItemUseDenied('GeneralTips', '离线模式暂不支持此箱子的奖励类型')
        # Preserve the entitlement when the target stack/currency is full.
        from admin_resources import balance, resource_key
        key, target = resource_key('item:' + str(cfgid))
        if balance(tx.state, key, target) + quantity > maximum(tx.state, key, target):
            raise ItemUseDenied('GeneralTips', '奖励数量将超过上限，请先消耗后再使用')
        return award_items(tx, [{'id': cfgid, 'num': quantity, 'type': 2}])
    if kind == 4:
        from handlers.progression import EQUIPS, add_equip
        if str(cfgid) not in EQUIPS:
            raise StorageError('Local selectable equipment template unavailable')
        if len(tx.state.get('equips', [])) + quantity > int(tx.state.get('max_equip_size', 500)):
            raise ItemUseDenied('equipBagSpaceLimit')
        equips = [add_equip(tx, cfgid) for _ in range(quantity)]
        return [{'id': cfgid, 'num': quantity, 'type': 4}], [Reply('EquipProto:EquipAdd', {
            'equips': equips, 'cur_size': len(tx.state['equips']),
            'max_size': int(tx.state.get('max_equip_size', 500)), 'is_finish': True})]
    raise ItemUseDenied('GeneralTips', '离线模式暂不支持此箱子的奖励类型')


def consume_item(tx, source):
    if not isinstance(source, dict):
        raise StorageError('Item use requires a record')
    if set(source) - {'id', 'cnt', 'ix', 'arg1'}:
        raise StorageError('Unexpected item-use fields')
    cfgid, count = integer(source.get('id')), integer(source.get('cnt'), 0, 50)
    ix = integer(source.get('ix', 0), 0, 32767)
    arg1 = integer(source.get('arg1', 0), 0, 4294967295)
    cfg = keyed_config('cfgItemInfo.lua', cfgid)
    if cfg.get('is_can_use') is not True:
        raise ItemUseDenied('canNotUse', cfg.get('name', str(cfgid)))
    if ix:
        raise ItemUseDenied('GeneralTips', '离线模式暂不支持按过期批次使用此道具')
    if tx.item_count(cfgid) < count:
        raise ItemUseDenied('itemNumNotEnough', cfg.get('name', str(cfgid)), count)
    value = {'id': cfgid, 'cnt': count, 'ix': ix, 'arg1': arg1}
    if cfg.get('type') == 17:
        reward = reward_record(integer(cfg.get('dy_value1')))
        if reward.get('type') != 4 or not isinstance(reward.get('item'), list):
            raise ItemUseDenied('GeneralTips', '离线模式暂不支持此箱子的选择规则')
        matches = [v for v in reward['item'] if v.get('index') == arg1]
        if len(matches) != 1:
            raise StorageError('Invalid selectable reward index')
        # GiftInfoView retains zero-count options in curUseList and sends them.
        # A positive option consumes one box per count, not reward.dropCnt boxes.
        if not count:
            return value, {}, 0, [], []
        tx.add_item(cfgid, -count)
        gets, replies = selection_award(tx, matches[0], count)
        value['gets'] = deepcopy(gets)
        return value, {cfgid: -count}, 17, replies, gets
    if cfg.get('type') != 10:
        raise ItemUseDenied('canNotUse', cfg.get('name', str(cfgid)))
    if arg1:
        raise StorageError('Unexpected non-selectable item argument')
    subtype = cfg.get('dy_value1')
    if not count:
        return value, {}, 0, [], []
    if subtype == 7:
        increase = integer(cfg.get('dy_value2')) * count
        maximum = row('cfgCfgPlrHot.lua', tx.state['player']['level'])['max']
        if tx.currency('hot') + increase > maximum:
            raise ItemUseDenied('plrHotUseMaxLimit')
        tx.add_item(cfgid, -count)
        tx.add_currency('hot', increase)
    elif subtype == 4:
        # Explicit local behavior for a usable config entry. Non-usable/automatic
        # experience rewards require the separate reward-grant pipeline.
        exp = indexed(cfg.get('dy_tb', []), 1)
        value = int(tx.state.get('store_exp', 0)) + integer(exp) * count
        if value > 4294967295:
            raise ItemUseDenied('GeneralTips', '经验池已达上限，请先消耗后再使用')
        tx.add_item(cfgid, -count)
        tx.state['store_exp'] = integer(value, 0, 4294967295)
    else:
        raise ItemUseDenied('GeneralTips', '离线模式尚未实现此道具的使用效果：' + cfg.get('name', str(cfgid)))
    return value, {cfgid: -count}, subtype, [], []


def consume(ctx, infos, batch):
    uid = ctx.require_login()
    if not isinstance(infos, list) or not 1 <= len(infos) <= 64:
        raise StorageError('Invalid item-use batch size')
    counts = []
    for source in infos:
        if not isinstance(source, dict) or set(source) - {'id', 'cnt', 'ix', 'arg1'}:
            raise StorageError('Unexpected item-use fields')
        integer(source.get('id'))
        integer(source.get('ix', 0), 0, 32767)
        integer(source.get('arg1', 0), 0, 4294967295)
        counts.append(integer(source.get('cnt'), 0, 50))
    if not 1 <= sum(counts) <= 50:
        raise StorageError('Item-use count must be between one and source g_MaxUseItem fifty')
    with ctx.store.transaction(uid) as tx:
        requested = {}
        for source, count in zip(infos, counts):
            cfgid = integer(source.get('id'))
            requested[cfgid] = requested.get(cfgid, 0) + count
        for cfgid, count in requested.items():
            if tx.item_count(cfgid) < count:
                cfg = keyed_config('cfgItemInfo.lua', cfgid)
                raise ItemUseDenied('itemNumNotEnough', cfg.get('name', str(cfgid)), count)
        results, deltas, effects, awards, gets = [], {}, set(), [], []
        for source in infos:
            value, change, effect, pushes, granted = consume_item(tx, source)
            results.append(value)
            effects.add(effect)
            awards.extend(pushes)
            gets.extend(granted)
            for key, amount in change.items():
                deltas[key] = deltas.get(key, 0) + amount
        replies = resource_replies(tx.state, deltas) + awards
        if 7 in effects:
            replies.append(Reply('LoginProto:PlrUpdate', {'infos': deepcopy(tx.state['player'])}))
        if 4 in effects:
            replies.append(Reply('PlayerProto:CardUpdate', {'cards': [], 'store_exp': tx.state['store_exp']}))
        if batch:
            merged = {}
            for value in gets:
                key = value['id'], value['type']
                merged.setdefault(key, {'id': value['id'], 'type': value['type'], 'num': 0})['num'] += value['num']
            replies.append(Reply('PlayerProto:UseItemListRet', {'infos': results, 'gets': list(merged.values()), 'isMerge': False}))
        else:
            replies.append(Reply('PlayerProto:UseItemRet', {'info': results[0], 'isMerge': False}))
    return replies


def item_use_log(ctx, name, infos, error):
    event = getattr(ctx.server, 'event', None)
    if not callable(event):
        return
    safe = [{key: value for key in ('id', 'cnt', 'ix', 'arg1')
             if isinstance(value := row.get(key), int) and not isinstance(value, bool)
             and -2147483648 <= value <= 4294967295}
            for row in infos[:64] if isinstance(row, dict)] if isinstance(infos, list) else []
    event('item_use_denied' if isinstance(error, ItemUseDenied) else 'item_use_rejected',
          name=name, infos=safe, reason=str(error)[:240])


def use_request(ctx, infos, batch):
    name = 'PlayerProto:UseItemList' if batch else 'PlayerProto:UseItem'
    try:
        return consume(ctx, infos, batch)
    except ItemUseDenied as error:
        item_use_log(ctx, name, infos, error)
        return [Reply('SystemProto:Tips', {'strId': error.key, 'opId': 3684 if batch else 2516,
                     'opName': name, 'args': [{'type': 0, 'param': str(value)} for value in error.params]})]
    except StorageError as error:
        item_use_log(ctx, name, infos, error)
        raise


@register('PlayerProto:UseItem')
async def use_item(ctx, fields):
    return use_request(ctx, [fields.get('info')], False)


@register('PlayerProto:UseItemList')
async def use_items(ctx, fields):
    return use_request(ctx, fields.get('infos'), True)


# Required local small catalogs are validated before listeners open on import.
for _catalog in ('cfgCardLevel.lua', 'cfgCfgCardBreakLimitLv.lua', 'cfgCardBreak.lua',
                 'cfgCardSkillExp.lua', 'cfgCfgMainTalentSkillUpgrade.lua',
                 'cfgCfgCardCoreLv.lua', 'cfgCardBreakMaterial2.lua', 'cfgCfgPlrHot.lua'):
    array_config(_catalog)
