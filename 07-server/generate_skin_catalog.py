"""Generate finite, source-backed skin sales and ownership metadata. No network."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from handlers.shop import catalog
from seed_generator import LUA_DIR

HERE = Path(__file__).resolve().parent
FILES = ("cfgCfgSkinInfo.lua", "cfgcharacter.lua", "cfgCardData.lua",
         "cfgItemInfo.lua", "cfgCfgCommodity.lua")
NEW_ID = 38037002

def positive_price(rows):
    return (isinstance(rows, list) and bool(rows) and
            all(isinstance(r, list) and len(r) >= 2 and
                isinstance(r[0], int) and r[0] > 0 and r[0] != 10998 and
                isinstance(r[1], int) and r[1] >= 0 for r in rows))

def generate():
    skin_cfg, source_characters, cards, items, goods = (catalog(f) for f in FILES)
    # Use the same finite model-path repairs as the deployed client. Source Lua
    # remains immutable; otherwise restored models still fail dynamic selection.
    characters = deepcopy(source_characters)
    board_policy_path = HERE.parent / '06-client/offline-resources/board-interaction-policy.json'
    board_policy = json.loads(board_policy_path.read_text('utf-8'))
    if board_policy.get('schema_version') != 1:
        raise ValueError('Unexpected board interaction policy schema')
    for model, fields in board_policy['character_overrides'].items():
        if int(model) not in characters or not isinstance(fields.get('l2dName'), str):
            raise ValueError('Missing source character for restored board model')
        characters[int(model)].update(deepcopy(fields))
    skin_items = {i: r for i, r in items.items() if r.get("type") == 9}
    models = {}
    card_rows = {}
    role_cards = {}
    for cid, c in cards.items():
        if c.get("role_id") and c.get("model"):
            row = {k: deepcopy(c[k]) for k in
                   ("id","role_id","model","base_card","breakModels","skin","changeCardIds",
                    "skinMinBreakLv","card_type","main_type","tType") if k in c}
            card_rows[str(cid)] = row
            role_cards.setdefault(c["role_id"], []).append(c)
    for item, r in skin_items.items():
        model, source_card = r.get("dy_value2"), r.get("dy_value1")
        role = cards.get(source_card, {}).get("role_id")
        if model not in skin_cfg or model not in characters or characters[model].get("role_id") != role:
            raise ValueError("Unmatched skin item/model: " + str(item))
        choices = role_cards.get(role, [])
        canonical = cards.get(source_card)
        if canonical is None:
            canonical = next((c for c in choices if c.get("base_card") is True), None)
        if canonical is None or not any(model in (c.get("skin") or []) for c in choices):
            raise ValueError("Missing selectable source skin/card: " + str(model))
        models[str(model)] = {"id":model, "item_id":item, "role_id":role,
                             "card_id":canonical["id"],
                             "select_card_id":next(c["id"] for c in choices if model in (c.get("skin") or [])),
                             "name":r.get("name",""),
                             "price":deepcopy(r.get("price", [])),
                             "has_l2d":bool(characters[model].get("l2dName")),
                             "character":{k: characters[model][k] for k in
                                          ("desc","key","skinType","isHide") if k in characters[model]}}
    if set(map(int, models)) != set(skin_cfg):
        raise ValueError("SkinInfo coverage does not exactly match type9 items")
    products, policies = {}, {}
    covered = set()
    for identifier, src in goods.items():
        rewards = [deepcopy(r) for r in src.get("jGets", [])
                   if isinstance(r, list) and len(r) >= 2 and r[0] in skin_items
                   and (len(r) < 3 or r[2] == 2)]
        if not rewards:
            continue
        if any(r[1] != 1 for r in rewards):
            raise ValueError("Permanent skin sale must contain one of each skin")
        if positive_price(src.get("jCosts1")):
            costs, policy = deepcopy(src["jCosts1"]), "source-jCosts1"
        elif positive_price(src.get("jCosts")):
            costs, policy = deepcopy(src["jCosts"]), "source-jCosts"
        elif identifier == 50025 and not src.get("jCosts") and not src.get("jCosts1"):
            costs, policy = [[10002,0]], "source-no-cost-single-claim"
        else:
            sdk = src.get("jCosts", [])
            if sdk and all(isinstance(r,list) and len(r)>=2 and r[0]==-1 and r[1]>0 for r in sdk):
                costs, policy = [[10002,sum(int(r[1]) for r in sdk)]], "local-sdk-amount-1-to-1"
            else:
                prices = [skin_items[r[0]].get("price") for r in rewards]
                if not all(positive_price(p) for p in prices):
                    raise ValueError("No source/local skin price: " + str(identifier))
                sums = {}
                for price in prices:
                    for item, amount, *_ in price:
                        sums[item] = sums.get(item, 0) + amount
                costs, policy = [[k,v] for k,v in sorted(sums.items())], "source-item-price"
        excluded = [deepcopy(r) for r in src.get("jGets", []) if r not in rewards]
        cfg = deepcopy(src)
        cfg.update(group=4, tabID=4001, nType=3, jGets=rewards, jCosts=costs, orgCosts=deepcopy(costs),
                   jCosts1=deepcopy(costs), nSumBuyLimit=1, nOnecBuyLimit=1,
                   nResetType=0,nResetValue=0,nBuyStart=0,nBuyEnd=0,
                   nShowLimitType=0,nShowLimitVal=0,nBuyLimitType=0,nBuyLimitVal=0,
                   limitedWeek=[],limitedTimes=[],fPrice=1,fDiscount=1,
                   nDiscountStart=0,nDiscountEnd=0)
        # Source bundle extras are expressly not part of this local skin-only sale.
        for key in ("prerequisiteID","jExGets","BonusItemID","canUseVoucher","packList"):
            cfg.pop(key, None)
        if excluded:
            cfg["sDesc"] = "本地档案皮肤单售：仅包含所列皮肤；原礼包其他奖励不包含。"
        cfg["_offline_skin"] = True
        products[str(identifier)] = cfg
        policies[str(identifier)] = {"price_policy":policy, "source_group":src.get("group"),
                                    "source_nType":src.get("nType"), "excluded_source_rewards":excluded}
        covered.update(skin_items[r[0]]["dy_value2"] for r in rewards)
    original_covered = len(covered)
    missing = set(skin_cfg) - covered
    if missing != {8037002} or NEW_ID in goods:
        raise ValueError("Unexpected missing source skin or supplemental commodity collision")
    m = models["8037002"]
    products[str(NEW_ID)] = {"id":NEW_ID,"key":str(NEW_ID),"group":4,"tabID":4001,"nType":3,
           "sName":m["name"],"sDesc":"本地档案补售，源物品价格：100金币。此皮肤属于锋流特殊召唤机械角色。",
           "sIcon":items[NEW_ID].get("icon",""),"sort":999,"packageQuality":6,
           "jGets":[[NEW_ID,1,2]],"jCosts":deepcopy(m["price"]),"jCosts1":deepcopy(m["price"]),"orgCosts":deepcopy(m["price"]),
           "nSumBuyLimit":1,"nOnecBuyLimit":1,"nResetType":0,"nResetValue":0,
           "nBuyStart":0,"nBuyEnd":0,"nDiscountStart":0,"nDiscountEnd":0,
           "fDiscount":1,"fPrice":1,"limitedWeek":[],"limitedTimes":[],"_offline_skin":True}
    policies[str(NEW_ID)] = {"price_policy":"source-item-price-supplement","source_item":NEW_ID,
                            "source_role_domain":{"main_type":cards[80370]["main_type"],
                                                 "tType":cards[80370]["tType"]}}
    # Paired-form source packages stay available, and every source model also
    # has a true single-skin option priced by its original ItemInfo.price.
    singleton_items = {r[0] for p in products.values() if len(p["jGets"]) == 1 for r in p["jGets"]}
    for m in models.values():
        item = m["item_id"]
        if item in singleton_items:
            continue
        if item in goods or str(item) in products or not positive_price(m["price"]):
            raise ValueError("Supplemental single-skin price/id is not source-backed")
        p = deepcopy(products[str(NEW_ID)])
        p.update(id=item,key=str(item),sName=m["name"],sIcon=items[item].get("icon",""),
                 sDesc="本地档案皮肤单售：仅含此皮肤；按源物品价格，不含原礼包附赠。",
                 jGets=[[item,1,2]],jCosts=deepcopy(m["price"]),jCosts1=deepcopy(m["price"]),orgCosts=deepcopy(m["price"]))
        products[str(item)] = p
        policies[str(item)] = {"price_policy":"source-item-price-single-skin","source_item":item}
    provenance = {f:hashlib.sha256((LUA_DIR/f).read_bytes()).hexdigest() for f in FILES}
    data = {"schema_version":1,"source_sha256":provenance,
            "board_interaction_policy_sha256":hashlib.sha256(board_policy_path.read_bytes()).hexdigest(),
            "models":models,
            "cards":card_rows,"products":products,"policies":policies,
            "base_characters":{str(i):{"role_id":r.get("role_id"),"has_l2d":bool(r.get("l2dName"))}
                               for i,r in characters.items() if r.get("role_id")},
            "coverage":{"source_models":len(models),"source_listed_models":original_covered,
                        "all_sale_models":len(covered|missing),"products":len(products)}}
    single_products = {str(m["id"]):next(p["id"] for p in products.values()
                         if p["jGets"]==[[m["item_id"],1,2]]) for m in models.values()}
    overlay = {"schema_version":1,"model_products":single_products,"description":"有限本地皮肤单售映射；不调用SDK，不包含原礼包非皮肤奖励",
               "products":[{k:deepcopy(v) for k,v in p.items() if not k.startswith("_")}
                           for p in products.values()],
               "coverage":data["coverage"],"source_sha256":provenance}
    (HERE/"data"/"skins-catalog.json").write_text(json.dumps(data,ensure_ascii=False,separators=(",",":"))+"\n","utf-8")
    out = HERE.parent/"06-client"/"offline-resources"/"skins-client-catalog.json"
    out.write_text(json.dumps(overlay,ensure_ascii=False,separators=(",",":"))+"\n","utf-8")
    print(json.dumps({"coverage":data["coverage"],"overlay":str(out)},ensure_ascii=False))
    return data

if __name__ == "__main__":
    generate()
