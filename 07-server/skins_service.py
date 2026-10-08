"""Permanent local skin ownership and selection; no bag items or SDK calls."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import time
from config_codec import app_path
from database import StorageError
from server_core import Reply

DATA = app_path("data", "skins-catalog.json")

class SkinRejected(StorageError):
    """Expected user-facing refusal; transaction has rolled back."""

def refusal(ctx,error):
    return checked(ctx,[Reply("SystemProto:Tips",{
        "strId":"GeneralTips","opId":2613,"opName":"PlayerProto:UseSkin",
        "args":[{"type":0,"param":str(error)}]})])

def integer(value, label="skin value", minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value,int) or not minimum <= value <= maximum:
        raise StorageError("Invalid " + label)
    return value

@lru_cache(maxsize=1)
def catalog():
    data = json.loads(DATA.read_text("utf-8"))
    if data.get("schema_version") != 1 or len(data.get("models",{})) != 227:
        raise StorageError("Missing complete local skin catalog")
    return data

def models():
    return catalog()["models"]

def item_models():
    return {m["item_id"]:m for m in models().values()}

def products():
    return {int(k):deepcopy(v) for k,v in catalog()["products"].items()}

def now(state):
    return integer(int(state.get("offline_clock",time.time())), "skin acquisition time")

def card_cfg(identifier):
    cfg = catalog()["cards"].get(str(identifier))
    if cfg is None:
        raise StorageError("Unknown source card for skin")
    return cfg

def temporary(state):
    result = {}
    for row in state.get("skins",[]):
        role = card_cfg(integer(row.get("cfgid"),"skin card",1))["role_id"]
        for value in row.get("ltSkins",[]):
            model = integer(value.get("id"),"temporary model",1)
            meta = models().get(str(model))
            if meta is None or meta["role_id"] != role:
                raise StorageError("Temporary skin/model family mismatch")
            integer(value.get("t",0),"skin expiry",0,4294967295)
            integer(value.get("nTime",0),"loan acquisition",0,4294967295)
            result[model] = deepcopy(value)
    return result

def owned(state,include_temporary=True):
    rows=state.get("skins",[])
    if not isinstance(rows,list):
        raise StorageError("Invalid persisted skin ownership")
    found={}
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get("info",[]),list):
            raise StorageError("Invalid persisted skin row")
        role=card_cfg(integer(row.get("cfgid"),"skin card",1))["role_id"]
        for pair in row.get("info",[]):
            model=integer(pair.get("first"),"owned model",1)
            acquired=integer(pair.get("second",0),"acquisition time")
            meta=models().get(str(model))
            if meta is None or meta["role_id"]!=role:
                raise StorageError("Persisted skin/model family mismatch")
            found[model]=acquired
    if include_temporary:
        for model,value in temporary(state).items():
            if value["t"]>now(state):
                found.setdefault(model,value.get("nTime",0))
    return found

def records(permanent,loans,added=None):
    groups={}
    for model,acquired in sorted(permanent.items()):
        meta=models()[str(model)]
        row=groups.setdefault(meta["card_id"],{"cfgid":meta["card_id"],"info":[],"ltSkins":[],"is_add":False})
        row["info"].append({"first":model,"second":acquired})
        if added and model in added:
            row["is_add"]=True
    for model,value in sorted(loans.items()):
        if model in permanent:
            continue
        meta=models()[str(model)]
        row=groups.setdefault(meta["card_id"],{"cfgid":meta["card_id"],"info":[],"ltSkins":[],"is_add":False})
        row["ltSkins"].append(deepcopy(value))
    return list(groups.values())

def render(state,wanted=0,added=None):
    wanted=integer(wanted,"skin query")
    family=card_cfg(wanted)["role_id"] if wanted else None
    permanent=owned(state,False)
    loans={model:v for model,v in temporary(state).items() if v["t"]>now(state)}
    rows=records(permanent,loans,added)
    return [row for row in rows if family is None or card_cfg(row["cfgid"])["role_id"]==family]

def checked(ctx, replies):
    for reply in replies:
        ctx.server.codec.encode_frame(reply.name,reply.fields)
    return replies

def award(tx,rows):
    known=item_models()
    current=owned(tx.state,False)
    loans=temporary(tx.state)
    selected=[]
    for row in rows:
        meta=known.get(integer(row.get("id"),"skin item",1))
        if meta is None or row.get("type")!=2 or integer(row.get("num"),"skin quantity",1)!=1:
            raise StorageError("Invalid configured skin grant")
        if meta["id"] in current or meta["id"] in selected:
            raise StorageError("Skin already owned; no duplicate charge")
        selected.append(meta["id"])
    stamp=now(tx.state)
    current.update({model:stamp for model in selected})
    # Preserve any existing loan's true t deadline; a purchased model becomes
    # permanent, and other models' rental/expiry history stays unchanged.
    tx.state["skins"]=records(current,loans)
    return deepcopy(rows),[Reply("PlayerProto:GetSkinsRet",{"info":render(tx.state,added=set(selected))})]

def unavailable(state,cfg):
    current = owned(state,False)
    return any(item_models()[r[0]]["id"] in current for r in cfg["jGets"])

def find_card(state,cid):
    cid = integer(cid,"owned card",1,4294967295)
    card = next((c for c in state["cards"] if c["cid"] == cid),None)
    if card is None:
        raise SkinRejected("未持有此角色，无法更换皮肤。")
    return card

def forms(state,card):
    cfg = card_cfg(card["cfgid"])
    role = cfg["role_id"]
    # 同调/形切 (fit_result/tTransfo/召唤) pairings resolve through the whole
    # card family, not merely the primary card's changeCardIds: RoleTool.GetBDSkin_a
    # (RoleTool.lua:517-536) pairs the chosen skin with a sibling card's model, so
    # that sibling must stay a candidate. Only 71010/71012/71020/71022 carry a
    # non-empty changeCardIds, which permanently refused the other families.
    family = [c for c in catalog()["cards"].values() if c["role_id"]==role]
    base = next((c for c in family if c.get("base_card") is True),cfg)
    primary = cfg if cfg.get("base_card") is True else base
    # candidates[0] must not move: validate_model() resolves model==0 from it.
    primary_candidates = [primary] + [c for c in family if c["id"] != primary["id"]]
    alternate_candidates = ([cfg,primary]
                            + [c for c in family if c["id"] not in (cfg["id"],primary["id"])])
    return primary_candidates, alternate_candidates

def validate_model(state,card,model,flag,candidates,enforce_l2d=True,owned_required=True):
    # enforce_l2d=False is used for the alternate slot: the client mirrors one
    # UI switch into both slots (RoleApparel.lua:346), so flag==2 can arrive for
    # an unset/alternate model that has no l2dName. Normalize to 1 there instead
    # of rejecting the whole request; the primary slot stays strict.
    # owned_required=False is used for the alternate slot too: skin_a is the
    # display model the client derives from the fit_result/tTransfo pairing, not
    # an independent purchase, so it needs ownership only on the primary slot.
    # Candidates still confine it to the card's own family.
    model = integer(model,"selected skin",0,4294967295)
    flag = integer(flag,"Live2D choice",1,2)
    if not model:
        resolved = candidates[0]["model"]
    else:
        resolved = model
        allowed = False
        for cfg in candidates:
            if model == cfg["model"]:
                allowed = True
            if model in (cfg.get("skin") or []) and (not owned_required or model in owned(state)):
                allowed = True
            for level,m in enumerate(cfg.get("breakModels") or [],1):
                if m == model and int(card.get("break_level",1)) >= level:
                    allowed = True
            minimum = (cfg.get("skinMinBreakLv") or {}).get(str(model))
            if minimum is not None and int(card.get("break_level",1)) < minimum:
                allowed = False
        if not allowed:
            raise SkinRejected("此皮肤尚未拥有、尚未解锁，或不属于当前角色形态。")
    meta = catalog()["base_characters"].get(str(resolved),{})
    if flag == 2 and not meta.get("has_l2d"):
        if enforce_l2d:
            raise SkinRejected("此皮肤没有动态立绘。")
        flag = 1
    return model,flag

def select(state,fields):
    card = find_card(state,fields.get("cid"))
    primary, alternate = forms(state,card)
    # Validate both choices before mutating either field.
    normal = validate_model(state,card,fields.get("skin",0),fields.get("skinIsl2d",1),
                            primary,owned_required=True)
    # The alternate slot mirrors the primary UI switch, so an absent/alternate
    # model without l2dName must be normalized rather than refused. Its value is
    # the client's paired display model, so it is not purchase-gated either; the
    # candidate family still rejects cross-family models.
    changed = validate_model(state,card,fields.get("skin_a",0),fields.get("skinIsl2d_a",1),
                             alternate,enforce_l2d=False,owned_required=False)
    card.update(skin=normal[0],skinIsl2d=normal[1],skin_a=changed[0],skinIsl2d_a=changed[1])
    return [Reply("PlayerProto:CardUpdate",{"cards":[deepcopy(card)],"store_exp":int(state.get("store_exp",0))})]

def expire(state):
    expired = []
    stamp = now(state)
    for row in state.get("skins",[]):
        deadlines = {v["id"]:v["t"] for v in row.get("ltSkins",[]) if v.get("t")}
        removed = {model for model,deadline in deadlines.items() if deadline <= stamp}
        expired.extend(removed)
        row["ltSkins"] = [p for p in row.get("ltSkins",[]) if p["id"] not in removed]
    cards = []
    for card in state["cards"]:
        changed = False
        for field,flag in (("skin","skinIsl2d"),("skin_a","skinIsl2d_a")):
            if card.get(field) in expired:
                card[field],card[flag],changed = 0,1,True
        if changed:
            cards.append(deepcopy(card))
    replies = [Reply("PlayerProto:CardUpdate",{"cards":cards,"store_exp":int(state.get("store_exp",0))})] if cards else []
    replies.append(Reply("PlayerProto:SkinExpiredRet",{"ids":sorted(set(expired))}))
    return replies
