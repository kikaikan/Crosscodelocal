"""Source-backed chip equipment and strengthening transactions; no remote calls."""
from copy import deepcopy
from functools import lru_cache
import re
import json
from pathlib import Path

from database import StorageError
from server_core import Reply
from seed_generator import LUA_DIR, python_data
from config_codec import app_path, parse_lua_table
from handlers.tasks import normalize_long_strings, table_end

CONFIG_FILES = frozenset((
    'cfgCfgEquip.lua','cfgCfgEquipExp.lua','cfgCfgMaterialEquip.lua',
    'cfgCfgEquipExpRand.lua','cfgCfgEquipSkill.lua','cfgCfgCardPropertyEnum.lua',
    'cfgLifeBuffer.lua','cfgCfgLifeBuffer.lua','cfgCardData.lua','cfgCardLevel.lua',
    'cfgCardBreak.lua','cfgCardIntensify.lua','cfgCfgCardIntensify.lua',
    'cfgskill.lua','cfgCfgMainTalentSkillUpgrade.lua',
))

class EquipmentRejected(Exception):
    """An authenticated, recoverable business failure, never an empty success."""


def integer(value, label, minimum=1, maximum=2147483647):
    if isinstance(value,bool) or not isinstance(value,int) or not minimum <= value <= maximum:
        raise EquipmentRejected(label+'超出有效范围')
    return value


@lru_cache(maxsize=16)
def config_source(filename):
    if filename not in CONFIG_FILES:
        raise StorageError('Unsupported equipment source table')
    try:
        source = normalize_long_strings((LUA_DIR/filename).read_text('utf-8-sig'))
    except (OSError,ValueError) as error:
        raise StorageError('Required local equipment source unavailable: '+filename) from error
    if len(source)>8_000_000:
        raise StorageError('Oversized equipment configuration')
    match=re.search(r'_G\[[^\]]+\]\s*=\s*\{',source)
    if match is None:
        raise StorageError('Missing equipment configuration table: '+filename)
    return source,match.end()-1


@lru_cache(maxsize=16)
def array_records(filename):
    source,start=config_source(filename)
    try:
        rows=python_data(parse_lua_table(source[start:table_end(source,start)]))
    except ValueError as error:
        raise StorageError('Malformed local equipment configuration: '+filename) from error
    if not isinstance(rows,list):
        raise StorageError('Expected array equipment table: '+filename)
    return {int(r['id']):r for r in rows}


@lru_cache(maxsize=4096)
def config_record(filename,key):
    source,start=config_source(filename)
    tail=source[start+1:].lstrip()
    if tail.startswith('{'):
        row=array_records(filename).get(int(key))
        if row is None:
            raise StorageError('Missing equipment configuration: '+filename+':'+str(key))
        return row
    match=re.search(r'\['+re.escape(str(int(key)))+r'\]\s*=\s*\{',source[start+1:])
    if match is None:
        raise StorageError('Missing equipment configuration: '+filename+':'+str(key))
    offset=start+1+match.end()-1
    try:
        row=python_data(parse_lua_table(source[offset:table_end(source,offset)]))
    except ValueError as error:
        raise StorageError('Malformed equipment record: '+filename+':'+str(key)) from error
    if not isinstance(row,dict) or row.get('id')!=int(key):
        raise StorageError('Equipment configuration ID mismatch')
    return row


def equip_config(equip):
    return config_record('cfgCfgEquip.lua',equip['cfgid'])


def validated_equips(state):
    rows=state.get('equips',[])
    if not isinstance(rows,list):
        raise StorageError('Persisted chip collection must be a list')
    ids=set()
    cards={int(card['cid']) for card in state['cards']}
    slots=set()
    for equip in rows:
        if not isinstance(equip,dict):
            raise StorageError('Malformed persisted chip')
        sid=integer(equip.get('sid'),'芯片编号')
        if sid in ids:
            raise StorageError('Duplicate persisted chip instance')
        ids.add(sid)
        cfg=equip_config(equip)
        integer(equip.get('level',0),'芯片等级',0,int(cfg.get('nMaxLvl',1)))
        integer(equip.get('exp',0),'芯片经验',0,4294967295)
        integer(equip.get('num',1),'芯片数量')
        owner=integer(equip.get('card_id',0),'所属角色',0)
        if owner and owner not in cards:
            raise StorageError('Persisted chip owner is not an owned card')
        if cfg.get('nType',1)==1:
            slot=integer(cfg.get('nSlot',1),'芯片槽位',1,5)
            if owner and (owner,slot) in slots:
                raise StorageError('Multiple persisted chips occupy the same card slot')
            slots.add((owner,slot)) if owner else None
        elif owner:
            raise StorageError('Material chip cannot be equipped')
        skills=equip.get('skills',[])
        if not isinstance(skills,list) or len(skills)>4:
            raise StorageError('Malformed persisted chip skills')
        for skill in skills:
            config_record('cfgCfgEquipSkill.lua',integer(skill,'芯片词条'))
    return rows


def card_equipments(state,card):
    # The durable sEquip.card_id is authoritative; both card views are refreshed
    # after every operation. Empty card.equips is truthy in Lua and would hide
    # equip_ids, so complete snapshots are required, not only a slot mapping.
    rows=[e for e in validated_equips(state) if e.get('card_id',0)==card['cid']]
    return sorted(rows,key=lambda e:equip_config(e).get('nSlot',1))


def owned_equip(state,sid):
    sid=integer(sid,'芯片编号')
    equip=next((e for e in validated_equips(state) if e['sid']==sid),None)
    if equip is None:
        raise EquipmentRejected('未持有这个芯片，或它已被消耗')
    return equip


def owned_card(state,cid):
    cid=integer(cid,'角色编号')
    card=next((c for c in state['cards'] if c['cid']==cid),None)
    if card is None:
        raise EquipmentRejected('未持有这个角色')
    return card


def ids(value,label,maximum=500):
    if not isinstance(value,list) or not 1 <= len(value) <= maximum:
        raise EquipmentRejected(label+'数量不正确')
    result=[integer(v,label) for v in value]
    if len(set(result))!=len(result):
        raise EquipmentRejected(label+'不能重复')
    return result


def size_fields(state):
    return {'cur_size':len(state.get('equips',[])),
            'max_size':int(state.get('max_equip_size',500))}


def checked(ctx,replies):
    for reply in replies:
        ctx.server.codec.encode_frame(reply.name,reply.fields)
    return replies


def refusal(error,op_name,op_id):
    return [Reply('SystemProto:Tips',{'strId':'GeneralTips','opId':op_id,'opName':op_name,
                  'args':[{'type':0,'param':str(error)}]})]


def wire_card(card):
    result=deepcopy(card)
    result['equip_ids']={int(slot):sid for slot,sid in (card.get('equip_ids') or {}).items()}
    return result


def sync_cards(state,cids):
    result=[]
    for cid in sorted(cids):
        if not cid:
            continue
        card=owned_card(state,cid)
        equips=card_equipments(state,card)
        card['equip_ids']={str(equip_config(e)['nSlot']):e['sid'] for e in equips}
        card['equips']=deepcopy(equips)
        # The complete calculator is shared with battle and card progression.
        # Import lazily to avoid the equipment_stats -> equip_service cycle.
        try:
            from equipment_stats import refresh_card_hp
        except ImportError as error:
            raise StorageError('Complete local chip stat calculator is required') from error
        refresh_card_hp(state,card)
        result.append(wire_card(card))
    return ([Reply('PlayerProto:CardUpdate',{'cards':result,
             'store_exp':int(state.get('store_exp',0))})] if result else [])


def get_equips(ctx):
    ctx.require_login()
    state=ctx.store.get_player(ctx.uid)
    equips=validated_equips(state)
    material_count=sum(int(e.get('num',1)) for e in equips if equip_config(e).get('nType',1)==2)
    # EquipAdd only loads data; GetEquipsRet emits Init_Equip_Finish. Keep that
    # completion callback last so large local inventories initialize in full.
    batches=[equips[n:n+200] for n in range(0,len(equips),200)] or [[]]
    replies=[Reply('EquipProto:EquipAdd',{'equips':deepcopy(batch),**size_fields(state),
                                        'is_finish':False}) for batch in batches[:-1]]
    replies.append(Reply('EquipProto:GetEquipsRet',{'equips':deepcopy(batches[-1]),
                    **size_fields(state),'materialNum':material_count}))
    return checked(ctx,replies)


def equip_up(state,fields,batch=False):
    card=owned_card(state,fields.get('target_card_id'))
    selected=ids(fields.get('equip_ids'),'芯片',5) if batch else [integer(fields.get('equip_id'),'芯片编号')]
    requested=[owned_equip(state,sid) for sid in selected]
    slots=[]
    for equip in requested:
        cfg=equip_config(equip)
        if cfg.get('nType',1)!=1:
            raise EquipmentRejected('强化素材不能装备到角色')
        slots.append(integer(cfg.get('nSlot',1),'芯片槽位',1,5))
    if len(set(slots))!=len(slots):
        raise EquipmentRejected('每个槽位只能装备一个芯片')
    affected={card['cid']}
    downs=[]
    touched={e['sid']:e for e in requested}
    for equip,slot in zip(requested,slots):
        previous=int(equip.get('card_id',0))
        if previous and previous!=card['cid']:
            affected.add(previous);downs.append(equip['sid'])
            equip['card_id']=0
        replacing=next((e for e in state['equips'] if e.get('card_id',0)==card['cid']
                        and equip_config(e).get('nSlot',1)==slot and e['sid']!=equip['sid']),None)
        if replacing is not None:
            replacing['card_id']=0;downs.append(replacing['sid'])
            touched[replacing['sid']]=replacing
        equip['card_id']=card['cid']
    # Down callbacks must see old owners before full bag/card snapshots replace
    # them. Then source Up/Ups callbacks use the new instance and target slots.
    result=[]
    if downs:
        result.append(Reply('EquipProto:EquipDownRet',{'equip_ids':list(dict.fromkeys(downs)),
                                                       'cur_size':len(state['equips'])}))
    result.append(Reply('EquipProto:EquipUpdate',{'equips':deepcopy(list(touched.values())),**size_fields(state)}))
    result.extend(sync_cards(state,affected))
    if batch:
        result.append(Reply('EquipProto:EquipUpsRet',{'target_card_id':card['cid'],
                             'up_ids':selected,'down_ids':[]}))
    else:
        result.append(Reply('EquipProto:EquipUpRet',{'equip_id':requested[0]['sid'],
                             'target_card_id':card['cid'],'target_slot':slots[0],
                             'cur_size':len(state['equips'])}))
    return result


def equip_down(state,fields):
    selected=ids(fields.get('equip_ids'),'芯片',5)
    rows=[owned_equip(state,sid) for sid in selected]
    affected={int(e.get('card_id',0)) for e in rows}
    for equip in rows:
        equip['card_id']=0
    return ([Reply('EquipProto:EquipDownRet',{'equip_ids':selected,'cur_size':len(state['equips'])}),
             Reply('EquipProto:EquipUpdate',{'equips':deepcopy(rows),**size_fields(state)})]
            +sync_cards(state,affected))


def experience_row(equip):
    cfg=equip_config(equip)
    if cfg.get('nType',1)==2:
        return config_record('cfgCfgMaterialEquip.lua',cfg.get('nQuality',1))
    quality=config_record('cfgCfgEquipExp.lua',cfg.get('nQuality',1))
    level=int(equip.get('level',0))
    rows=quality.get('tInfos',[])
    if not isinstance(rows,list) or level>=len(rows):
        raise StorageError('Missing source chip experience level')
    return rows[level]


def strengthen(tx,fields):
    state=tx.state
    target=owned_equip(state,fields.get('sid'))
    cfg=equip_config(target)
    if cfg.get('nType',1)!=1:
        raise EquipmentRejected('强化素材不能作为目标芯片')
    level=int(target.get('level',0));maximum=int(cfg.get('nMaxLvl',1))
    if level>=maximum:
        raise EquipmentRejected('芯片已达到最高等级')
    raw_ids=fields.get('equip_ids',[])
    selected=ids(raw_ids,'强化素材',10) if raw_ids else []
    raw_items=fields.get('items',[])
    if not isinstance(raw_items,list) or len(raw_items)>1:
        raise EquipmentRejected('芯片经验物品选择不正确')
    materials=[owned_equip(state,sid) for sid in selected]
    if len(selected)+len(raw_items)>10 or not selected and not raw_items:
        raise EquipmentRejected('请选择一至十项强化素材')
    exp=gold=0
    for material in materials:
        if material['sid']==target['sid']:
            raise EquipmentRejected('不能将目标芯片作为素材')
        if material.get('lock',0):
            raise EquipmentRejected('请先解锁作为素材的芯片')
        if material.get('card_id',0):
            raise EquipmentRejected('已装备的芯片不能作为素材')
        row=experience_row(material)
        exp+=integer(row.get('nMaterialExp'),'源素材经验')
        gold+=integer(row.get('nMaterialCost'),'源素材金币',0)
    item_amount=0
    if raw_items:
        item=raw_items[0]
        if not isinstance(item,dict) or item.get('id')!=10021 or item.get('type',2)!=2:
            raise EquipmentRejected('目前芯片强化只接受源芯片经验物品10021')
        item_amount=integer(item.get('num'),'芯片经验数量')
        exp+=item_amount
        # StuffArray.lua:41-42; GEnum ITEMS_ID.EQUIP_EXP=10021.
        settings=json.loads(app_path('data/progression-settings.json').read_text('utf-8'))
        price=settings.get('g_ClipExpPrcie',{})
        if price.get('type')!='int' or re.fullmatch(r'[0-9]+',str(price.get('value',''))) is None:
            raise StorageError('Invalid source chip experience price')
        gold+=item_amount*integer(int(price['value']),'源芯片经验单价',0)
        if tx.item_count(10021)<item_amount:
            raise EquipmentRejected('芯片经验不足')
    if gold>2147483647 or exp>4294967295:
        raise EquipmentRejected('强化消耗超出有效范围')
    if tx.item_count(10001)<gold:
        raise EquipmentRejected('金币不足')
    # Source weights currently give factor1 weight100 and all other factors0.
    active=[r for r in array_records('cfgCfgEquipExpRand.lua').values() if r.get('nWeight',0)>0]
    if len(active)!=1 or active[0].get('fRand')!=1:
        raise StorageError('Changed chip critical-experience policy is not yet implemented')
    multiplier_id=active[0]['id']
    experience=int(target.get('exp',0))+exp
    while level<maximum:
        trial=deepcopy(target);trial['level']=level
        needed=integer(experience_row(trial).get('nExp'),'源升级经验')
        if experience<needed:
            break
        experience-=needed;level+=1
    # Match EquipData.UpLevel exactly, including surplus from indivisible chips;
    # item slider in the client already limits optional item EXP to remaining cap.
    integer(experience,'强化后经验',0,4294967295)
    tx.add_item(10001,-gold)
    if item_amount:
        tx.add_item(10021,-item_amount)
    deleted=[];updated=[]
    for material in materials:
        if equip_config(material).get('nType',1)==2 and material.get('num',1)>1:
            material['num']-=1;updated.append(deepcopy(material))
        else:
            state['equips'].remove(material);deleted.append(material['sid'])
    target.update(level=level,exp=experience)
    from handlers.cards_items import resource_replies,task_updates
    result=resource_replies(state,{10001:-gold,10021:-item_amount})
    if deleted:
        result.append(Reply('EquipProto:EquipDelete',{'sids':deleted,**size_fields(state)}))
    if updated:
        result.append(Reply('EquipProto:EquipUpdate',{'equips':updated,**size_fields(state)}))
    if target.get('card_id',0):
        result.extend(sync_cards(state,{target['card_id']}))
    result.extend(task_updates(state,'equip_upgrade'))
    result.extend(task_updates(state,'equip_exp_spent',exp))
    result.extend(task_updates(state,'state_changed'))
    result.append(Reply('EquipProto:EquipUpgradeRet',{'equip':deepcopy(target),
                         'gold':tx.item_count(10001),'id':multiplier_id}))
    return result


def set_lock(state,fields):
    infos=fields.get('infos')
    if not isinstance(infos,list) or not 1<=len(infos)<=500:
        raise EquipmentRejected('锁定芯片列表不正确')
    checked_rows=[];seen=set()
    for info in infos:
        if not isinstance(info,dict):
            raise EquipmentRejected('锁定芯片参数不正确')
        equip=owned_equip(state,info.get('sid'))
        value=integer(info.get('lock'),'芯片锁定状态',0,1)
        if equip['sid'] in seen:
            raise EquipmentRejected('芯片编号不能重复')
        seen.add(equip['sid']);checked_rows.append((equip,value))
    for equip,value in checked_rows:
        equip['lock']=value
    return [Reply('EquipProto:EquipLockRet',{'infos':[{'sid':e['sid'],'lock':v} for e,v in checked_rows]})]


def set_new(state,fields):
    selected=ids(fields.get('sids'),'芯片',500)
    value=integer(fields.get('is_new'),'芯片新获取状态',0,1)
    rows=[owned_equip(state,sid) for sid in selected]
    for equip in rows:
        equip['is_new']=value
    return [Reply('EquipProto:SetIsNewRet',{'sids':selected,'is_new':value})]
