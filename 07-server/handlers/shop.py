"""Local non-payment shop: configured costs, stock and atomic persistent grants."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
import json
from pathlib import Path
import re

from database import StorageError
from server_core import Reply, register
from seed_generator import LUA_DIR, python_data
from config_codec import app_path, parse_lua_table
from handlers.initialization import feature_open, local_time, reset_times, cleared_stages
from handlers.tasks import normalize_long_strings, table_end, integer, advance_tasks, grant_rewards
from access_policy import enabled

FILES = {"cfgCfgCommodity.lua", "cfgCfgCommodity2.lua", "cfgCfgCommodity3.lua", "cfgCfgShopPage.lua",
         "cfgCfgShopTab.lua", "cfgCfgRandCommodity.lua", "cfgCfgExchange.lua", "cfgRewardInfo.lua",
         "cfgCfgSkinInfo.lua", "cfgcharacter.lua", "cfgCardData.lua", "cfgItemInfo.lua",
         "cfgSection.lua", "cfgCfgArchiveMultiPicture.lua", "cfgCfgMenuBg.lua",
         "cfgCfgItemExchange.lua"}
TZ = timezone(timedelta(hours=8))
RECORD_KEY = re.compile(r"\[([0-9]+)\]\s*=\s*")


@lru_cache(maxsize=16)
def catalog(filename: str) -> dict:
    if filename not in FILES:
        raise StorageError("Unsupported shop catalog")
    source = normalize_long_strings((LUA_DIR / filename).read_text("utf-8-sig"))
    if len(source) > 4_000_000:
        raise StorageError("Oversized shop configuration")
    assignment = re.search(r"_G\[[^\]]+\]\s*=\s*\{", source)
    if assignment is None:
        raise StorageError("Missing static shop table")
    i, result = assignment.end(), {}
    while i < len(source):
        while source[i].isspace() or source[i] == ",":
            i += 1
        if source[i] == "}":
            return result
        match = RECORD_KEY.match(source, i)
        if match:
            i += len(match[0])
        if source[i] != "{":
            raise StorageError("Malformed shop record")
        end = table_end(source, i)
        row = python_data(parse_lua_table(source[i:end]))
        key = integer(row.get("id"), "configuration id", 0 if filename == "cfgRewardInfo.lua" else 1)
        if key in result:
            raise StorageError("Duplicate static shop id")
        result[key], i = row, end
    raise StorageError("Unclosed shop catalog")


@lru_cache(maxsize=1)
def commodities():
    # The extracted cfgCfgCommodity is the already flattened union, including
    # the small-price/breakfast rows. Do not overlay raw child tables on it.
    from skins_service import products
    result = deepcopy(catalog("cfgCfgCommodity.lua"))
    result.update(products())
    return result


def time_open(now, start=0, end=0):
    return (not start or now >= start) and (not end or now < end)


def unlock_all(state):
    return False  # Compatibility for callers; ordinary shop gates cannot be bypassed.


def archived_atlas(state, cfg):
    return enabled(state, 'illustrations') and (cfg.get('group') == 5 or cfg.get('id') == 5 and cfg.get('showType') == 7)


def open_bounds(state, start=0, end=0, cfg=None):
    # ShopPageData.IsOpen and CommodityData.GetNowTimeCanBuy read server
    # timestamps, so opening only the server predicate leaves the UI closed.
    if cfg and archived_atlas(state, cfg):
        return (0, 0)
    if cfg and cfg.get("jGets") and pool_coin(cfg):
        # 本地策略，无官服样本：非付费道具池货币商品按常开上报。CommodityData.GetNowTimeCanBuy
        # (CommodityData.lua:916-928) 只要 begin/end 非 0 且已过期就把该行灰掉，而这些行绑定的是
        # 已结束的扭蛋活动档期（如 31068 的 2026-08-28~09-16）；离线本地部署要保持可购买。
        # 客户端把 (0,0) 视为常开（ShopPageData.lua:114-125 同一约定）。
        return (0, 0)
    return (start, end)


def shop_state(state):
    data = state.setdefault("shop", {})
    for key in ("purchases", "receipts", "random", "flush_generations"):
        data.setdefault(key, {})
    return data


OPEN, HIDDEN, CLOSED = "open", "hidden", "closed"


def page_gate(state, page_id):
    """Classify one shop page: open / hidden / closed.

    HIDDEN is exactly the one rule the local item-pool-currency policy may clear:
    isHide=1 and the page is not listed in state['shop']['enabled_pages']. Every other
    reason (ShopView gate, showType exclusions, page time window) still answers CLOSED.
    """
    if not feature_open(state, "ShopView"):
        return CLOSED
    cfg = catalog("cfgCfgShopPage.lua").get(page_id)
    if cfg is None or cfg.get("showType") in (3, 4, 5, 8):
        return CLOSED  # Membership/payment/atlas-specific domains remain separate.
    if archived_atlas(state, cfg):
        return OPEN  # Source-backed non-payment pages; no completion/enrollment fabricated.
    if not time_open(local_time(state), cfg.get("nStartTime", 0), cfg.get("nEndTime", 0)):
        return CLOSED
    if cfg.get("isHide") == 1:
        if page_id == 904:
            return OPEN if feature_open(state, "ExerciseLView") else CLOSED
        if page_id == 9001:
            return OPEN  # Only its non-payment, prerequisite-qualified free rows.
        if page_id in state.get("shop", {}).get("enabled_pages", []):
            return OPEN
        return HIDDEN
    return OPEN


def page_open(state, page_id):
    gate = page_gate(state, page_id)
    return gate == OPEN or (gate == HIDDEN and pool_coin_page_open(state, page_id))


def pool_coin_page_open(state, page_id):
    """本地策略，无官服样本：承载当前可售道具池货币的隐藏页按需视为开启。

    Only the HIDDEN rule is cleared, and only for a page that actually carries a
    non-payment item-pool currency commodity whose own windows/thresholds are open.
    ShopView, showType exclusions, the page time window, the enabled_pages list and
    every commodity-level door keep their original meaning.
    """
    return any(display_doors(state, cfg) for cfg in pool_coin_pages().get(page_id, ()))


def limit_pass(state, kind, value):
    if not kind:
        return True
    if unlock_all(state) and kind in (1, 2, 3):
        # GEnum CommodityLimitType: account age, player level and dungeon
        # entrance gates. Relationship/multi-team families stay unsupported.
        return True
    if kind == 1:
        return local_time(state) < int(state["player"].get("create_time", 0)) + value * 86400
    if kind == 2:
        return state["player"]["level"] >= value
    if kind == 3:
        return value in cleared_stages(state)
    return False  # Unimplemented relationship/multi-team/legacy limit families.


def nonpayment(cfg):
    return ((cfg.get("nType") in (1, 2) or cfg.get("_offline_skin") is True) and bool(cfg.get("jGets")) and
            all(isinstance(row, list) and len(row) >= 2 and row[0] > 0
                for key in ("jCosts", "jCosts1") for row in cfg.get(key, [])) and
            all(len(row) in (2, 3) and (len(row) == 2 or row[2] in (2, 3, 4))
                for row in cfg.get("jGets", [])))


@lru_cache(maxsize=1)
def pool_currency_items() -> frozenset:
    """Item ids charged by the configured item pools, from the local item-pool catalog.

    本地策略，无官服样本：货币集合从 07-server/data/item-pool-pools.json 的池消耗推导，
    不写死 id 列表。池消耗 costtype 1/3 的行是 [item, num]，costtype 2 的行是
    [index, item, num]（ItemPoolInfo.lua:206-223）；CfgItemPoolConsume 只有 costNum/drawNum
    没有道具 id，所以被扣的道具只来自池 cost 行。该表由 02-tools/scripts/item-pool-build.py 生成。
    """
    path = app_path("data", "item-pool-pools.json")
    try:
        pools = json.loads(path.read_text(encoding="utf-8"))["pools"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StorageError("Missing local item-pool catalog for shop currency detection") from error
    items = set()
    for pool in pools.values():
        column = 1 if int(pool.get("costtype", 1)) == 2 else 0
        for row in pool.get("cost", []):
            items.add(int(row[column]))
    if not items:
        raise StorageError("Local item-pool catalog lists no currency")
    return frozenset(items)


def pool_coin(cfg) -> bool:
    """本地策略，无官服样本：非付费商品，且奖励里含任一配置道具池的消耗货币。"""
    if not nonpayment(cfg):
        return False
    currencies = pool_currency_items()
    return any(int(row[0]) in currencies for row in cfg.get("jGets", []))


@lru_cache(maxsize=4096)
def pool_coin_commodity(identifier: int) -> bool:
    cfg = commodities().get(int(identifier))
    return cfg is not None and pool_coin(cfg)


@lru_cache(maxsize=1)
def pool_coin_pages() -> dict:
    """Page id -> the pool-currency commodities it carries (identification is cached)."""
    result = {}
    for cfg in commodities().values():
        if pool_coin(cfg):
            result.setdefault(cfg.get("group"), []).append(cfg)
    return result


def display_doors(state, cfg):
    """The purchase doors of one commodity that do not depend on its page."""
    # 本地策略，无官服样本：非付费道具池货币商品跳过自身购买档期（扭蛋活动期已结束时离线仍可买）；
    # limitedWeek/limitedTimes、tab 窗口与 nShowLimitType 展示门槛保持原样。
    if not pool_coin(cfg) and not archived_atlas(state, cfg) and not time_open(
            local_time(state), cfg.get("nBuyStart", 0), cfg.get("nBuyEnd", 0)):
        return False
    if cfg.get("limitedWeek") or cfg.get("limitedTimes"):
        return False
    tab = catalog("cfgCfgShopTab.lua").get(cfg.get("tabID"))
    if tab and not archived_atlas(state, cfg) and not time_open(local_time(state), tab.get("nStartTime", 0), tab.get("nEndTime", 0)):
        return False
    return limit_pass(state, cfg.get("nShowLimitType", 0), cfg.get("nShowLimitVal", 0))


def visible(state, cfg):
    if not nonpayment(cfg):
        return False
    gate = page_gate(state, cfg["group"])
    if gate != OPEN:
        # 本地策略，无官服样本：只有承载道具池货币的商品能让 HIDDEN 页放行；同页其它
        # 商品仍被 isHide 挡住，且 CLOSED（ShopView/时间窗/showType）一律不放行。
        if gate != HIDDEN or not (pool_coin_commodity(cfg["id"]) and pool_coin_page_open(state, cfg["group"])):
            return False
    return display_doors(state, cfg)


def cycle(state, cfg):
    """GCalculatorHelp.lua:1453-1591: 2021-11-01 midnight cycle anchor."""
    now = local_time(state)
    kind, value = int(cfg.get("nResetType", 0)), int(cfg.get("nResetValue", 0))
    if kind == 0:
        return "permanent", 0
    if kind == 4:
        generation = state.get("shop", {}).get("flush_generations", {}).get(str(value), 0)
        return "flush:" + str(value) + ":" + str(generation), 0
    if kind not in (1, 2, 3) or value < 1:
        raise StorageError("Unsupported commodity refresh cycle")
    current = datetime.fromtimestamp(now, TZ)
    if kind in (1, 2):
        anchor = int(datetime(2021, 11, 1, tzinfo=TZ).timestamp())
        seconds = value * 86400 * (7 if kind == 2 else 1)
        period = (now - anchor) // seconds
        return str(kind) + ":" + str(value) + ":" + str(period), anchor + (period + 1) * seconds
    months = (current.year - 2021) * 12 + current.month - 11
    period = months // value
    serial = 2021 * 12 + 10 + (period + 1) * value
    next_time = int(datetime(serial // 12, serial % 12 + 1, 1, tzinfo=TZ).timestamp())
    return "3:" + str(value) + ":" + str(period), next_time


def purchase_record(state, cfg):
    data = shop_state(state)
    period, reset = cycle(state, cfg)
    row = data["purchases"].setdefault(str(cfg["id"]), {"buy_sum": 0, "last_buy_time": 0, "total_bought": 0})
    if row.get("period") != period:
        row.update(period=period, buy_sum=0, reset_time=reset)
    return row


def can_purchase(state, cfg):
    if cfg.get('group') == 5:
        pictures = catalog('cfgCfgArchiveMultiPicture.lua')
        items = {v.get('itemId') for v in pictures.values()}
        if not cfg.get('jGets') or any(r[0] not in items or r[1] != 1 or len(r) > 2 and r[2] != 2 for r in cfg['jGets']):
            return False
        if any(int(state.get('inventory', {}).get(str(r[0]), 0)) > 0 for r in cfg['jGets']):
            return False
    if cfg.get("_offline_skin"):
        from skins_service import unavailable
        if unavailable(state,cfg):
            return False
    if not limit_pass(state, cfg.get("nBuyLimitType", 0), cfg.get("nBuyLimitVal", 0)):
        return False
    previous = cfg.get("prerequisiteID")
    if previous and not shop_state(state)["purchases"].get(str(previous), {}).get("total_bought", 0):
        return False
    return True


def commodity_info(state, cfg):
    record = purchase_record(state, cfg)
    # 本地策略，无官服样本：道具池货币商品不限购。can_buy_cnt is signed on the wire
    # (GameMsg sCommodity), and CommodityData:GetNum()==-1 is the client's unlimited value.
    unlimited = pool_coin_commodity(cfg["id"])
    maximum = int(cfg.get("nSumBuyLimit", -1))
    remaining = -1 if unlimited or maximum < 0 else max(0, maximum - record["buy_sum"])
    if not can_purchase(state, cfg):
        remaining = 0
    config = {key: deepcopy(cfg[key]) for key in ("nDiscountStart", "nDiscountEnd", "fDiscount", "jGets", "jExGets",
          "jCosts", "nOnecBuyLimit", "nResetType", "nResetValue", "jCosts1", "BonusItemID") if key in cfg}
    # nSumBuyLimit is uint on wire; can_buy_cnt is signed and carries -1.
    config["nSumBuyLimit"] = max(0, maximum)
    if "nOnecBuyLimit" in config:
        # ShopPackPayView.lua:64 maps its signed unlimited sentinel to99; the pool-currency
        # rows use that same local unlimited sentinel for their per-purchase cap.
        config["nOnecBuyLimit"] = (99 if unlimited or int(config["nOnecBuyLimit"]) < 0
                                   else int(config["nOnecBuyLimit"]))
    config["isShow"] = 0  # CommodityData.IsShow: server value 1 hides the row.
    start, end = open_bounds(state, cfg.get("nBuyStart", 0), cfg.get("nBuyEnd", 0), cfg)
    result = {"id": cfg["id"], "open_time": start, "close_time": end,
              "shop_id": cfg["group"], "last_buy_time": record["last_buy_time"], "buy_sum": record["buy_sum"],
              "reset_time": record["reset_time"], "can_buy_cnt": remaining,
              "cnt": min(32767, max(-1, int(cfg.get("nOnecBuyLimit", 1)))), "shop_config": config}
    if cfg.get("tabID"):
        result["group_id"] = cfg["tabID"]
    return result


@register("ShopProto:GetShopOpenTime")
async def get_open_time(ctx, fields):
    state = ctx.store.get_player(ctx.require_login())
    infos = []
    for page_id, cfg in catalog("cfgCfgShopPage.lua").items():
        opened = page_open(state, page_id)
        start, end = open_bounds(state, cfg.get("nStartTime", 0), cfg.get("nEndTime", 0), cfg) if opened else (0, 1)
        infos.append({"shop_id": page_id, "open_time": start, "close_time": end})
    for tab_id, cfg in catalog("cfgCfgShopTab.lua").items():
        opened = page_open(state, cfg["group"]) and (archived_atlas(state, cfg) or
                 time_open(local_time(state), cfg.get("nStartTime", 0), cfg.get("nEndTime", 0)))
        start, end = open_bounds(state, cfg.get("nStartTime", 0), cfg.get("nEndTime", 0), cfg) if opened else (0, 1)
        infos.append({"shop_id": cfg["group"], "group_id": tab_id,
                      "open_time": start, "close_time": end})
    return [Reply("ShopProto:GetShopOpenTimeRet", {"infos": infos})]


@register("ShopProto:GetShopCommodity")
async def get_commodity(ctx, fields):
    uid = ctx.require_login()
    page, group = integer(fields.get("shop_id", 0), "shop id"), integer(fields.get("group_id", 0), "tab id")
    with ctx.store.transaction(uid) as tx:
        rows = [commodity_info(tx.state, cfg) for cfg in commodities().values() if visible(tx.state, cfg)
                and (not page or cfg["group"] == page) and (not group or cfg.get("tabID") == group)]
    return [Reply("ShopProto:GetShopCommodityRet", {"infos": rows[i:i + 25], "m_cnt": 0, "is_finish": i + 25 >= len(rows)})
            for i in range(0, max(1, len(rows)), 25)]


@register("ShopProto:GetShopInfos")
async def get_infos(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        rows = [{key: value for key, value in commodity_info(tx.state, cfg).items()
                 if key in ("id", "last_buy_time", "buy_sum", "reset_time", "can_buy_cnt", "cnt")}
                for cfg in commodities().values() if visible(tx.state, cfg)]
    return [Reply("ShopProto:GetShopInfosAdd", {"infos": rows[i:i + 80], "m_cnt": 0, "is_finish": i + 80 >= len(rows)})
            for i in range(0, max(1, len(rows)), 80)]


@register("ShopProto:GetShopResetTime")
async def get_reset_time(ctx, fields):
    state = ctx.store.get_player(ctx.require_login())
    result = {name: cycle(state, {"nResetType": kind, "nResetValue": 1})[1]
              for name, kind in (("d_time", 1), ("w_time", 2), ("m_time", 3))}
    return [Reply("ShopProto:GetShopResetTimeRet", result)]


def debit(tx, costs):
    from handlers.cards_items import resource_replies
    deltas = {}
    fuel_changed = False
    for key, amount in costs.items():
        amount = integer(amount, "cost")
        if int(key) == 10035:
            tx.add_currency("hot", -amount)
            fuel_changed = True
        else:
            tx.add_item(key, -amount)
            deltas[key] = -amount
    replies = resource_replies(tx.state, deltas)
    if fuel_changed and not any(reply.name == "LoginProto:PlrUpdate" for reply in replies):
        replies.append(Reply("LoginProto:PlrUpdate", {"infos": deepcopy(tx.state["player"])}))
    return replies


def buy_cost(cfg, fields, count, now):
    if fields.get("vouchers") or fields.get("grid1", 0) or fields.get("grid2", 0):
        raise StorageError("Voucher/selection shop effects are not reconstructed")
    key = fields.get("useJCost") or ("jCosts1" if fields.get("useCost") == "price_2" else "jCosts")
    if key not in ("jCosts", "jCosts1") or fields.get("useCost", "price_1") not in ("price_1", "price_2"):
        raise StorageError("Unknown configured price choice")
    if key == "jCosts1" and key not in cfg:
        raise StorageError("Alternate commodity cost is not configured")
    discount = Decimal(1)
    if time_open(now, cfg.get("nDiscountStart", 0), cfg.get("nDiscountEnd", 0)):
        discount = Decimal(str(cfg.get("fDiscount", 1)))
    if not 0 < discount <= 1:
        raise StorageError("Invalid configured commodity discount")
    costs = {}
    for entry in cfg.get(key, []):
        item, quantity = integer(entry[0], "cost item", 1), integer(entry[1], "cost quantity")
        amount = int((Decimal(quantity) * discount).to_integral_value(rounding=ROUND_CEILING)) * count
        if amount:
            costs[item] = integer(costs.get(item, 0) + amount, "total cost")
    return key, costs


def simple_rewards(rows):
    return [{"id": row["id"], "num": row["num"], "type": row["type"]} for row in rows]


def award(tx, rows):
    #10035 is actual fuel, not an ordinary bag item (PlayerClient.lua:365).
    fuel = sum(row["num"] for row in rows if row["type"] == 2 and row["id"] == 10035)
    from skins_service import item_models, award as award_skins
    skin_ids = item_models()
    skin_rows = [row for row in rows if row["type"] == 2 and row["id"] in skin_ids]
    rendered, replies = grant_rewards(tx, [row for row in rows if row not in skin_rows
                                          and not (row["type"] == 2 and row["id"] == 10035)])
    if skin_rows:
        skin_gets, skin_replies = award_skins(tx,skin_rows)
        rendered.extend(skin_gets)
        replies.extend(skin_replies)
    if fuel:
        tx.add_currency("hot", fuel)
        rendered.append({"id": 10035, "num": fuel, "type": 2})
        replies.append(Reply("LoginProto:PlrUpdate", {"infos": deepcopy(tx.state["player"])}))
    return rendered, replies


class _ReceiptReplay(Exception):
    def __init__(self,replies):
        self.replies = replies

@register("ShopProto:Buy")
async def buy(ctx, fields):
    from skins_service import checked
    uid = ctx.require_login()
    identifier = integer(fields.get("id", 0), "commodity id", 1)
    quantity = integer(fields.get("buy_sum", 1), "purchase quantity", 1, 32767)
    timestamp = integer(fields.get("buy_time", 0), "purchase timestamp", 0, 4294967295)
    cfg = commodities().get(identifier)
    if cfg is None:
        raise StorageError("Unknown configured commodity")
    # 本地策略，无官服样本：道具池货币商品不限购，跳过单次与累计限购校验。
    unlimited = pool_coin_commodity(identifier)
    if not unlimited and int(cfg.get("nOnecBuyLimit",1)) > 0 and quantity > int(cfg["nOnecBuyLimit"]):
        raise StorageError("Commodity per-purchase quantity exceeded")
    try:
        with ctx.store.transaction(uid) as tx:
            key, costs = buy_cost(cfg, fields, quantity, local_time(tx.state))
            record = purchase_record(tx.state, cfg)
            receipt_key = ":".join(map(str,(identifier,timestamp,quantity,key))) if timestamp else "free:" + str(identifier) + ":" + record["period"]
            # A committed retry is valid after ownership makes the shelf sold out.
            # Exit by exception so no read-only replay changes the save revision.
            if receipt_key in shop_state(tx.state)["receipts"]:
                raise _ReceiptReplay(checked(ctx,[Reply("ShopProto:BuyRet",{
                    "id":identifier,"info":commodity_info(tx.state,cfg),"gets":[],
                    "add_bufs":[],"m_cnt":0,"useJCost":key})]))
            if not visible(tx.state,cfg) or not can_purchase(tx.state,cfg):
                raise StorageError("Commodity is paid, closed, locked or already owned")
            if costs and (not timestamp or abs(timestamp-local_time(tx.state))>120):
                raise StorageError("Purchase timestamp is missing or stale")
            once,total=int(cfg.get("nOnecBuyLimit",1)),int(cfg.get("nSumBuyLimit",-1))
            if not unlimited and ((once>0 and quantity>once) or (total>=0 and record["buy_sum"]+quantity>total)):
                raise StorageError("Commodity purchase limit exceeded")
            replies=debit(tx,costs)
            rows=[{"id":r[0],"num":integer(r[1]*quantity,"reward quantity",1),
                   "type":r[2] if len(r)>2 else 2} for r in cfg["jGets"]]
            rendered,grants=award(tx,rows)
            replies.extend(grants)
            record.update(buy_sum=record["buy_sum"]+quantity,
                          total_bought=record["total_bought"]+quantity,
                          last_buy_time=local_time(tx.state))
            shop_state(tx.state)["receipts"][receipt_key]={
                "id":identifier,"time":local_time(tx.state),"quantity":quantity,
                "costs":costs,"rewards":deepcopy(rendered)}
            replies.extend(advance_tasks(tx.state,"shop_exchange",1))
            replies.append(Reply("ShopProto:BuyRet",{
                "id":identifier,"info":commodity_info(tx.state,cfg),
                "gets":simple_rewards(rendered),"add_bufs":[],"m_cnt":0,"useJCost":key}))
            checked(ctx,replies)
        return replies
    except _ReceiptReplay as replay:
        return replay.replies


def random_schedule(state, cfg):
    now = local_time(state)
    values, kind = cfg.get("aFlushTimes", []), cfg.get("nFlushType", 5)
    if kind == 1 and len(values) == 1:
        return cycle(state, {"nResetType": 1, "nResetValue": values[0]})
    if kind != 5 or not values or any(not isinstance(v, int) or not 0 <= v <= 23 for v in values):
        raise StorageError("Random-shop schedule is not reconstructed")
    current = datetime.fromtimestamp(now, TZ)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    points = sorted(int((midnight + timedelta(days=day, hours=hour)).timestamp())
                    for day in (-1, 0, 1) for hour in values)
    previous = max(value for value in points if value <= now)
    return str(previous), min(value for value in points if value > now)


def pick(state, rows, count):
    from handlers.gacha import gacha_state, random_below
    choices, result = list(rows), []
    while choices and len(result) < count:
        weights = [Decimal(str(row.get("s_probability", row.get("probability", 1)))) for row in choices]
        scale = Decimal(10) ** min(30, max(max(0, -weight.as_tuple().exponent) for weight in weights))
        amounts = [max(0, int(weight * scale)) for weight in weights]
        if not sum(amounts):
            break
        draw = random_below(gacha_state(state), sum(amounts))
        for index, amount in enumerate(amounts):
            if draw < amount:
                result.append(choices.pop(index))
                break
            draw -= amount
    return result


def eligible_reward(state, row):
    if unlock_all(state):
        return True  # Keep source leaves, weights, prices and stock; bypass only gates.
    level = row.get("level")
    if level and not level[0] <= state["player"]["level"] <= level[-1]:
        return False
    return all(value in cleared_stages(state) for value in row.get("dupID", []))


def generate_pool(state, reward_id, depth=0):
    if depth > 12:
        raise StorageError("Deep random-shop template nesting")
    cfg = catalog("cfgRewardInfo.lua").get(reward_id)
    if not cfg or cfg["type"] not in (2, 3, 5):
        raise StorageError("Unsupported random-shop template")
    candidates = []
    for source in cfg.get("item", []):
        if not eligible_reward(state, source):
            continue
        if source["type"] == 1:
            candidates.extend(generate_pool(state, source["id"], depth + 1))
        elif source["type"] in (2, 3, 4) and source.get("price") and source["price"][0] > 0:
            row = deepcopy(source)
            row["reward_id"] = reward_id
            candidates.append(row)
    # Explicit local random-shop policy: nested pools select their configured
    # dropCnt without replacement, parent type5 selects its final dropCnt. Missing
    # probabilities use equal weights; no official-server algorithm claim.
    return candidates if cfg["type"] == 2 else pick(state, candidates, int(cfg.get("dropCnt", 1)))


def roll_discount(state, row):
    choices = [{"discount": pair[0], "probability": pair[1]} for pair in row.get("disProbability", [])]
    choice = pick(state, choices, 1)
    return Decimal(str(choice[0]["discount"])) if choice else Decimal(1)


def random_stock(state, cfg, refresh=False):
    period, next_time = random_schedule(state, cfg)
    data = shop_state(state)["random"]
    old = data.get(str(cfg["id"]))
    if not refresh and old and old["period"] == period:
        return old
    slots = []
    for row in generate_pool(state, cfg["nRewardId"]):
        discount = roll_discount(state, row)
        if not 0 < discount <= 1:
            raise StorageError("Invalid random-shop discount")
        cost = int((Decimal(row["price"][1]) * discount).to_integral_value(rounding=ROUND_CEILING))
        value = {"reward_id": row["reward_id"], "id": row["id"], "num": row["count"], "price": [row["price"][0], cost],
                 "type": row["type"], "dis": float(discount), "index": row["index"], "had_get": 0}
        if "buyLimit" in row:
            value["buyLimit"] = row["buyLimit"]
        slots.append(value)
    result = {"period": period, "next_hour": next_time, "slots": slots,
              "refresh_count": old.get("refresh_count", 0) + int(refresh) if old else int(refresh)}
    data[str(cfg["id"])] = result
    return result


def random_config(state, identifier):
    cfg = catalog("cfgCfgRandCommodity.lua").get(identifier)
    if not cfg or not page_open(state, cfg["group"]):
        raise StorageError("Random shop is unknown or locked")
    return cfg


@register("ShopProto:GetExchangeInfo")
async def get_exchange(ctx, fields):
    uid = ctx.require_login()
    identifier = integer(fields.get("cfgid", 0), "exchange shop id", 1)
    refresh = fields.get("is_flush", False)
    if not isinstance(refresh, bool):
        raise StorageError("Invalid random-shop refresh flag")
    with ctx.store.transaction(uid) as tx:
        cfg = random_config(tx.state, identifier)
        replies = []
        if refresh:
            if not cfg.get("aManFlushCosts"):
                raise StorageError("Manual refresh is not configured")
            costs = {}
            for key, amount in cfg["aManFlushCosts"]:
                costs[key] = costs.get(key, 0) + integer(amount, "refresh cost", 1)
            replies.extend(debit(tx, costs))
        stock = random_stock(tx.state, cfg, refresh)
        replies.append(Reply("ShopProto:GetExchangeInfoRet", {"cfgid": identifier,
                       "infos": deepcopy(stock["slots"]), "next_hour": stock["next_hour"]}))
    return replies


@register("ShopProto:Exchange")
async def exchange(ctx, fields):
    uid = ctx.require_login()
    identifier, index = integer(fields.get("cfgid", 0), "exchange shop id", 1), integer(fields.get("index", 0), "slot", 1, 255)
    item, quantity = integer(fields.get("id", 0), "exchange item", 1), integer(fields.get("num", 0), "exchange quantity", 1, 32767)
    with ctx.store.transaction(uid) as tx:
        cfg = random_config(tx.state, identifier)
        stock = random_stock(tx.state, cfg)
        if index > len(stock["slots"]):
            raise StorageError("Random-shop slot is unavailable")
        row = stock["slots"][index - 1]
        if row["id"] != item:
            raise StorageError("Random-shop slot changed; fetch current stock")
        limit = row.get("buyLimit")
        if limit is not None and row["had_get"] + quantity > limit:
            raise StorageError("Random-shop stock limit exceeded")
        replies = debit(tx, {row["price"][0]: integer(row["price"][1] * quantity, "exchange total cost")})
        rendered, grants = award(tx, [{"id": item, "num": integer(row["num"] * quantity, "exchange reward", 1), "type": row["type"]}])
        replies.extend(grants)
        row["had_get"] += quantity
        shop_state(tx.state).setdefault("exchange_history", []).append({"cfgid": identifier, "slot": index,
                       "id": item, "quantity": quantity, "cost": row["price"], "time": local_time(tx.state)})
        replies.extend(advance_tasks(tx.state, "shop_exchange", 1))
        result = {"cfgid": identifier, "index": index, "id": item, "num": quantity,
                  "had_get": row["had_get"], "gets": simple_rewards(rendered), "add_bufs": []}
        if limit is not None:
            result["buyLimit"] = limit
        replies.append(Reply("ShopProto:ExchangeRet", result))
    return replies
