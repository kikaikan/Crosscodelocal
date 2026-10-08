"""Offline asyncio TCP dispatcher. No outgoing requests or official fallback."""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import importlib
import ipaddress
import json
from pathlib import Path
import struct
import sys
import threading
import time
from functools import wraps
from access_policy import OPERATION_GATES, require_feature
from error_policy import (CLOSE, TIPS_MESSAGE, AuthRequired, UnknownRequest, classify,
                          client_message, describe, tips_fields)

sys.dont_write_bytecode = True
# Frozen onefile builds unpack into a temporary directory, so __file__ no longer
# describes where the user keeps data tables and protocol definitions. In that
# mode the application directory is the folder holding the executable; source
# runs keep the original repository-relative behaviour.
FROZEN = bool(getattr(sys,'frozen',False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ROOT = APP_DIR if FROZEN else Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'02-tools'/'scripts'))
from config_codec import app_path
from protocol_codec import CodecError, IVProtoCodec, WireConfig, encode_packet, readable
from database import Store, StorageError
import collection_unlock
import reply_chunks
HANDLERS = {}
# start_local.ps1 passes this same list explicitly. Keeping it here means a frozen
# executable (and any bare `python server_core.py`) registers the same business
# modules without repeating the list on every command line.
DEFAULT_HANDLERS = ('handlers.player_state','handlers.initialization','handlers.gacha','handlers.item_pool',
    'handlers.sub_talent','handlers.sign_in','handlers.cards_items','handlers.item_exchange','handlers.battle',
    'handlers.tasks','handlers.shop','handlers.mail','handlers.download_reward','handlers.gifts','handlers.skins',
    'handlers.local_gaps','handlers.equipment','handlers.building','handlers.dorm','handlers.ability','handlers.panels')

class UnsupportedOperation(ValueError):
    pass

def register(name):
    def decorator(handler):
        if name in HANDLERS:
            raise ValueError('Duplicate handler: '+name)
        if name in OPERATION_GATES:
            original = handler
            @wraps(original)
            async def handler(ctx, fields):
                uid = ctx.require_login()
                require_feature(ctx.store.get_player(uid), OPERATION_GATES[name])
                return await original(ctx, fields)
        HANDLERS[name] = handler
        return handler
    return decorator

@dataclass
class Reply:
    name: str
    fields: dict

@dataclass
class Context:
    server: 'LocalServer'
    role: str
    uid: int | None = None
    logged_in: bool = False
    initialized: bool = False
    connection_id: str | None = None
    @property
    def store(self):
        return self.server.store
    def require_login(self):
        # Session level, not business level: an unauthenticated request closes the socket.
        if not self.logged_in or self.uid is None:
            raise AuthRequired('Local login required')
        return self.uid

class FrameBuffer:
    def __init__(self,maximum=32768):
        self.buffer,self.maximum = bytearray(),maximum
    def feed(self,block):
        self.buffer.extend(block)
        frames = []
        while len(self.buffer)>=2:
            size = struct.unpack_from('>H',self.buffer)[0]
            if size<6 or size>self.maximum:
                raise CodecError('Client outer length out of range')
            if len(self.buffer)<size+2:
                break
            if self.buffer[2]!=1:
                raise CodecError('Unobserved client flag')
            frames.append(bytes(self.buffer[3:size+2]))
            del self.buffer[:size+2]
            if len(frames)>256:
                raise CodecError('Too many coalesced packets')
        if len(self.buffer)>self.maximum+2:
            raise CodecError('Pending frame limit exceeded')
        return frames

@register('ClientProto:QueryAccount')
async def query_account(ctx,fields):
    if ctx.role!='query':
        raise AuthRequired('QueryAccount requires query endpoint')
    account = ctx.store.create_account(str(fields.get('account','')),ctx.server.seed)
    ctx.uid = account['uid']
    # Incoming pwd is never persisted, forwarded or logged.
    return [Reply('LoginProto:QueryAccount',{'uid':ctx.uid,'svr_version':str(fields.get('SvnVersion','')),
                  'anti_addiction':{},'is_anti_addiction':0})]

@register('ClientProto:PreLoginGame')
async def pre_login(ctx,fields):
    uid = int(fields.get('uid',0))
    if ctx.role!='query' or ctx.uid!=uid:
        raise AuthRequired('Query this local account first')
    return [Reply('LoginProto:PreLoginGame',{'key':ctx.store.issue_ticket(uid),
                  'ip':ctx.server.client_host,'port':ctx.server.game_port,'is_ok':True})]

@register('ClientProto:LoginGame')
async def login_game(ctx,fields):
    uid = int(fields.get('uid',0))
    if ctx.role!='game' or not ctx.store.validate_ticket(uid,fields.get('key')):
        raise AuthRequired('Valid locally issued ticket required')
    ctx.uid,ctx.logged_in = uid,True
    with ctx.store.transaction(uid) as tx:
        tx.state['player']['currtime'] = int(time.time())
        data = deepcopy(tx.state['login'])
        data['infos'] = deepcopy(tx.state['player'])
    replies = []
    if 'offline_unlock_all' in tx.state:
        from admin_control import access_reply
        replies.append(access_reply(tx.state))
    download = sys.modules.get('handlers.download_reward')
    if download is not None and 'offline_unlock_all' in tx.state:
        replies.extend(download.initial_pushes(tx.state))
    return replies+[Reply('LoginProto:LoginGame',data)]

@register('ClientProto:Heartbeat')
async def heartbeat(ctx,fields):
    replies = []
    if ctx.logged_in and ctx.uid is not None:
        from admin_control import consume_notifications, refresh_daily_gifts
        if 'handlers.gifts' in sys.modules:
            refresh_daily_gifts(ctx.store, ctx.uid, ctx.server.codec)
        replies = consume_notifications(ctx.store, ctx.uid, ctx.server.codec)
    return replies+[Reply('LoginProto:Heartbeat',{})]

@register('PlayerProto:GetLifeBuff')
async def life_buffs(ctx,fields):
    ctx.require_login()
    return [Reply('PlayerProto:GetLifeBuffRet',{'buffs':ctx.store.get_player(ctx.uid).get('life_buffs',{})})]

@register('EquipProto:GetEquips')
async def get_equips(ctx,fields):
    ctx.require_login()
    state = ctx.store.get_player(ctx.uid)
    equips = state.get('equips',[])
    return [Reply('EquipProto:GetEquipsRet',{'equips':equips,'cur_size':len(equips),
                  'max_size':int(state['max_equip_size']),'materialNum':0})]

@register('PlayerProto:CardsData')
async def cards_data(ctx,fields):
    ctx.require_login()
    state = ctx.store.get_player(ctx.uid)
    return [Reply('PlayerProto:CardsDataRet',{'store_exp':int(state.get('store_exp',0)),'rename_records':{}}),
            *reply_chunks.card_add(ctx.server.codec,state['cards'],len(state['cards']),state['max_card_size'])]

@register('PlayerProto:GetCardRole')
async def card_roles(ctx,fields):
    ctx.require_login()
    from card_roles_service import repaired_state
    roles = repaired_state(ctx.store, ctx.uid).get('card_roles',[])
    return reply_chunks.update_card_role(ctx.server.codec,roles)

def initial_pushes(state,codec):
    # Whole-snapshot replies are chunked by measured bytes: an oversized frame
    # would otherwise close the connection and block every later login.
    items = [{'id':int(k),'num':int(v),'time':0,'ix':0,'expiry':0,'get_infos':{}}
             for k,v in state['inventory'].items() if int(v)>0]
    replies = [Reply('PlayerProto:CardsDataRet',{'store_exp':int(state.get('store_exp',0)),'rename_records':{}}),
            *reply_chunks.card_add(codec,state['cards'],len(state['cards']),state['max_card_size']),
            *reply_chunks.add_card_role(codec,state.get('card_roles',[])),
            *reply_chunks.item_bag(codec,items),
            Reply('PlayerProto:TeamData',{'data':state['teams'],'count':len(state['teams']),'isFinish':True}),
            Reply('PlayerProto:DuplicateData',{'mainLine':state['progress'].get('mainLine',[]),'is_finish':True})]
    tactical = sys.modules.get('handlers.battle_tactical')
    if tactical is not None:
        replies.extend(tactical.initial_pushes(state))
    return replies

@register('ClientProto:InitFinish')
async def init_finish(ctx,fields):
    ctx.require_login()
    pushes = []
    if not ctx.initialized:
        from card_roles_service import repaired_state
        state = repaired_state(ctx.store, ctx.uid)
        pushes = initial_pushes(state,ctx.server.codec)
        ctx.initialized = True
    return pushes+[Reply('ClientProto:InitFinishRet',{'is_reconnect':bool(fields.get('is_reconnect',False))})]

class LocalServer:
    def __init__(self,codec,store,seed,client_host='10.0.2.2',query_port=19001,game_port=19041,
                 log_path=None,idle_timeout=90,assembly_timeout=15,max_connections=32):
        self.codec,self.store,self.seed = codec,store,seed
        destination = ipaddress.ip_address(client_host)
        if destination.version!=4 or destination.is_multicast or destination.is_unspecified or not (destination.is_private or destination.is_loopback):
            raise StorageError('Advertised game endpoint must be a local/private IPv4 address')
        self.client_host,self.query_port,self.game_port = client_host,query_port,game_port
        self.log_path = Path(log_path) if log_path else None
        self.idle_timeout,self.assembly_timeout = idle_timeout,assembly_timeout
        self.max_connections,self.connections = max_connections,0
        self.connections_seen = 0
        self.listeners,self.writers,self.tasks = [],set(),set()
        self._log_handle = None
    def event(self,kind,**fields):
        line = json.dumps({'time_ms':time.time_ns()//1_000_000,'event':kind,**fields},ensure_ascii=False,separators=(',',':'))
        if self.log_path:
            if self._log_handle is None:
                self.log_path.parent.mkdir(parents=True,exist_ok=True)
                self._log_handle = self.log_path.open('a',encoding='utf-8')
            self._log_handle.write(line+'\n')
            self._log_handle.flush()
        else:
            print(line,flush=True)
    async def dispatch(self,ctx,frame):
        handler = HANDLERS.get(frame.name)
        if handler is None:
            raise UnknownRequest(frame.name,frame.opcode)
        result = await handler(ctx,readable(frame.fields))
        if not isinstance(result,list) or any(not isinstance(r,Reply) for r in result):
            raise TypeError('Handler must return list[Reply]')
        return result
    async def handle_connection(self,reader,writer,role):
        task=asyncio.current_task()
        self.tasks.add(task)
        self.connections_seen+=1
        connection_id=role+'-'+str(self.connections_seen)
        if self.connections>=self.max_connections:
            self.event('connection_limit_rejected',role=role,connection_id=connection_id,
                       limit=self.max_connections)
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)
            return
        self.connections+=1
        self.writers.add(writer)
        ctx,frames = Context(self,role),FrameBuffer(self.codec.config.max_frame_size+1)
        ctx.connection_id = connection_id
        partial_since = None
        request_id = 0
        close_reason = None
        self.event('connect',role=role,connection_id=connection_id)
        try:
            while True:
                timeout = self.idle_timeout
                if partial_since is not None:
                    remaining = self.assembly_timeout-(time.monotonic()-partial_since)
                    if remaining<=0:
                        close_reason = 'assembly_timeout'
                        raise TimeoutError('Partial assembly timeout')
                    timeout = min(timeout,remaining)
                try:
                    block = await asyncio.wait_for(reader.read(8192),timeout)
                except TimeoutError:
                    close_reason = close_reason or ('assembly_timeout' if timeout<self.idle_timeout else 'idle_timeout')
                    raise
                if not block:
                    if frames.buffer:
                        close_reason = 'incomplete_packet'
                        raise CodecError('Client closed with incomplete packet')
                    break
                payloads = frames.feed(block)
                partial_since = (partial_since or time.monotonic()) if frames.buffer else None
                for payload in payloads:
                    requests,tail = self.codec.decode_stream(payload)
                    if tail or not requests or len(requests)>128:
                        close_reason = 'malformed_inner_frame'
                        raise CodecError('Malformed inner frame sequence')
                    for request in requests:
                        request_id+=1
                        self.event('request',role=role,uid=ctx.uid,opcode=request.opcode,name=request.name,
                                   connection_id=connection_id,request_id=request_id)
                        try:
                            replies = await self.dispatch(ctx,request)
                        except Exception as error:
                            disposition,reason = classify(error)
                            failure = describe(error,disposition,reason,role=role,uid=ctx.uid,
                                               connection_id=connection_id,request_id=request_id,
                                               name=request.name,opcode=request.opcode)
                            self.event(failure.pop('event'),**failure)
                            if disposition==CLOSE:
                                close_reason = reason
                                raise
                            # Unsupported, rejected and failing requests keep the session usable.
                            _,tip = tips_fields(request.name,request.opcode,client_message(error,request.name))
                            replies = [Reply(TIPS_MESSAGE,tip)]
                        # Encode every frame before writing any of them: an unencodable
                        # reply must not leave half a reply set on the wire.
                        try:
                            outbound = [self.codec.encode_frame(reply.name,reply.fields) for reply in replies]
                        except CodecError as error:
                            close_reason = 'frame_encode_failed'
                            self.event('frame_encode_failed',role=role,uid=ctx.uid,
                                       connection_id=connection_id,request_id=request_id,
                                       name=request.name,error=str(error),
                                       replies=[reply.name for reply in replies])
                            raise
                        for index,inner in enumerate(outbound):
                            writer.write(encode_packet(inner,flag=3,direction='s2c',timestamp=time.time_ns()//1_000_000))
                            self.event('response',role=role,uid=ctx.uid,name=replies[index].name,bytes=len(inner),
                                       connection_id=connection_id,request_id=request_id)
                        await writer.drain()
        except Exception as error:
            if close_reason is None:
                close_reason = classify(error)[1]
            self.event('connection_closed',role=role,uid=ctx.uid,connection_id=connection_id,
                       error=type(error).__name__,close_reason=close_reason)
        finally:
            self.writers.discard(writer)
            self.tasks.discard(task)
            self.connections-=1
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.event('disconnect',role=role,uid=ctx.uid,connection_id=connection_id,
                       close_reason=close_reason)
    async def start(self,bind='127.0.0.1'):
        try:
            for role,port in [('query',self.query_port),('game',self.game_port)]:
                listener = await asyncio.start_server(lambda r,w,role=role:self.handle_connection(r,w,role),bind,port)
                self.listeners.append(listener)
            self.query_port = self.listeners[0].sockets[0].getsockname()[1]
            self.game_port = self.listeners[1].sockets[0].getsockname()[1]
        except BaseException:
            await self.close()
            raise
        self.event('started',bind=bind,query_port=self.query_port,game_port=self.game_port,handlers=sorted(HANDLERS),offline_only=True)
    async def close(self):
        for listener in self.listeners:
            listener.close()
        await asyncio.gather(*(listener.wait_closed() for listener in self.listeners))
        self.listeners.clear()
        for writer in list(self.writers):
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in list(self.writers)),return_exceptions=True)
        pending=[task for task in self.tasks if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending,return_exceptions=True)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

def load_dependencies(schema_path,seed_path):
    if not schema_path.is_file() or not seed_path.is_file():
        raise StorageError('Missing schema/new-account seed; no remote fallback exists')
    schema = json.loads(schema_path.read_text(encoding='utf-8'))
    seed = json.loads(seed_path.read_text(encoding='utf-8'))
    Store.validate_seed(seed)
    # 新账号模板自带档案回忆/插画与心间私语的开启项（本地策略，见 collection_unlock）。
    # 放在冒烟编码之前，帧上限检查因此覆盖真实背包。
    collection_unlock.apply(seed)
    codec = IVProtoCodec(schema,WireConfig('little'))
    for name in HANDLERS:
        if name not in codec.schemas:
            raise CodecError('Required message absent from schema: '+name)
    codec.encode_frame('LoginProto:LoginGame',{**seed['login'],'infos':seed['player']})
    for reply in initial_pushes(seed,codec):
        codec.encode_frame(reply.name,reply.fields)
    return codec,seed

async def run(args):
    # --handler is additive only when the caller names modules; with none named the
    # packaged defaults apply. This keeps `--handler handlers.gacha` a true subset
    # while a bare run (and the frozen exe) still registers every business module.
    for module in dict.fromkeys(args.handler or DEFAULT_HANDLERS):
        importlib.import_module(module)
    codec,seed = load_dependencies(args.schema,args.seed)
    # Content access is independent of cleared stages, rewards or ownership.
    from access_policy import configure, migrate
    configure(seed, {'pools': not args.progression_gates and not args.restrict_pools,
                     'activities': not args.progression_gates and not args.restrict_activities,
                     'illustrations': not args.progression_gates and not args.restrict_illustrations})
    store = Store(args.database)
    migrate(store)
    collection_unlock.migrate(store)
    server = LocalServer(codec,store,seed,args.client_host,args.query_port,args.game_port,args.log)
    control = None
    try:
        await server.start(args.bind)
        if args.control_port:
            # The HTTP thread shares this process, so one executable covers the
            # resource/control page together with both TCP game endpoints.
            import control_http
            control = control_http.build_server(args.control_port,args.bind,APP_DIR,args.static_dir)
            threading.Thread(target=control.serve_forever,name='control-http',daemon=True).start()
            server.event('control_started',bind=args.bind,port=control.server_port,
                         static_dir=str(args.static_dir),frozen=FROZEN)
        await asyncio.Event().wait()
    finally:
        if control is not None:
            control.shutdown()
            control.server_close()
        await server.close()
        store.close()

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    # Frozen builds keep data tables, the database and logs next to the exe; a
    # source checkout keeps them next to this file inside 07-server.
    here = app_path('data')
    ap.add_argument('--bind',default='127.0.0.1')
    ap.add_argument('--client-host',default='10.0.2.2')
    ap.add_argument('--query-port',type=int,default=19001)
    ap.add_argument('--game-port',type=int,default=19041)
    ap.add_argument('--control-port',type=int,default=18080 if FROZEN else 0,
                    help='HTTP resource/control page port; 0 disables it (source default)')
    ap.add_argument('--static-dir',type=Path,default=APP_DIR/'local-static',
                    help='Optional plain-file resource root served under /cross/release/...')
    ap.add_argument('--schema',type=Path,default=ROOT/'05-protocol'/'endpoints.json')
    ap.add_argument('--seed',type=Path,default=here/'new_account_seed.json')
    ap.add_argument('--database',type=Path,default=here/'players.sqlite3')
    ap.add_argument('--log',type=Path,default=app_path('logs')/'server.jsonl')
    ap.add_argument('--handler',action='append',default=[],
                    help='Plugin module, e.g. handlers.gacha; repeatable. '
                         'Omit to load all default business modules')
    # Packaging self-check: import a caller-supplied module list and report the
    # result as JSON, so a build can prove dynamic imports survived freezing.
    ap.add_argument('--check-imports',metavar='MODULES',default=None,nargs='?',const='-',
                    help='Import a JSON module list (or stdin when omitted) and report failures')
    ap.add_argument('--progression-gates', action='store_true', help='New accounts retain original progression access gates')
    ap.add_argument('--restrict-pools', action='store_true', help='Keep source pool gates for new accounts')
    ap.add_argument('--restrict-activities', action='store_true', help='Keep source activity gates for new accounts')
    ap.add_argument('--restrict-illustrations', action='store_true', help='Keep source illustration sale dates for new accounts')
    args = ap.parse_args()
    if args.check_imports is not None:
        names = json.loads(sys.stdin.read() if args.check_imports=='-' else args.check_imports)
        failures = []
        for name in names:
            try:
                importlib.import_module(name)
            except Exception as error:
                failures.append(name+' -> '+type(error).__name__+': '+str(error))
        print(json.dumps({'missing':failures,'checked':len(names)}),flush=True)
        sys.exit(1 if failures else 0)
    if any(not 1<=p<=32767 for p in [args.query_port,args.game_port]) or args.query_port==args.game_port:
        ap.error('Distinct query/game ports must fit protocol signed short: 1..32767')
    sys.modules.setdefault('server_core',sys.modules[__name__])
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
if __name__=='__main__':
    main()
