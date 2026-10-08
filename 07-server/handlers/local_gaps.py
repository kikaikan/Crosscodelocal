"""Client requests seen in the disconnect log that the local server can answer honestly.

Two outcomes live here:

* real empty reads - the local save genuinely holds no data for the query, so the
  original reply shape is answered with its documented zero/empty state (the same
  shape handlers/initialization.py already answers for the sibling read);
* explicit refusals - operations whose original contract cannot be honoured
  (reward claims, share counters) answer through the shared SystemProto:Tips
  policy instead of a fabricated success.

Every entry keeps the connection: nothing here closes a session, and nothing here
writes state it cannot justify.
"""
from database import StorageError
from server_core import Reply, register

# The zeroed sPracticeInfo that handlers/initialization.py:224-226 already answers for
# ArmyProto:GetPracticeInfo. ExerciseMgr:GetPracticeInfoRet (ExerciseMgr.lua:410) is the
# consumer of both replies, so this stays an existing behaviour, not a new claim.
PRACTICE_ZERO = {'start_time': 0, 'end_time': 0, 'can_join_cnt': 0, 't_join_cnt': 0,
                 'flush_cnt': 0, 'rank_level': 0, 'max_rank_level': 0, 'rank': 0,
                 'max_rank': 0, 'score': 0, 'can_join_buy_cnt': 0}


@register('ArmyProto:GetSelfPracticeInfo')
async def self_practice_info(ctx, fields):
    """ArmyProto.lua:138 - answered from the same local practice snapshot as GetPracticeInfo."""
    ctx.require_login()
    return [Reply('ArmyProto:GetSelfPracticeInfoRet', {'info': dict(PRACTICE_ZERO), 'army_ix': 0})]


@register('PlayerProto:GetRank')
async def rank(ctx, fields):
    """PlayerProto.lua:1404 - RankMgr.lua:84 tolerates a board with no rows.

    The local save has no leaderboard, so the honest answer is an empty board plus
    the caller's own rank type. reward_issue is a json field whose codec type needs a
    Lua table; it is omitted rather than faked as a string.
    """
    ctx.require_login()
    rank_type = fields.get('rank_type')
    if isinstance(rank_type, bool) or not isinstance(rank_type, int):
        raise StorageError('Leaderboard type must be an integer')
    return [Reply('PlayerProto:GetRankRet', {'rank_type': rank_type, 'data': [], 'rank': 0,
                  'score': '0', 'next_refresh_time': 0, 'turn_num': 0})]


@register('ExplorationProto:GetReward')
async def exploration_reward(ctx, fields):
    """ExplorationProto.lua:17 claims a reward; the local save has no exploration rewards."""
    ctx.require_login()
    raise StorageError('本地服务没有勘探奖励数据，本次没有发放任何奖励。')


# PlayerProto:RandomPanelClean and the rest of the random-board family are
# owned by handlers.panels, which persists state['panels'] for real.


@register('ShareProto:AddShareCount')
async def add_share_count(ctx, fields):
    """ShareProto.lua:2 promises RewardNotice/ItemUpdate rewards from the share flow.

    Without a local share economy the honest answer is a refusal, not a silent counter
    that reports a success no client-visible state can observe.
    """
    ctx.require_login()
    raise StorageError('本地服务未记录分享次数，未发放分享奖励。')
