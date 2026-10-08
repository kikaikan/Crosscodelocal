import asyncio
from copy import deepcopy
import json
from pathlib import Path
import secrets
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
SERVER=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SERVER))
from database import Store, StorageError
from server_core import ROOT, Context, FrameBuffer, HANDLERS, LocalServer, load_dependencies
from protocol_codec import CodecError, encode_packet, readable
import handlers.item_exchange

CODEC,SEED=load_dependencies(ROOT/'05-protocol'/'endpoints.json',SERVER/'data'/'new_account_seed.json')

def free_port():
    for _ in range(100):
        port=10000+secrets.randbelow(22000)
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1',port))
                return port
            except OSError:
                pass
    raise RuntimeError('No local test port available in protocol signed-short range')

class FramingTests(unittest.TestCase):
    def test_fragmented_and_coalesced_real_schema_frames(self):
        inner=CODEC.encode_frame('ClientProto:Heartbeat',{})
        outer=encode_packet(inner)
        parser=FrameBuffer()
        result=[]
        for byte in outer:
            result+=parser.feed(bytes([byte]))
        self.assertEqual(result,[inner])
        self.assertFalse(parser.buffer)
        self.assertEqual(parser.feed(outer*3),[inner]*3)

    def test_bad_length_and_flag_are_rejected(self):
        for raw in (b'\x00\x00',b'\x00\x05',b'\xff\xff'):
            with self.assertRaises(CodecError):
                FrameBuffer().feed(raw)
        good=bytearray(encode_packet(CODEC.encode_frame('ClientProto:Heartbeat',{})))
        good[2]=3
        with self.assertRaises(CodecError):
            FrameBuffer().feed(good)

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=SERVER/'tests',prefix='store-')
        self.path=Path(self.temporary.name)/'state.sqlite3'
        self.store=Store(self.path)
        self.uid=self.store.create_account('new-local-account',SEED)['uid']
    def tearDown(self):
        self.store.close()
        assert Path(self.temporary.name).resolve().is_relative_to((SERVER/'tests').resolve())
        self.temporary.cleanup()
    def test_account_is_new_and_persists_with_inventory_currency_consistency(self):
        state=self.store.get_player(self.uid)
        self.assertEqual(state['player']['uid'],self.uid)
        self.assertEqual(state['player']['level'],1)
        self.assertEqual(state['progress']['cleared_stages'],[])
        self.assertEqual(self.store.create_account('new-local-account',SEED)['uid'],self.uid)
        with self.store.transaction(self.uid) as tx:
            expected=tx.add_item(10002,-1)
            self.assertEqual(tx.currency('diamond'),expected)
            added=tx.add_card(71010,{'hp':1387})
        self.store.close()
        self.store=Store(self.path)
        state=self.store.get_player(self.uid)
        self.assertEqual(state['player']['diamond'],expected)
        self.assertEqual(state['inventory']['10002'],expected)
        self.assertIn(added['cid'],[c['cid'] for c in state['cards']])
    def test_failed_mutation_rolls_back_and_tickets_do_not_accept_other_accounts(self):
        before=self.store.get_player(self.uid)
        with self.assertRaises(StorageError):
            with self.store.transaction(self.uid) as tx:
                tx.add_currency('gold',100)
                tx.add_item(10002,-999999)
        self.assertEqual(self.store.get_player(self.uid),before)
        key=self.store.issue_ticket(self.uid)
        self.assertTrue(self.store.validate_ticket(self.uid,key))
        self.assertFalse(self.store.validate_ticket(self.uid+1,key))
        self.assertFalse(self.store.validate_ticket(self.uid,'official-or-invalid-token'))

class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=SERVER/'tests',prefix='socket-')
        directory=Path(self.temporary.name)
        self.store=Store(directory/'state.sqlite3')
        query,game=free_port(),free_port()
        while game==query:
            game=free_port()
        self.server=LocalServer(CODEC,self.store,SEED,'127.0.0.1',query,game,directory/'events.jsonl',assembly_timeout=0.2)
        await self.server.start()
        self.clients=[]
    async def asyncTearDown(self):
        for reader,writer in self.clients:
            writer.close()
            await writer.wait_closed()
        await self.server.close()
        await asyncio.sleep(0)
        self.store.close()
        assert Path(self.temporary.name).resolve().is_relative_to((SERVER/'tests').resolve())
        self.temporary.cleanup()
    async def connect(self,port):
        result=await asyncio.open_connection('127.0.0.1',port)
        self.clients.append(result)
        return result
    async def read_reply(self,reader):
        prefix=await asyncio.wait_for(reader.readexactly(2),2)
        size=struct.unpack('>H',prefix)[0]
        body=await asyncio.wait_for(reader.readexactly(size),2)
        self.assertEqual(body[0],3)
        self.assertGreater(struct.unpack_from('>Q',body,1)[0],0)
        return CODEC.decode_frame(body[9:])
    async def send(self,writer,name,fields):
        writer.write(encode_packet(CODEC.encode_frame(name,fields)))
        await writer.drain()
    async def login(self):
        qr,qw=await self.connect(self.server.query_port)
        packet=encode_packet(CODEC.encode_frame('ClientProto:QueryAccount',{'account':'socket-new','SvnVersion':'3.3.0','pwd':'not-persisted'}))
        qw.write(packet[:3]);await qw.drain();await asyncio.sleep(0.01)
        qw.write(packet[3:]);await qw.drain()
        account=await self.read_reply(qr)
        uid=account.fields['uid']
        await self.send(qw,'ClientProto:PreLoginGame',{'uid':uid,'distinctId':'local'})
        pre=await self.read_reply(qr)
        self.assertEqual(pre.fields['port'],self.server.game_port)
        gr,gw=await self.connect(self.server.game_port)
        await self.send(gw,'ClientProto:LoginGame',{'uid':uid,'key':pre.fields['key'],'SvnVersion':'3.3.0'})
        response=await self.read_reply(gr)
        self.assertEqual(response.name,'LoginProto:LoginGame')
        self.assertEqual(response.fields['infos']['level'],1)
        return uid,gr,gw
    async def test_dual_endpoint_login_heartbeat_and_dynamic_initial_pushes(self):
        uid,reader,writer=await self.login()
        with self.store.transaction(uid) as tx:
            tx.add_currency('gold',7)
        both=encode_packet(CODEC.encode_frame('ClientProto:Heartbeat',{}))+encode_packet(CODEC.encode_frame('ClientProto:InitFinish',{}))
        writer.write(both);await writer.drain()
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')
        replies=[]
        for _ in range(16):
            replies.append(await self.read_reply(reader))
            if replies[-1].name=='ClientProto:InitFinishRet':
                break
        self.assertEqual(replies[-1].name,'ClientProto:InitFinishRet')
        bag=next(r for r in replies if r.name=='PlayerProto:ItemBag')
        gold=next(x['num'] for x in bag.fields['item'] if x['id']==10001)
        self.assertEqual(gold,self.store.get_player(uid)['player']['gold'])
    async def test_invalid_ticket_closes_but_unsupported_request_keeps_session(self):
        reader,writer=await self.connect(self.server.game_port)
        await self.send(writer,'ClientProto:LoginGame',{'uid':900000001,'key':'invalid'})
        self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        uid,reader,writer=await self.login()
        unknown=next(name for name in CODEC.schemas if name.startswith('ClientProto:') and name not in HANDLERS)
        await self.send(writer,unknown,{})
        tip=await self.read_reply(reader)
        self.assertEqual(tip.name,'SystemProto:Tips')
        self.assertEqual(tip.fields['strId'],'GeneralTips')
        self.assertEqual(tip.fields['opName'],unknown)
        self.assertEqual(tip.fields['opId'],CODEC.schemas[unknown]['opcode'])
        self.assertNotIn(tip.fields['strId'],('accExist','accNotExist','accLenErr','pwdLenErr','sqlFail',
                                             'pwdErr','relogin','svrBusy','loadDataErr'))
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')

    async def test_business_rejection_answers_a_tip_and_keeps_session(self):
        import handlers.player_state
        uid,reader,writer=await self.login()
        before=self.store.get_player(uid)
        await self.send(writer,'PlayerProto:SetClientData',{'key':'plot_data','type':9,'data':'{}'})
        tip=await self.read_reply(reader)
        self.assertEqual(tip.name,'SystemProto:Tips')
        self.assertEqual(tip.fields['opName'],'PlayerProto:SetClientData')
        self.assertEqual(self.store.get_player(uid),before)
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')

    async def test_client_data_rejection_logs_the_undecodable_key(self):
        """P1.3: one historical frame carried no usable 'type'.  The tip has to stay generic
        while the event names the key, otherwise the caller cannot be identified at all."""
        import handlers.player_state
        uid,reader,writer=await self.login()
        await self.send(writer,'PlayerProto:SetClientData',{'key':'mystery_key'})
        tip=await self.read_reply(reader)
        self.assertEqual(tip.name,'SystemProto:Tips')
        self.assertEqual(tip.fields['opName'],'PlayerProto:SetClientData')
        events=[json.loads(line) for line in (Path(self.temporary.name)/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        failed=next(event for event in events if event['event']=='request_failed')
        self.assertEqual(failed['reason'],'business_rejected')
        self.assertEqual(failed['error'],'StorageError')
        self.assertEqual(failed['diagnostic'],{'data_present':False,'key':'mystery_key','type':None})
        self.assertNotIn('mystery_key',failed['detail'])
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')

    async def test_unexpected_handler_defect_logs_traceback_and_keeps_session(self):
        async def explode(ctx,fields):
            raise TypeError('synthetic defect')
        with patch.dict(HANDLERS,{'ClientProto:Heartbeat':explode}):
            uid,reader,writer=await self.login()
            await self.send(writer,'ClientProto:Heartbeat',{})
            tip=await self.read_reply(reader)
            self.assertEqual(tip.name,'SystemProto:Tips')
        events=[json.loads(line) for line in (Path(self.temporary.name)/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        failed=next(event for event in events if event['event']=='request_failed')
        self.assertEqual(failed['reason'],'unexpected_failure')
        self.assertEqual(failed['name'],'ClientProto:Heartbeat')
        self.assertEqual(failed['disposition'],'continue')
        self.assertIn('TypeError',failed['traceback'])
        self.assertTrue(failed['connection_id'])
        self.assertGreaterEqual(failed['request_id'],1)
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')

    async def test_exchange_then_heartbeat_keeps_same_connection_open(self):
        uid,reader,writer=await self.login()
        before=deepcopy(self.store.get_player(uid))
        await self.send(writer,'ClientProto:ExchangeItem',{
            'exchanges':[{'id':1003,'num':1,'type':2}], 'card_pool_id':4321})
        replies=[]
        for _ in range(5):
            replies.append(await self.read_reply(reader))
            if replies[-1].name=='ClientProto:ExchangeItemRet':
                break
        self.assertEqual(replies[-1].name,'ClientProto:ExchangeItemRet')
        self.assertEqual(replies[-1].fields['card_pool_id'],4321)
        state=self.store.get_player(uid)
        self.assertEqual(state['player']['diamond'],before['player']['diamond']-1)
        self.assertEqual(state['login']['BIND_DIAMOND'],before['login']['BIND_DIAMOND']+1)
        self.assertEqual(state['cards'],before['cards'])
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')
    async def test_connection_limit_is_logged_and_refused(self):
        directory=Path(self.temporary.name)/'limit'
        directory.mkdir()
        store=Store(directory/'state.sqlite3')
        query,game=free_port(),free_port()
        while game==query:
            game=free_port()
        server=LocalServer(CODEC,store,SEED,'127.0.0.1',query,game,directory/'events.jsonl',max_connections=1)
        await server.start()
        try:
            first=await asyncio.open_connection('127.0.0.1',query)
            await asyncio.sleep(0.05)
            second=await asyncio.open_connection('127.0.0.1',query)
            self.assertEqual(await asyncio.wait_for(second[0].read(),2),b'')
            events=[json.loads(line) for line in (directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
            refused=[event for event in events if event['event']=='connection_limit_rejected']
            self.assertEqual(len(refused),1)
            self.assertEqual(refused[0]['limit'],1)
            second[1].close()
            await second[1].wait_closed()
            first[1].close()
            await first[1].wait_closed()
        finally:
            await server.close()
            store.close()

    async def test_partial_frame_assembly_timeout(self):
        reader,writer=await self.connect(self.server.query_port)
        writer.write(b'\x00\x20\x01');await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')

    async def test_login_notification_then_name_check_keeps_socket_open(self):
        import handlers.player_state
        uid,reader,writer=await self.login()
        await self.send(writer,'FightProtocol:RogueTSetWindow',{'ty':1,'value':1791073579})
        response=await self.read_reply(reader)
        self.assertEqual(response.name,'FightProto:RogueTSetWindowRet')
        await self.send(writer,'PlayerProto:PlrNameCheckUse',{'name':'离线队长'})
        response=await self.read_reply(reader)
        self.assertEqual(response.name,'PlayerProto:PlrNameCheckUseRet')
        self.assertFalse(response.fields['isUse'])
        self.assertEqual(self.store.get_player(uid)['ui_preferences']['rogue_t_window']['win1'],1791073579)

if __name__=='__main__':
    unittest.main()
