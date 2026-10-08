"""Read-only first-login state for the decoded CrossCore client.

Entry: LoginProto.lua:261 OnEquipInitFinish -> Shop/Plot/MgrCenter/Guild
-> ClientProto.InitFinish (:156). Post-finish reads are in ClientProto.lua:165.
The explicit specifications below are empty *read states*, never reward claims
or successful writes. Closed domains remain closed until their config gates
and local state allow them. No official snapshot, credential, or network call
is used. Additional domains can populate state['initialization'][request].
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import re
import time

from server_core import register, Reply
from database import StorageError
import reply_chunks
from seed_generator import LUA_DIR, balanced_table, python_data
from config_codec import parse_lua_table


@lru_cache(maxsize=8)
def config_table(filename: str) -> dict:
    """Parse a bounded, fixed-path Lua data table; never evaluate a module."""
    if filename not in {"cfgCfgOpenCondition.lua", "cfgCfgOpenConditionMore.lua", "cfgCfgOpenRules.lua", "cfgCfgSignReward.lua", "cfgcfgColosseum.lua", "cfgcfgMonopoly.lua", "cfgcfgMonopolyGrid.lua"}:
        raise ValueError("Unsupported initialization catalog")
    source = (LUA_DIR / filename).read_text("utf-8-sig")
    assignment = re.search(r"_G\[[^\]]+\]\s*=\s*\{", source)
    if assignment is None:
        raise ValueError("Missing static configuration table")
    data = python_data(parse_lua_table(balanced_table(source, assignment.end() - 1)))
    if isinstance(data, list):
        data = {row["id"]: row for row in data}
    if not isinstance(data, dict):
        raise ValueError("Expected keyed configuration catalog")
    return data


def cleared_stages(state: dict) -> set[int]:
    progress = state.get("progress", {})
    stages = {int(value) for value in progress.get("cleared_stages", [])}
    for row in progress.get("mainLine", []):
        if not isinstance(row, dict):
            continue
        identifier = row.get("id", row.get("dupId", row.get("dupID")))
        stars = row.get("star", row.get("stars", 0))
        if identifier is not None and (stars or row.get("is_pass", False)):
            stages.add(int(identifier))
    return stages


def feature_open(state: dict, view: str) -> bool:
    """Mirror MenuMgr.lua:466 config level/stage/guide AND conditions.

    Unknown gates fail closed. Legacy unlock flags cannot bypass feature rules.
    """
    cfg = config_table("cfgCfgOpenConditionMore.lua" if view.startswith('special') else "cfgCfgOpenCondition.lua").get(view)
    if cfg is None:
        return False
    return conditions_open(state, cfg.get("conditions", []))


def conditions_open(state, conditions):
    rules = config_table("cfgCfgOpenRules.lua")
    passed = cleared_stages(state)
    guides = {int(value) for value in state.get("progress", {}).get("completed_guides", [])}
    for rule_id in conditions:
        rule = rules.get(rule_id)
        if rule is None:
            return False
        if rule.get('openTime'):
            # Source weekday arrays are Monday..Sunday, server uses UTC+8.
            weekday = datetime.fromtimestamp(local_time(state), timezone(timedelta(hours=8))).weekday()
            if rule['openTime'][weekday] != 1:
                return False
        kind, value = rule.get("type"), rule.get("val")
        if kind == 1:
            if state.get("player", {}).get("level", 1) < value:
                return False
        elif kind == 2:
            if value not in passed:
                return False
        elif kind == 3:
            if value not in guides:
                return False
        else:
            return False
    return True


def player_state(ctx) -> dict:
    ctx.require_login()
    return ctx.store.get_player(ctx.uid)


def local_time(state: dict) -> int:
    return int(state.get("offline_clock", time.time()))


@dataclass(frozen=True)
class ReadSpec:
    response: str
    defaults: dict
    gate: str | None = None
    note: str = "Empty collection for a fresh local account"


# Each response was checked against GameMsg.lua and its callback. is_finish /
# isFinish markers terminate list reads, not an action success acknowledgement.
# Domain state uses the exact response fields under initialization[request].
READS = {
    "ShopProto:GetSkinRebateRecord": ReadSpec("ShopProto:GetSkinRebateRecordRet", {"skinRebateRecordList": []}, "SkinRebate"),
    "PlayerProto:SectionMultiInfo": ReadSpec("PlayerProto:SectionMultiInfoRet", {"infos": [], "cntInfos": []}),
    "PlayerProto:CardSkillUpgradelist": ReadSpec("PlayerProto:CardSkillUpgradelistRet", {"infos": {}, "is_finish": True}),
    "PlayerProto:GetSpecialDropsInfo": ReadSpec("PlayerProto:GetSpecialDropsInfoRet", {"dropInfos": []}),
    "PlayerProto:DuplicateModUpData": ReadSpec("PlayerProto:DuplicateModUpDataRet", {"modUpData": []}),
    "AchievementProto:GetFinishInfo": ReadSpec("AchievementProto:GetFinishInfoRet", {"finish_list": [], "is_finish": True}, "Achievement"),
    "AchievementProto:GetRewardInfo": ReadSpec("AchievementProto:GetRewardInfoRet", {"infos": [], "is_finish": True}, "Achievement"),
    "BadgedProto:GetBadgedInfo": ReadSpec("BadgedProto:GetBadgedInfoRet", {"infos": [], "is_finish": True}, "BadgeView"),
    "BadgedProto:GetSortBadgedInfo": ReadSpec("BadgedProto:GetSortBadgedInfoRet", {"pos": []}, "BadgeView"),
    "FriendProto:GetFriendsData": ReadSpec("FriendProto:FriendAdd", {"friends": [], "had_del_cnt": 0, "had_apply_cnt": 0}, "FriendView"),
    "GuildProto:GuildInfo": ReadSpec("GuildProto:GuildInfoRet", {"title": 0}, "GuildMenu", "No guild membership: omit info, rather than constructing a guild"),
    # BuildingProto:BuildsBaseInfo / BuildsList and DormProto:GetOpenDorm are owned by
    # handlers.building / handlers.dorm: an empty local base never releases the client's
    # matrix_scene_enter loading weight (MatrixView.lua:25,57-69,78).
    "BuildingProto:AssualtInfo": ReadSpec("BuildingProto:AssualtInfoRet", {"info": {"running": False, "wIds": {}, "fIds": {}, "index": 0, "rewards": []}}, "Matrix"),
    "PlayerProto:GetNewTowerResetCnt": ReadSpec("PlayerProto:GetNewTowerResetCntRet", {"reset_cnt": []}, "TowerView"),
    "PlayerProto:GetNewTowerCardInfo": ReadSpec("PlayerProto:GetNewTowerCardInfoRet", {"card_infos": [], "is_finish": True}, "TowerView"),
    "PlayerProto:GetEternalBattleCardInfo": ReadSpec("PlayerProto:GetEternalBattleInfoRet", {"card_infos": [], "is_finish": True}),
    "PlayerProto:GetStarPalaceInfo": ReadSpec("PlayerProto:GetStarPalaceInfoRet", {"infos": [], "isReset": False}, note="No active star-palace fight: omit dupId/stopTime"),
    "FightProtocol:GetRogueInfo": ReadSpec("FightProto:GetRogueInfoRet", {"datas": [], "is_finish": True, "maxGroup": 0, "isFighting": False}, "RogueView"),
    "FightProtocol:GetRogueSInfo": ReadSpec("FightProto:GetRogueSInfoRet", {"datas": [], "is_finish": True, "maxGroup": 0, "isFighting": False, "gained": []}, "RogueSView"),
    "FightProtocol:GetRogueTInfo": ReadSpec("FightProto:GetRogueTInfoRet", {"data": [], "is_finish": True, "score": 0, "stageIdx": 0, "monthScore": 0, "periodIdx": 0, "maxGroup": 0, "maxBoss": 0, "win1": 0, "win2": 0}, "RogueTView"),
    "FightProtocol:GetRogueMapInfo": ReadSpec("FightProto:GetRogueMapInfoRet", {"groupData": [], "rollCnt": 0, "nDuplicateID": 0, "nDupExp": 0, "nSupportTimes": 0, "isSimulated": False, "hasRewardBubble": False, "hasSupportBubble": False, "nRequireTimes": 0}),
    "FightProtocol:GetTowerDeepInfo": ReadSpec("FightProto:GetTowerDeepInfoRet", {"datas": [], "maxGroup": 0}, "TowerDeep"),
    "FightProtocol:GetChainFrontInfo": ReadSpec("FightProto:GetChainFrontInfoRet", {"maxScore": 0, "dupScore": []}),
    "FightProtocol:GetPeriodicBossInfo": ReadSpec("FightProto:GetPeriodicBossInfoRet", {"info": []}),
    "RegressionProto:GetInfo": ReadSpec("RegressionProto:GetInfoRet", {"resourcesIsGain": 0}, note="Fresh account has no returning-player eligibility"),
    "RegressionProto:ResupplyInfo": ReadSpec("RegressionProto:ResupplyInfoRet", {"info": []}),
    "RegressionProto:ActiveRewardsInfo": ReadSpec("RegressionProto:ActiveRewardsInfoRet", {"idx": 0, "loginDay": 0, "gainArr": []}),
    "RegressionProto:PlrBindInfo": ReadSpec("RegressionProto:PlrBindInfoRet", {}, note="CollaborationMgr.lua:41 treats an empty table as no active binding event"),
    "RegressionProto:PlrBindInviteList": ReadSpec("RegressionProto:PlrBindInviteListRet", {"friends": [], "isEnd": True}),
    "DormProto:DormPetInfo": ReadSpec("DormProto:DormPetInfoRet", {"info": []}, "Dorm"),
    "DormProto:BuyRecord": ReadSpec("DormProto:BuyRecordRet", {"infos": {}}, "Dorm"),
    "SummerProto:PetInfo": ReadSpec("SummerProto:PetInfoRet", {"info": [], "cur_pet": 0, "locked": [], "gained": [], "tNextRandom": 0, "haveReward": False}, "PetMain", "No enabled summer pet event"),
    "OperateActiveProto:GetSkinRebateInfo": ReadSpec("OperateActiveProto:GetSkinRebateInfoRet", {"skinIdList": []}, "SkinRebate"),
    "OperateActiveProto:GetDragonBoatFestivalInfo": ReadSpec("OperateActiveProto:GetDragonBoatFestivalInfoRet", {"infos": [], "type": 0, "isTake": 0}, note="No enabled festival schedule"),
    "OperateActiveProto:GetBreakfastCardData": ReadSpec("OperateActiveProto:BreakfastCardDataRet", {"data": []}, "Breakfast", "No paid breakfast-card enrollment"),
    "OperateActiveProto:GetOldSkinRebateInfo": ReadSpec("OperateActiveProto:GetOldSkinRebateInfoRet", {"info": {"isActive": 0, "totalRebate": 0}, "buyInfos": []}),
    "OperateActiveProto:GetHalloweenGameData": ReadSpec("OperateActiveProto:GetHalloweenGameDataRet", {"cnt": 0, "remainCnt": 0, "maxScore": 0}, "HalloweenMenu"),
    "OperateActiveProto:GetChristmasGiftData": ReadSpec("OperateActiveProto:GetChristmasGiftDataRet", {"id": 0, "cnt": 0, "remainCnt": 0, "maxScore": 0}, "MerryChristmas"),
    "OperateActiveProto:GetQuestionInfo": ReadSpec("OperateActiveProto:GetQuestionInfoRet", {"openTime": 0, "answerCnt": 0, "reward": [], "questionInfos": []}),
    "OperateActiveProto:GetSkinDiscountDaily": ReadSpec("OperateActiveProto:GetSkinDiscountDailyRet", {"dailyReward": False}, "SkinDealsMain"),
    "QuestionnaireProto:GetInfo": ReadSpec("QuestionnaireProto:GetInfoRet", {"infos": []}),
    "LovePlusProto:GetChapterSimpleInfo": ReadSpec("LovePlusProto:GetChapterSimpleInfoRet", {"chapterInfo": [], "imgIds": []}, "LovePlusView"),
    "CrossBossProto:GetData": ReadSpec("CrossBossProto:GetDataRet", {"actId": 0, "state": 0, "curStage": 0, "stageProgress": 0, "nodeList": [], "buffList": []}),
    "PlayerProto:GetColletData": ReadSpec("PlayerProto:GetColletDataRet", {"score": 0, "data": []}, note="No recharge purchases; informational read only"),
    "PlayerProto:GetColletDataByType": ReadSpec("PlayerProto:GetColletDataByTypeRet", {"type": 0, "openTime": 0, "closeTime": 0, "score": 0, "data": []}, note="No recharge purchases; informational read only"),
}


def render_read(state: dict, request: str, fields: dict) -> list[Reply]:
    spec = READS[request]
    result = deepcopy(spec.defaults)
    unlocked = spec.gate is None or feature_open(state, spec.gate)
    if unlocked:
        saved = state.get("initialization", {}).get(request, {})
        if not isinstance(saved, dict):
            raise StorageError("Malformed local initialization read state")
        result.update(deepcopy(saved))
    # Only harmless request identifiers are echoed, never user credentials.
    if request == "RegressionProto:PlrBindInviteList":
        result["page"] = int(fields.get("page", 1))
    if request == "OperateActiveProto:GetQuestionInfo":
        result["id"] = int(fields.get("id", 0))
    if request == "PlayerProto:GetColletDataByType":
        result["type"] = int(fields.get("type", 0))
    if request == 'FightProtocol:GetRogueTInfo':
        # Notification acknowledgements persist even while the mode is locked.
        saved = state.get('ui_preferences', {}).get('rogue_t_window', {})
        for key in ('win1', 'win2'):
            if key in saved:
                result[key] = int(saved[key])
    return [Reply(spec.response, result)]


def _register_read(request):
    @register(request)
    async def handler(ctx, fields):
        return render_read(player_state(ctx), request, fields)
    handler.__name__ = request.replace(":", "_")
    return handler


for _request in READS:
    _register_read(_request)


# PlayerProto:GetSkins / UseSkin are owned by handlers.skins.


# AbilityProto:GetAbility / AddAbility are owned by handlers.ability: the real unlock needs
# the CfgPlrAbility table and the ability-point transaction, which do not belong in a
# bounded read module.


@register("ArmyProto:GetPracticeInfo")
async def practice_info(ctx, fields):
    state = player_state(ctx)
    # A locked new player has zero attempts; no artificial opponents or rank.
    result = {"info": {"start_time": 0, "end_time": 0, "can_join_cnt": 0, "t_join_cnt": 0,
                       "flush_cnt": 0, "rank_level": 0, "max_rank_level": 0, "rank": 0,
                       "max_rank": 0, "score": 0, "can_join_buy_cnt": 0},
              "objs": [], "selfInfo": bool(fields.get("selfInfo", True)),
              "listInfo": bool(fields.get("listInfo", True)), "army_ix": 0, "fightBaseLogs": {}}
    if feature_open(state, "ExerciseLView"):
        result.update(deepcopy(state.get("practice", {})))
    return [Reply("ArmyProto:GetPracticeInfoRet", result)]


@register("ArmyProto:FreeMatchInfo")
async def free_match_info(ctx, fields):
    state = player_state(ctx)
    # ExerciseRMgr.lua:18 unconditionally dereferences reward_info.
    result = {"cfg_id": 0, "score": 0, "rank": 0, "max_rank": 0, "can_join_cnt": 0,
              "reward_info": {"join_cnt": 0, "get_join_cnt_id": 0, "get_rank_lv_id": 0,
                              "win_cnt": 0, "get_win_cnt_ix": 0},
              "role_panel_id": int(state.get("login", {}).get("role_panel_id", 0)), "live2d": 0}
    if feature_open(state, "ExerciseLView"):
        result.update(deepcopy(state.get("free_match", {})))
    return [Reply("ArmyProto:FreeMatchInfoRet", result)]


@register("DormProto:GetSelfTheme")
async def dorm_themes(ctx, fields):
    state = player_state(ctx)
    types = fields.get("themeTypes", [0])
    if not isinstance(types, list) or len(types) > 16:
        raise StorageError("Invalid dorm theme type selection")
    themes = state.get("dorm_themes", {}) if feature_open(state, "Dorm") else {}
    if not types:
        types = [0]
    return [Reply("DormProto:GetSelfThemeRet", {"themeType": int(kind),
                "themes": deepcopy(themes.get(str(kind), {})), "isFinish": index == len(types) - 1})
            for index, kind in enumerate(types)]


@register("PlayerProto:GetAllMusic")
async def owned_music(ctx, fields):
    state = player_state(ctx)
    return [Reply("PlayerProto:GetAllMusicRet", {"data": deepcopy(state.get("owned_music", [])), "is_finish": True})]


@register("PlayerProto:GetNewPanel")
async def panels(ctx, fields):
    state = player_state(ctx)
    result = panel_snapshot(state)
    # The random frames go first: PlayerProto.lua:1377-1378 runs
    # CRoleDisplayMgr:LoginCheck() inside the GetNewPanelRet callback and that
    # login check reads random_panels through GetRealLen (CRoleDisplayMgr.lua:687-699),
    # so a random snapshot delivered afterwards would never rotate the board on
    # login. CRoleDisplayMgr.lua:174-177 accepts random_idx/name_list only when
    # finish is true, so an empty board set still sends one finished frame.
    replies = reply_chunks.random_panels(ctx.server.codec, result["random_panels"],
                                         result["random_idx"], result["name_list"])
    result.pop("random_panels", None)
    result.pop("random_idx", None)
    result.pop("name_list", None)
    replies.append(Reply("PlayerProto:GetNewPanelRet", result))
    return replies


def panel_snapshot(state):
    # CRoleDisplayMgr.lua initializes numbered presets locally; only selected
    # commander is added. No official profile layout is copied.
    # sNewPanel.ids are character/model IDs (CRoleDisplayData.lua:349), not
    # role/card config IDs. A single visible slot must be the top slot so
    # MenuView.lua:821 executes its loading-completion callback.
    # random_type defaults to 1 and never to 0: CRoleDisplayMgr.lua:89-91 falls
    # back with 'panelRet.random_type or SINGLE', and Lua treats 0 as truthy, so
    # a stored 0 would make GetRandomPanels(0) answer an empty list.
    from panel_service import stored
    selected = int(state.get("login", {}).get("panel_id", 0))
    if not selected:
        selected = int(state["cards"][0]["skin"])
    result = {"panels": {"1": {"idx": 1, "ids": [selected],
                               "detail1": {"x": 0, "y": 0, "scale": 1, "live2d": False, "top": True},
                               "detail2": {"x": 0, "y": 0, "scale": 1, "live2d": False, "top": False},
                               "bg": int(state.get("login", {}).get("background_id", 1)), "ty": 1}},
              "setting": 0, "random": 0, "using": 1, "update_time": 0, "random_type": 1,
              "random_panels": {}, "random_idx": 7, "name_list": []}
    saved = stored(state)
    stored_panels = saved.pop("panels")
    result.update(saved)
    result["panels"].update(stored_panels)
    if result["random"] == 1:
        chosen = result["random_panels"].get(str(result["using"]))
        if isinstance(chosen, dict):
            result["random_panel"] = deepcopy(chosen)
    return result


@register('PlayerProto:SetNewPanel')
async def set_panels(ctx, fields):
    from panel_service import save
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        save(tx.state, fields, panel_snapshot(tx.state))
        # Same shape as the login read, including random_panel when the saved
        # selection is a random board (panel_snapshot decides that once).
        reply = Reply('PlayerProto:GetNewPanelRet', panel_snapshot(tx.state))
        ctx.server.codec.encode_frame(reply.name, reply.fields)
    return [reply]


@register('PlayerProto:SetNewPanelUsing')
async def use_panel(ctx, fields):
    from panel_service import save
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        result = save(tx.state, {'using': fields.get('using')}, panel_snapshot(tx.state))
        data = {key: result[key] for key in ('using', 'update_time')}
        # CRoleDisplayMgr.lua:184-186 applies a random board only when the reply
        # carries it, so selecting one must answer with that board.
        chosen = result['random_panels'].get(str(result['using'])) if result['random'] == 1 else None
        if isinstance(chosen, dict):
            data['random_panel'] = deepcopy(chosen)
        reply = Reply('PlayerProto:SetNewPanelUsingRet', data)
        ctx.server.codec.encode_frame(reply.name, reply.fields)
    return [reply]


@register("FightProtocol:GetBossActivityInfo")
async def world_boss_info(ctx, fields):
    player_state(ctx)
    # The requested legacy list initializes WorldBossMgr only. GlobalBossMgr
    # is another manager whose Init leaves timers nil; its own closed-state
    # push is needed before MenuView initializes timers. Omitting bossId skips
    # GlobalBossData.InitCfg while SetInfo always sets closeTime to zero.
    # No configured/live encounter or reward is enrolled by this read.
    return [Reply("FightProto:GetBossActivityInfo", {"list": []}),
            Reply("FightProto:GlobalBossInfoRet", {"beginTime": 0, "endTime": 0, "hp": 0})]


@register("OperateActiveProto:GetRichManData")
async def rich_man_info(ctx, fields):
    state = player_state(ctx)
    now = local_time(state)
    rows = config_table("cfgcfgMonopoly.lua")
    active = next((row for row in rows.values() if row.get("nStartTime", 0) <= now < row.get("nEndTime", 0)), None)
    cfg = active or max(rows.values(), key=lambda row: row.get("nStartTime", 0))
    # RichManInfo.SetData unconditionally builds a configured map even when
    # the seasonal event has expired. Preserve its original schedule instead
    # of returning Lua-truthy ID zero or inventing a new event period.
    map_id = int(cfg["Temp"][0])
    grid = config_table("cfgcfgMonopolyGrid.lua")[map_id]
    start = next(row for row in grid["infos"] if row.get("type") == 1)
    result = {"cfgId": int(cfg["id"]), "sort": int(start["index"]), "mapId": map_id,
              "eventList": [], "throwCnt": 0}
    saved = state.get("rich_man", {}).get(str(cfg["id"]))
    if active and saved and feature_open(state, "RichManMain"):
        saved_map = config_table("cfgcfgMonopolyGrid.lua").get(int(saved.get("mapId", map_id)))
        if saved_map and int(saved.get("sort", result["sort"])) in {row["index"] for row in saved_map["infos"]}:
            for key in ("mapId", "sort", "eventList", "throwCnt"):
                if key in saved:
                    result[key] = deepcopy(saved[key])
    return [Reply("OperateActiveProto:GetRichManDataRet", result)]


def reset_times(timestamp: int) -> dict:
    # Local reset policy uses the sign calendar's configured 03:00 boundary,
    # Monday weeks and first-of-month. Exact official shop/task reset rules
    # were not recovered; this is not evidence for the original server policy.
    zone = timezone(timedelta(hours=8))
    now = datetime.fromtimestamp(timestamp, zone)
    day = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if day <= now:
        day += timedelta(days=1)
    week = day + timedelta(days=(-day.weekday()) % 7)
    first = now.replace(day=1, hour=3, minute=0, second=0, microsecond=0)
    if first <= now:
        if first.month == 12:
            first = first.replace(year=first.year + 1, month=1)
        else:
            first = first.replace(month=first.month + 1)
    return {"d_time": int(day.timestamp()), "w_time": int(week.timestamp()), "m_time": int(first.timestamp())}


@register("ExplorationProto:GetTaskResetTime")
async def exploration_reset(ctx, fields):
    state = player_state(ctx)
    return [Reply("ExplorationProto:GetTaskResetTimeRet", reset_times(local_time(state)))]


@register("ExplorationProto:GetInfo")
async def exploration(ctx, fields):
    state = player_state(ctx)
    identifier = int(fields.get("id", 0))
    result = {"id": identifier, "lv": 1, "exp": 0, "type": 0,
              "get_infos": {}, "can_get_cnt": 0, "ex_can_get_cnt": 0}
    if feature_open(state, "ExplorationMain"):
        result.update(deepcopy(state.get("explorations", {}).get(str(identifier), {})))
    return [Reply("ExplorationProto:GetInfoRet", result)]


@register("PermitProto:GetInfo")
async def permits(ctx, fields):
    state = player_state(ctx)
    wanted = int(fields.get("id", -1))
    rows = deepcopy(state.get("permits", []))
    if wanted != -1:
        rows = [row for row in rows if row.get("id") == wanted]
    return [Reply("PermitProto:GetInfoRet", {"list": rows})]


@register("EquipProto:EquipRefreshGetLastData")
async def equip_refresh(ctx, fields):
    state = player_state(ctx)
    return [Reply("EquipProto:EquipRefreshGetLastDataRet", deepcopy(state.get("last_equip_refresh", {})))]


@register("TaskProto:GetRoleGuideBaseInfo")
async def role_guides(ctx, fields):
    state = player_state(ctx)
    rows = state.get("role_guide_infos", []) if feature_open(state, "CharacterRaising") else []
    return [Reply("TaskProto:GetRoleGuideBaseInfoUpdate", {"infos": deepcopy(rows)})]


@register("AbattoirProto:GetSeasonData")
async def colosseum_season(ctx, fields):
    state = player_state(ctx)
    rows = config_table("cfgcfgColosseum.lua")
    def schedule(row):
        return [int(datetime.strptime(row[key], "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
                for key in ("begTime", "endTime")]
    now = local_time(state)
    timed = [(row, schedule(row)) for row in rows.values()]
    active = next(((row, times) for row, times in timed if times[0] <= now <= times[1]), None)
    if active is None:
        # Returning the latest configured schedule preserves a valid cfg id for
        # unconditional client consumers while its original expired/end times
        # keep the entry closed. No invented future season is enrolled.
        active = max(timed, key=lambda pair: pair[1][0])
    cfg, times = active
    result = {"id": cfg["id"], "startTime": times[0], "endTime": times[1], "rewardTime": 0,
              "randRefreshTime": 0, "selectRefreshTime": 0, "scoreData": [],
              "randModData": {"randLvs": [], "selectCardData": {}, "isGet": False, "isOver": False},
              "isRandPay": False, "isSelectPay": False, "freeCnt": 0}
    # Saved state belongs to this season and is eligible only after the source
    # feature gate and schedule both allow it. Closed accounts see no run/reward.
    saved = state.get("colosseum", {}).get(str(cfg["id"]))
    if saved and feature_open(state, "ColosseumView") and times[0] <= now <= times[1]:
        for key in ("rewardTime", "randRefreshTime", "selectRefreshTime", "scoreData", "randModData",
                    "isRandPay", "isSelectPay", "freeCnt"):
            if key in saved:
                result[key] = deepcopy(saved[key])
    return [Reply("AbattoirProto:GetSeasonDataRet", result)]


def sign_info(state: dict, fields: dict, timestamp: int) -> list[Reply]:
    """Read current daily calendar without recording a sign or awarding items.

    CfgSignReward type1 is month/date based (GEnum.lua:840); the month and day
    change at Beijing 03:00 (SignInMgr.lua:26). Continuous/event enrollment is
    an explicit server state list; not every historical catalog is enabled.
    """
    calendar = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))) - timedelta(hours=3)
    requested = int(fields.get("id", 0))
    replies = []
    sign_state = state.get("signs", {})
    enrollment = {int(value) for value in state.get("enabled_sign_ids", [])}
    for identifier, cfg in sorted(config_table("cfgCfgSignReward.lua").items()):
        identifier = int(identifier)
        if requested not in (0, identifier) or identifier == 5001:
            continue  # paid sign-in catalog is outside this local server scope
        begin, end = cfg.get("nBegTime", 0), cfg.get("nEndTime", 0)
        if begin and timestamp < begin or end and timestamp >= end:
            continue
        if cfg.get("type") == 1:
            index = calendar.month
        elif identifier in enrollment:
            index = 1
        else:
            continue
        if not any(row.get("index") == index for row in cfg.get("infos", [])):
            continue
        saved = sign_state.get(f"{identifier}_{index}", {})
        indexs = {int(day): deepcopy(value) for day, value in saved.get("indexs", {}).items()}
        rewards = {"index": index, "indexs": indexs,
                   "muCheckinCost": int(saved.get("muCheckinCost", 0))}
        for key in ("lastSingTime", "firstSingTime"):
            if key in saved:
                rewards[key] = int(saved[key])
        replies.append(Reply("ClientProto:GetSignInfoRet", {"id": identifier, "index": index,
                             "rewardsInfos": rewards, "is_end": False, "is_mucheckin": False}))
    if replies:
        replies[-1].fields["is_end"] = True
    else:
        replies.append(Reply("ClientProto:GetSignInfoRet", {"id": 0, "index": 0, "is_end": True}))
    return replies


@register("ClientProto:GetSignInfo")
async def signs(ctx, fields):
    state = player_state(ctx)
    return sign_info(state, fields, local_time(state))


# Explicitly excluded here: AddSign (owned by handlers.sign_in), GetRewardByType (claims), shop Buy /
# Exchange, payments, task completion, all gameplay mutations. Core/peer modules
# own Get/SetClientData, Setting, PlrPaneInfo, cards/equips, gacha and InitFinish.
UNCOVERED = ["continuous/event/makeup sign-ins (handlers.sign_in initially covers daily only)",
             "ClientProto:GetMemberRewardInfo", "OperateActiveProto:GetMaidCoffeeData",
             "OperateActiveProto:GetPopupPackInfo", "role-training / seasonal / event task domains",
             "active event schedules and battle gameplay after unlock"]
