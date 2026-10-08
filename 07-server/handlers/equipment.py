"""Original chip write protocols with atomic storage and checked wire replies."""
from database import StorageError
from server_core import register
import equip_service as service


def uid(ctx):
    result=ctx.require_login()
    if ctx.role!='game':
        raise StorageError('Chip requests require local game authentication')
    return result


def transaction(ctx,fields,callback,op_name,op_id):
    owner=uid(ctx)
    try:
        with ctx.store.transaction(owner) as tx:
            replies=service.checked(ctx,callback(tx,fields))
        return replies
    except service.EquipmentRejected as error:
        return service.checked(ctx,service.refusal(error,op_name,op_id))


@register('EquipProto:EquipUp')
async def equip_up(ctx,fields):
    return transaction(ctx,fields,lambda tx,f:service.equip_up(tx.state,f),'EquipProto:EquipUp',2721)


@register('EquipProto:EquipUps')
async def equip_ups(ctx,fields):
    return transaction(ctx,fields,lambda tx,f:service.equip_up(tx.state,f,True),'EquipProto:EquipUps',2737)


@register('EquipProto:EquipDown')
async def equip_down(ctx,fields):
    return transaction(ctx,fields,lambda tx,f:service.equip_down(tx.state,f),'EquipProto:EquipDown',2742)


@register('EquipProto:EquipUpgrade')
async def upgrade(ctx,fields):
    return transaction(ctx,fields,service.strengthen,'EquipProto:EquipUpgrade',2712)


@register('EquipProto:EquipLock')
async def lock(ctx,fields):
    return transaction(ctx,fields,lambda tx,f:service.set_lock(tx.state,f),'EquipProto:EquipLock',2719)


@register('EquipProto:SetIsNew')
async def set_new(ctx,fields):
    return transaction(ctx,fields,lambda tx,f:service.set_new(tx.state,f),'EquipProto:SetIsNew',2727)
