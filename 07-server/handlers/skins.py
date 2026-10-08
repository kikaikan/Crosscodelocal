"""Authenticated original skin protocols backed by durable local ownership."""
from database import StorageError
from server_core import register, Reply
import skins_service as skins

def uid(ctx):
    value = ctx.require_login()
    if ctx.role != "game":
        raise StorageError("Skin requests require authenticated local game connection")
    return value

@register("PlayerProto:GetSkins")
async def get_skins(ctx,fields):
    state = ctx.store.get_player(uid(ctx))
    return skins.checked(ctx,[Reply("PlayerProto:GetSkinsRet",
                                    {"info":skins.render(state,fields.get("cfgid",0))})])

@register("PlayerProto:UseSkin")
async def use_skin(ctx,fields):
    value=uid(ctx)
    try:
        with ctx.store.transaction(value) as tx:
            replies=skins.checked(ctx,skins.select(tx.state,fields))
        return replies
    except skins.SkinRejected as error:
        return skins.refusal(ctx,error)

@register("PlayerProto:SkinExpired")
async def skin_expired(ctx,fields):
    if fields:
        raise StorageError("SkinExpired has an empty source wire body")
    with ctx.store.transaction(uid(ctx)) as tx:
        replies = skins.checked(ctx,skins.expire(tx.state))
    return replies
