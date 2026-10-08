"""Member-gift read refreshes eligible source daily mail, then exact 1029 state."""
from server_core import register
from gift_service import refresh_gifts, member_pushes


class _Unchanged(Exception):
    """Rollback an unchanged read without incrementing the account revision."""


@register('ClientProto:GetMemberRewardInfo')
async def member_info(ctx, fields):
    from mail_service import render_mail_pushes
    uid = ctx.require_login()
    try:
        with ctx.store.transaction(uid) as tx:
            refreshed = refresh_gifts(tx)
            replies = member_pushes(tx.state)
            if not refreshed['changed']:
                raise _Unchanged()
            replies += render_mail_pushes(tx.state, refreshed['mail_ids'])
    except _Unchanged:
        pass
    return replies
