"""道具池 / 扭蛋：RegressionProto:ItemPoolInfo + ItemPoolDraw（全池开放，本地重建）。

线格式（只读权威）
  05-protocol/endpoints.json
    RegressionProto:ItemPoolInfo     opcode 4206  c2s  有官服样本
    RegressionProto:ItemPoolInfoRet  opcode 4207  s2c  官服样本 {"info": []}
    RegressionProto:ItemPoolDraw     opcode 4208  c2s  static_source_only
    RegressionProto:ItemPoolDrawRet  opcode 4209  s2c  static_source_only
  03-unpack/lua/device-luascripts/GameMsg.lua:5448-5472
    sItemPool       = id, round, startTime, drawArr(json), drawTimes
    ItemPoolInfoRet = info(list|sItemPool)
    ItemPoolDraw    = id, times
    ItemPoolDrawRet = info(struts|sItemPool), drawArr(array|uint), drawRound
  drawArr 语义由客户端消费代码确定：
    * sItemPool.drawArr = 奖励index 到 已抽取次数 的映射（ItemPoolInfo.lua:41-52,289-311；
      GCalculatorHelp.lua:3060-3090 用 rewardnum - drawArr[v.index] 算剩余）。
    * DrawRet.drawArr = 本次抽中的奖励 index 列表（LuckyGachaMain.lua:166-198、
      GachaMain.lua:396-433 用 ipairs 遍历后与 v:GetIndex() 比对）。
  客户端入口：ItemPoolActivityMgr:Init 发 ItemPoolInfo(id=nil,nextRound=nil)
  （ItemPoolActivityMgr.lua:4-8）；LuckyGachaMain.lua:35-39 在取不到池时 LogError 并
  直接 return，所以 info 为空等于扭蛋界面打不开。

配置（只读权威，离线生成，不执行 Lua）
  cfgCfgItemPool.lua / cfgCfgItemPoolReward.lua / cfgCfgItemPoolConsume.lua
  -> 07-server/data/item-pool-pools.json（02-tools/scripts/item-pool-build.py）

本地策略，无官服样本（本文件所有 draw/轮次/开放判定都属于本地重建）
  * 官服 ItemPoolInfoRet 样本
    05-protocol/samples/tcp/session1-stream02-s2c-frame0126.decoded.json 只有
    {"info": []}（抓包时无开启中的池），Draw/DrawRet 没有任何样本。运行期修复前证据：
    07-server/logs/server.jsonl 里 opcode 4206 的 response 恒为 8 字节。
  * 返回哪些池：本地把配置里的全部 10 个池都返回（用户要求 扭蛋启用全部）；官服只返回
    当时开启的池。客户端自己的 ItemPoolInfo:IsOpen()（ItemPoolInfo.lua:103-119）仍按
    cfg.starttime/endtime 判定，服务端无法改配置时间窗，也不在本模块改动。
  * 抽奖随机：按奖励 weight 加权；随机源是存档内 sha256 计数器流（与 handlers.gacha 相同的
    本地 RNG 构造），可复现，但没有官服算法证据。
  * 轮次推进：只有客户端显式发 nextRound=true 才前进一轮，并要求本轮关键奖励已抽完
    （映射自 ItemPoolInfo.lua:122-149 CanNext 的关键奖励判定）。
  * costtype=2/3 消耗条目越界时取最后一条（ItemPoolInfo.lua:216-219、228-232 在越界时
    LogError 并放弃），这是本地保底策略，不是官服行为。

错误策略沿用 server_core/error_policy：业务拒绝抛 StorageError 得到 SystemProto:Tips，
会话保持；只有鉴权失败/协议损坏/超时才断连。帧编码上限 32767
（02-tools/scripts/protocol_codec.py:42），本模块回包实测只有几百字节。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import time

from config_codec import app_path
from database import StorageError
from server_core import Reply, register

DATA = app_path('data', 'item-pool-pools.json')
CATALOG = json.loads(DATA.read_text(encoding='utf-8'))
POOLS = CATALOG['pools']
GROUPS = CATALOG['reward_groups']
CONSUME = CATALOG['consume']

INFO_REQUEST = 'RegressionProto:ItemPoolInfo'
INFO_REPLY = 'RegressionProto:ItemPoolInfoRet'
DRAW_REQUEST = 'RegressionProto:ItemPoolDraw'
DRAW_REPLY = 'RegressionProto:ItemPoolDrawRet'

# ItemPoolExtractType (GEnum.lua:2064-2071).
ROUND_LOOP, ROUND_LIMIT, ONCE, DROP_LOOP, CONTROL, CONTROL_NOT_INFINITE = 1, 2, 3, 4, 5, 6
# ItemPoolInfo.lua:151-159 把 RoundLoop/DropLoop 视为无限轮、Once 视为一轮。
UNLIMITED_ROUNDS = (ROUND_LOOP, DROP_LOOP)


def now(state: dict) -> int:
    """Local offline clock; same convention as handlers.initialization/gacha."""
    return int(state.get('offline_clock', time.time()))


def integer(fields: dict, key: str, default=None):
    value = fields.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError('道具池字段必须是整数: ' + key)
    return value


def pools_state(state: dict) -> dict:
    """state['item_pools'] holds each pool's draw bookkeeping and the local RNG state."""
    data = state.get('item_pools')
    if not isinstance(data, dict):
        data = {}
        state['item_pools'] = data
    data.setdefault('version', 1)
    data.setdefault('rng_seed', secrets.token_hex(32))
    data.setdefault('rng_counter', 0)
    if not isinstance(data.get('pools'), dict):
        data['pools'] = {}
    return data


def pool_entry(data: dict, pool_id: int) -> dict:
    entry = data['pools'].get(str(int(pool_id)))
    if not isinstance(entry, dict):
        entry = {'round': 1, 'drawTimes': 0, 'drawArr': {}}
        data['pools'][str(int(pool_id))] = entry
    # JSON round trips turn drawArr keys into strings; normalize on every access.
    entry['round'] = max(1, int(entry.get('round', 1)))
    entry['drawTimes'] = max(0, int(entry.get('drawTimes', 0)))
    entry['drawArr'] = {str(int(key)): int(value)
                        for key, value in (entry.get('drawArr') or {}).items()}
    return entry


def drawn(entry: dict) -> dict:
    return {int(key): int(value) for key, value in entry['drawArr'].items()}


def reward_rounds(row: dict) -> set:
    return {int(value) for value in row['rounds']}


def configured_rounds(group: dict) -> list:
    return sorted({int(round_no) for row in group['pool'] for round_no in row['rounds']})


def max_rounds(pool: dict, group: dict):
    """Mirror ItemPoolInfo.lua:151-159; None means an unlimited RoundLoop/DropLoop pool."""
    kind = int(pool['extracttype'])
    if kind in UNLIMITED_ROUNDS:
        return None
    if kind == ONCE:
        return 1
    rounds = configured_rounds(group)
    return max(rounds) if rounds else 1


def effective_round(group: dict, round_no: int) -> int:
    """ItemPoolInfo:GetInfos clamps a round above the configured maximum (lua:285)."""
    rounds = configured_rounds(group)
    return min(max(1, int(round_no)), max(rounds) if rounds else 1)


def candidates(group: dict, taken: dict, round_no: int) -> list:
    """Reward rows still drawable in this round; isInfinite rows never exhaust."""
    result = []
    for row in group['pool']:
        if int(round_no) not in reward_rounds(row):
            continue
        if not row.get('isInfinite'):
            remaining = int(row.get('rewardnum', 1)) - int(taken.get(int(row['index']), 0))
            if remaining <= 0:
                continue
        if int(row.get('weight', 0)) > 0:
            result.append(row)
    return result


def random_below(data: dict, maximum: int) -> int:
    """Local reproducible RNG stream; identical construction to handlers.gacha:85-95."""
    if maximum < 1 or maximum > 1 << 256:
        raise StorageError('道具池随机范围无效')
    ceiling = (1 << 256) - ((1 << 256) % maximum)
    while True:
        count = int(data['rng_counter'])
        block = hashlib.sha256(bytes.fromhex(data['rng_seed']) + count.to_bytes(16, 'big')).digest()
        data['rng_counter'] = count + 1
        value = int.from_bytes(block, 'big')
        if value < ceiling:
            return value % maximum


def weighted_pick(data: dict, rows: list) -> dict:
    weights = [int(row.get('weight', 0)) for row in rows]
    total = sum(weights)
    if total <= 0:
        raise StorageError('道具池权重配置无效')
    value = random_below(data, total)
    for row, weight in zip(rows, weights):
        if value < weight:
            return row
        value -= weight
    raise AssertionError('道具池加权抽取越界')


def cost_plan(pool: dict, draw_count: int, tx) -> list:
    """Consumables for one draw, mirroring ItemPoolInfo.lua:162-249 GetCostGoods.

    costtype 1: cost = {{item, num}}           -> first pair (lua:206-209)
    costtype 2: cost = {{ix, item, num}, ...}  -> the (drawCount+1)-th pair (lua:210-223)
    costtype 3: specialCost -> CfgItemPoolConsume.infos[drawCount+1].costNum,
                primary item first then the optional second item (lua:224-247)
    """
    kind = int(pool.get('costtype', 1))
    cost = pool.get('cost') or []
    if not cost:
        raise StorageError('道具池缺少消耗配置')
    if kind == 1:
        return [(int(cost[0][0]), int(cost[0][1]))]
    if kind == 2:
        row = cost[min(int(draw_count), len(cost) - 1)]
        return [(int(row[1]), int(row[2]))]
    if kind == 3:
        table = CONSUME.get(str(int(pool.get('specialCost', 0)))) or {}
        infos = table.get('infos') or []
        if not infos:
            raise StorageError('道具池消耗表不完整')
        step = infos[min(int(draw_count), len(infos) - 1)]
        need = int(step.get('costNum', 0))
        if need <= 0:
            return []
        primary = int(cost[0][0])
        secondary = int(cost[1][0]) if len(cost) > 1 else None
        have = tx.item_count(primary)
        if have < need:
            plan = [(primary, have)] if have > 0 else []
            remaining = need - have
            plan.append((secondary, remaining) if secondary is not None else (primary, remaining))
            return plan
        return [(primary, need)]
    raise StorageError('未复现的道具池消耗类型')


def deduct(tx, plan: list) -> dict:
    totals = {}
    for item, amount in plan:
        totals[item] = totals.get(item, 0) + amount
    for item, amount in totals.items():
        if tx.item_count(item) < amount:
            raise StorageError('道具池消耗道具不足')
    for item, amount in totals.items():
        tx.add_item(item, -amount)
    return {item: -amount for item, amount in totals.items()}


def draw_once(data: dict, group: dict, entry: dict) -> tuple:
    """Pick one reward row for the entry's current effective round and record it."""
    round_no = effective_round(group, int(entry['round']))
    rows = candidates(group, drawn(entry), round_no)
    if not rows:
        raise StorageError('本轮道具池奖励已全部抽完')
    chosen = weighted_pick(data, rows)
    index = int(chosen['index'])
    entry['drawArr'][str(index)] = int(drawn(entry).get(index, 0)) + 1
    return chosen, round_no


def grant(tx, row: dict) -> tuple:
    reward = row.get('reward') or []
    if len(reward) < 2:
        raise StorageError('道具池奖励配置无效')
    item, amount = int(reward[0]), int(reward[1])
    tx.add_item(item, amount)
    return item, amount


def pool_info(pool: dict, entry: dict, stamp: int) -> dict:
    """One sItemPool row (GameMsg.lua:5448-5452)."""
    return {'id': int(pool['id']),
            'round': int(entry['round']),
            'startTime': int(entry.get('startTime') or stamp),
            'drawArr': drawn(entry),
            'drawTimes': int(entry['drawTimes'])}


def round_cleared(group: dict, taken: dict, round_no: int) -> bool:
    """ItemPoolInfo.lua:126-141: a round may advance once its key rewards are taken."""
    for row in group['pool']:
        if int(round_no) not in reward_rounds(row) or row.get('isInfinite') or not row.get('iskeyreward'):
            continue
        if int(row.get('rewardnum', 1)) - int(taken.get(int(row['index']), 0)) > 0:
            return False
    return True


def local_entry(raw) -> dict:
    """Normalize a stored pool entry without creating it (pure read path)."""
    entry = raw if isinstance(raw, dict) else {}
    return {'round': max(1, int(entry.get('round', 1))),
            'drawTimes': max(0, int(entry.get('drawTimes', 0))),
            'drawArr': {str(int(key)): int(value)
                        for key, value in (entry.get('drawArr') or {}).items()},
            'startTime': int(entry.get('startTime', 0) or 0)}


def info_rows(state: dict, requested) -> list:
    """Every configured pool, or only the requested one; all of them are 'open' locally."""
    identifiers = [key for key in sorted(POOLS, key=int)
                   if requested in (None, 0) or int(key) == int(requested)]
    if requested not in (None, 0) and not identifiers:
        raise StorageError('未知的道具池')
    stored = state.get('item_pools')
    pools = (stored or {}).get('pools') if isinstance(stored, dict) else {}
    pools = pools if isinstance(pools, dict) else {}
    stamp = now(state)
    return [pool_info(POOLS[key], local_entry(pools.get(key)), stamp) for key in identifiers]


@register(INFO_REQUEST)
async def item_pool_info(ctx, fields):
    """Return pool state; id selects one pool, nextRound advances it by one round.

    本地策略，无官服样本：官服只返回当时开启的池，这里把配置里的池全部返回
    （用户要求扭蛋启用全部）。nextRound 的准入条件是本地规则。
    """
    uid = ctx.require_login()
    raw = fields.get('id')
    requested = None if raw is None else integer(fields, 'id')
    if requested not in (None, 0) and str(int(requested)) not in POOLS:
        raise StorageError('未知的道具池')
    if not fields.get('nextRound'):
        # Pure read: no transaction, so a login-time info request cannot bump the revision.
        return [Reply(INFO_REPLY, {'info': info_rows(ctx.store.get_player(uid), requested)})]
    if requested in (None, 0):
        # The client always sends the pool id with nextRound=true (ItemPoolActivity.lua:122,127).
        raise StorageError('进入下一轮必须指定道具池')
    with ctx.store.transaction(uid) as tx:
        data = pools_state(tx.state)
        pool = POOLS[str(int(requested))]
        group = GROUPS[str(int(pool['group']))]
        entry = pool_entry(data, int(requested))
        limit = max_rounds(pool, group)
        current = int(entry['round'])
        if not round_cleared(group, drawn(entry), effective_round(group, current)):
            raise StorageError('本轮关键奖励尚未抽完，不能进入下一轮')
        if limit is not None and current >= limit:
            raise StorageError('道具池已经是最后一轮')
        entry['round'] = current + 1
        entry.setdefault('startTime', now(tx.state))
        rows = [pool_info(pool, entry, now(tx.state))]
    return [Reply(INFO_REPLY, {'info': rows})]


@register(DRAW_REQUEST)
async def item_pool_draw(ctx, fields):
    """Deduct the configured cost, draw times rewards, persist them and answer DrawRet.

    本地策略，无官服样本：扣费顺序、加权随机和轮次判定都是本地规则。任一步失败都抛
    StorageError，整个事务回滚（消耗、奖励、drawArr、drawTimes 一起还原），
    server_core 把它转成 SystemProto:Tips 且保持会话。
    """
    uid = ctx.require_login()
    pool_id = integer(fields, 'id')
    times = integer(fields, 'times', 1)
    pool = POOLS.get(str(pool_id))
    if pool is None:
        raise StorageError('未知的道具池')
    group = GROUPS[str(int(pool['group']))]
    limit = int(pool.get('maxcostnum', 1))
    if not 1 <= times <= limit:
        raise StorageError('抽取次数超出道具池单次上限')
    with ctx.store.transaction(uid) as tx:
        data = pools_state(tx.state)
        entry = pool_entry(data, pool_id)
        stamp = now(tx.state)
        entry.setdefault('startTime', stamp)
        deltas, indexes = {}, []
        draw_round = effective_round(group, int(entry['round']))
        for _ in range(times):
            plan = cost_plan(pool, int(entry['drawTimes']), tx)
            for item, amount in deduct(tx, plan).items():
                deltas[item] = deltas.get(item, 0) + amount
            row, draw_round = draw_once(data, group, entry)
            item, amount = grant(tx, row)
            deltas[item] = deltas.get(item, 0) + amount
            entry['drawTimes'] = int(entry['drawTimes']) + 1
            indexes.append(int(row['index']))
        result = {'info': pool_info(pool, entry, stamp), 'drawArr': indexes,
                  'drawRound': draw_round}
        replies = []
        if deltas:
            replies.append(Reply('PlayerProto:ItemUpdate', {'data': [
                {'id': item, 'add': amount, 'num': tx.item_count(item), 'time': 0, 'ix': 0,
                 'expiry': 0, 'get_infos': {}} for item, amount in sorted(deltas.items())]}))
        replies.append(Reply(DRAW_REPLY, result))
    return replies

# ---------------------------------------------------------------------------
# 活动列表时间下发（配合 task-21 的客户端 CfgActiveList 补丁）
# 线格式：05-protocol/endpoints.json 的 OperateActiveProto:GetActiveTimeList(4704) /
# GetActiveTimeListRet(4705)；GameMsg.lua:5778-5792 的 sOperateActive =
# id(uint), openTime(int), closeTime(int), payRate(int), noticeId(int), state(int)，
# 全部 optional。官服样本 05-protocol/samples/tcp/session1-stream02-s2c-frame0146.decoded.json
# 里每条只带 id + openTime + closeTime，本次按同样形状下发。
# 客户端 ActivityMgr:UpdateDatas（ActivityMgr.lua:253-267）把 openTime/closeTime 覆盖到
# Cfgs.CfgActiveList 对应条目上再刷新开启态；ActivityData:IsOpenTime（ActivityData.lua:127-134）
# 的判定是 now > sTime and now <= eTime，所以 0/0 会被判成关闭，必须给真实窗口。
# ---------------------------------------------------------------------------
ACTIVE_REQUEST = 'OperateActiveProto:GetActiveTimeList'
ACTIVE_REPLY = 'OperateActiveProto:GetActiveTimeListRet'

# 本地策略，无官服样本：6 期扭蛋活动 id 与 task-21 的客户端 CfgActiveList 补丁一一对应
# （1017=期1→池1003（cfgCfgActiveList.lua 现有条目，type=1017/info.cfgId=1003）、
# 10172→1005、10173→1006、10174→1008、10175→1010、10176→1004）。
# 这些 id 是客户端补丁分配的，无法从只读的 03-unpack 配置推导，故在此显式列出。
POOL_ACTIVITIES = (1017, 10172, 10173, 10174, 10175, 10176)
# 窗口起点回拨一天，保证 now > openTime 严格成立（IsOpenTime 用严格大于）。
ACTIVITY_LEAD_SECONDS = 86400
# closeTime 在 sOperateActive 里是 int32（GameMsg.lua:5780 的 "int"），
# 02-tools/scripts/protocol_codec.py 用 struct 'i' 编码：4102444800 会抛
# CodecError("Value 4102444800 does not fit i")，在 dispatcher 里等于
# close_reason='frame_encode_failed' 直接断连。故取 int32 上限 2147483647
#（2038-01-19 03:14:07 UTC），语义上仍是「远未来、实际不会关闭」的本地哨兵。
ACTIVITY_CLOSE_TIME = 2147483647


@register(ACTIVE_REQUEST)
async def active_time_list(ctx, fields):
    """把 6 期扭蛋活动的真实时间窗下发成开启（本地策略，无官服样本）。

    官服只下发当时真正在开的档期；离线本地部署要让 6 期扭蛋都能从活动列表进入，
    因此统一给 [now-1d, 2147483647] 的窗口。除时间窗外的字段（payRate/noticeId/state）
    保持省略，与官服样本一致，也避免伪造充值/开启态语义。
    """
    state = ctx.store.get_player(ctx.require_login())
    open_time = now(state) - ACTIVITY_LEAD_SECONDS
    rows = [{'id': int(identifier), 'openTime': open_time, 'closeTime': ACTIVITY_CLOSE_TIME}
            for identifier in POOL_ACTIVITIES]
    return [Reply(ACTIVE_REPLY, {'operateActiveList': rows, 'isFinish': True})]
