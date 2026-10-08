"""Client-visible outcomes for rejected, unsupported and failing requests.

Only protocol-level corruption may end a session. Everything else answers with
SystemProto:Tips so the client keeps a usable connection. This module never
imports server_core: server_core imports it.
"""
from __future__ import annotations
import traceback

from database import StorageError

TIPS_MESSAGE = 'SystemProto:Tips'
# cfgCfgTipsSimpleChinese.lua:249, nShowType=1 -> a plain screen tip.
TIPS_STR_ID = 'GeneralTips'
# TipsMgr.lua:4-7 disconnects the client for these keys; never answer with them.
DISCONNECTING_KEYS = frozenset({'accExist', 'accNotExist', 'accLenErr', 'pwdLenErr', 'sqlFail',
                                'pwdErr', 'relogin', 'svrBusy', 'loadDataErr'})
UNSUPPORTED_TEXT = '本地服务尚未实现该功能：{name}。本次操作没有生效。'
FAILURE_TEXT = '本地服务处理该请求时出错，操作未生效：{name}。'
BUSY_TEXT = '本地存档正忙，请稍后重试。'
CLOSE = 'close'
CONTINUE = 'continue'

class AuthRequired(StorageError):
    """Not authenticated, invalid ticket, or wrong endpoint role: close the session.

    Subclasses StorageError so existing handlers and tests keep their contract,
    while classify() maps it to a close instead of a client tip.
    """

class ProtocolFatal(Exception):
    """Frame or encoding level corruption: the connection cannot continue safely."""

    def __init__(self, message, reason='protocol_fatal'):
        super().__init__(message)
        self.close_reason = reason

class UnknownRequest(LookupError):
    """The schema knows this request, but no local handler is registered for it."""

    def __init__(self, name, opcode=None):
        super().__init__(name)
        self.name = name
        self.opcode = opcode

def named(error, class_name):
    """True when error or one of its bases carries class_name, without importing it."""
    return any(base.__name__ == class_name for base in type(error).__mro__)

def retryable(error):
    """True for storage failures that are safe to retry, marked by their class."""
    return bool(getattr(type(error), 'retryable', False))

def client_message(error, name=''):
    """Return the text shown in the client tip; never leak an internal traceback."""
    if named(error, 'UnknownRequest'):
        return UNSUPPORTED_TEXT.format(name=name or getattr(error, 'name', ''))
    if retryable(error):
        return BUSY_TEXT
    if named(error, 'StorageError'):
        text = str(error).strip()
        if text:
            return text
    return FAILURE_TEXT.format(name=name)

def tips_fields(request_name, opcode, text):
    """Fields for SystemProto:Tips, matching handlers/mail.py:22 and equip_service.py:167."""
    return (TIPS_MESSAGE, {'strId': TIPS_STR_ID, 'opId': int(opcode or 0), 'opName': request_name,
                           'args': [{'type': 0, 'param': text}]})

def classify(error, frames_sent=False):
    """Return (disposition, reason); disposition is CLOSE or CONTINUE."""
    if frames_sent:
        # Part of a multi-frame reply is already on the wire.
        return CLOSE, 'partial_write'
    if isinstance(error, AuthRequired):
        return CLOSE, 'auth_required'
    if named(error, 'CodecError'):
        return CLOSE, 'protocol_fatal'
    if isinstance(error, ProtocolFatal):
        return CLOSE, error.close_reason
    if isinstance(error, (TimeoutError, ConnectionError)):
        return CLOSE, 'transport'
    if named(error, 'UnknownRequest'):
        return CONTINUE, 'unsupported_request'
    if retryable(error):
        return CONTINUE, 'storage_busy'
    if named(error, 'StorageError'):
        return CONTINUE, 'business_rejected'
    return CONTINUE, 'unexpected_failure'

def describe(error, disposition, reason, *, role=None, uid=None, connection_id=None,
             request_id=None, name=None, opcode=None, stage='dispatch'):
    """Event fields for one failed request; tracebacks only for unexpected defects."""
    event = {'event': 'request_failed', 'disposition': disposition, 'reason': reason, 'stage': stage,
             'role': role, 'uid': uid, 'connection_id': connection_id, 'request_id': request_id,
             'name': name, 'opcode': opcode, 'error': type(error).__name__}
    if reason == 'unexpected_failure':
        # format_exception lists source lines and the message, never locals.
        event['traceback'] = ''.join(traceback.format_exception(type(error), error,
                                                               error.__traceback__))[-4000:]
    else:
        event['detail'] = str(error)[:300]
    diagnostic = getattr(error, 'diagnostic', None)
    if isinstance(diagnostic, dict) and diagnostic:
        # Handlers attach structured, non-secret context for rejections whose client-facing
        # tip has to stay generic; sorting keeps the event byte-stable for log diffs.
        event['diagnostic'] = {str(key): diagnostic[key] for key in sorted(diagnostic)}
    return event
