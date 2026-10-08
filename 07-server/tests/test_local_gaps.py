"""task-2 regressions: honest answers for the observed disconnect requests,
the default local module list, and ordinary client UI data under SetClientData."""
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import re
import secrets
import socket
import struct
import sys
import tempfile
import unittest

sys.dont_write_bytecode=True
SERVER=Path(__file__).resolve().parents[1]
ROOT=SERVER.parent
sys.path.insert(0,str(SERVER))
from database import Store, StorageError
from server_core import Context, LocalServer, load_dependencies
from protocol_codec import encode_packet, readable
from handlers import local_gaps
from handlers import player_state as domain
from handlers import progression

CODEC,SEED=load_dependencies(ROOT/"05-protocol"/"endpoints.json",SERVER/"data"/"new_account_seed.json")

# The requests the disconnect audit observed, with the field shapes
# PlayerProto.lua/ArmyProto.lua/ExplorationProto.lua/ShareProto.lua actually send.
# Random boards moved out of local_gaps: handlers/panels.py now stores them and
# answers every board request with its own *Ret.
REQUESTS=[
    ('ArmyProto:GetSelfPracticeInfo', {}, 'ArmyProto:GetSelfPracticeInfoRet'),
    ('PlayerProto:GetRank', {'nPage':1,'rank_type':10047}, 'PlayerProto:GetRankRet'),
    ('ExplorationProto:GetReward', {'id':1,'rid':-1}, None),
    ('ExplorationProto:GetReward', {'id':1,'rid':3,'ix':2}, None),
    ('ShareProto:AddShareCount', {}, None),
]
PANEL_REQUESTS=['PlayerProto:SetRandomPanel','PlayerProto:GetRandomPanelDetail',
                'PlayerProto:SetPanelRandomType','PlayerProto:RemoveRandomPanel',
                'PlayerProto:AddRandomSkinsAll','PlayerProto:SetRandomPanelName',
                'PlayerProto:RandomPanelClean']
NAMES=sorted({name for name,_,_ in REQUESTS})
EQUIP_NAMES=["EquipProto:EquipUp","EquipProto:EquipUps","EquipProto:EquipDown",
            "EquipProto:EquipUpgrade","EquipProto:EquipLock","EquipProto:SetIsNew"]


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


def default_handlers():
    """The module list start_local.ps1:3 actually starts, parsed from the script."""
    text=(ROOT/"start_local.ps1").read_text(encoding="utf-8")
    match=re.search(r'\$Handlers\s*=\s*@\(([^)]*)\)',text)
    if match is None:
        raise AssertionError('start_local.ps1 default handler list not found')
    return re.findall(r"'([^']+)'",match.group(1))


async def start_and_close(server):
    await server.start()
    await server.close()


class LocalGapHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=SERVER/'tests',prefix='gaps-')
        self.path=Path(self.temporary.name)/'state.sqlite3'
        self.store=Store(self.path)
        self.uid=self.store.create_account('gap-test',SEED)['uid']
        self.ctx=Context(LocalServer(CODEC,self.store,SEED),'game',self.uid,True)
    def tearDown(self):
        self.store.close()
        assert Path(self.temporary.name).resolve().is_relative_to((SERVER/'tests').resolve())
        self.temporary.cleanup()
    def state(self):
        return self.store.get_player(self.uid)
    def revision(self):
        return self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?',(self.uid,)).fetchone()[0]
    def call(self,handler,fields):
        replies=asyncio.run(handler(self.ctx,fields))
        for reply in replies:
            inner=CODEC.encode_frame(reply.name,reply.fields)
            frames,tail=CODEC.decode_stream(inner)
            self.assertFalse(tail)
            self.assertEqual(frames[0].name,reply.name)
        return replies
    def decoded(self,reply):
        return readable(CODEC.decode_frame(CODEC.encode_frame(reply.name,reply.fields)).fields)

    def test_practice_and_rank_are_empty_reads_that_do_not_touch_the_save(self):
        before=deepcopy(self.state()); revision=self.revision()
        practice=self.call(local_gaps.self_practice_info,{})
        self.assertEqual([reply.name for reply in practice],['ArmyProto:GetSelfPracticeInfoRet'])
        fields=self.decoded(practice[0])
        self.assertEqual(set(fields),{'info','army_ix'})
        self.assertEqual(fields['army_ix'],0)
        self.assertEqual(fields['info'],local_gaps.PRACTICE_ZERO)
        # Unknown keys are dropped silently by the codec, so compare with the sibling read
        # that already answers this structure (handlers/initialization.py:220).
        from handlers import initialization
        sibling=asyncio.run(initialization.practice_info(self.ctx,{}))[0]
        self.assertEqual(fields['info'],sibling.fields['info'])
        schema={field['name'] for field in CODEC.schemas['sPracticeInfo']['fields']}
        self.assertTrue(set(fields['info']) <= schema)
        self.assertEqual(self.call(local_gaps.rank,{'nPage':1,'rank_type':10047})[0].fields,
                         {'rank_type':10047,'data':[],'rank':0,'score':'0','next_refresh_time':0,'turn_num':0})
        # reward_issue is a json field; it is omitted instead of encoded as a string.
        self.assertNotIn('reward_issue',self.decoded(self.call(local_gaps.rank,{'nPage':1,'rank_type':10047})[0]))
        self.assertEqual(before,self.state()); self.assertEqual(revision,self.revision())

    def test_unhonourable_actions_refuse_by_name_and_keep_the_save(self):
        cases=[(local_gaps.exploration_reward,{'id':1,'rid':-1},'没有发放任何奖励'),
               (local_gaps.add_share_count,{},'未记录分享次数')]
        for handler,fields,needle in cases:
            before=deepcopy(self.state()); revision=self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(handler,fields)
            self.assertIn(needle,str(raised.exception))
            self.assertEqual(before,self.state())
            self.assertEqual(revision,self.revision())

    def test_rank_requires_an_integer_board_type(self):
        for fields in [{},{"nPage":1},{"nPage":1,"rank_type":True}]:
            before=deepcopy(self.state())
            with self.assertRaises(StorageError):
                self.call(local_gaps.rank,fields)
            self.assertEqual(before,self.state())

    def test_guide_bulk_skip_persists_through_set_client_data_and_reloads(self):
        payload={('k%d' % group):1 for group in progression.guide_groups()}
        self.call(domain.set_client_data,{'key':'guide_data_key','type':3,'data':json.dumps(payload)})
        state=self.state()
        expected=sorted(progression.guide_groups())
        self.assertEqual(state['progress']['completed_guides'],expected)
        self.assertEqual(state['progress']['skipped_guides'],expected)
        stored=self.call(domain.get_client_data,{'key':'guide_data_key'})[0].fields
        self.assertEqual(stored['type'],3)
        self.assertEqual(json.loads(stored['data']),payload)

    def test_guide_single_completion_and_ordinary_ui_keys_still_persist(self):
        self.call(domain.set_client_data,{'key':'guide_data_key','type':3,'data':json.dumps({'k10':1})})
        self.assertEqual(self.state()['progress']['completed_guides'],[10])
        self.assertNotIn('skipped_guides',self.state()['progress'])
        ordinary=[
            ('plot_data',{'type':3,'data':json.dumps({'line_1':10013})}),
            ('new_player_fight_state',{'type':1,'data':'1'}),
            ('passiveRed_isLook',{'type':3,'data':json.dumps({'71010':1})}),
            ('fight_data_key',{'type':3,'data':json.dumps(['2026-10-05 01:00:00'])}),
            ('empty_ui_table',{'type':3,'data':'{}'}),
        ]
        for key,record in ordinary:
            self.call(domain.set_client_data,{"key":key,**record})
            stored=self.call(domain.get_client_data,{"key":key})[0].fields
            self.assertEqual(stored,{"key":key,**record})
        # A non-guide key never rewrites tutorial progress.
        self.assertEqual(self.state()['progress']['completed_guides'],[10])

    def test_default_module_list_registers_local_gaps_and_equipment_at_startup(self):
        modules=default_handlers()
        self.assertIn('handlers.local_gaps',modules)
        self.assertIn('handlers.equipment',modules)
        # The random-board family lives in handlers/panels and must be part of the
        # default launcher list, or 3630-3643 stay unsupported at runtime.
        self.assertIn('handlers.panels',modules)
        # The pre-existing tail keeps its order wherever the panel module is added.
        self.assertEqual([name for name in modules if name in
                          ('handlers.local_gaps','handlers.equipment','handlers.building',
                           'handlers.dorm','handlers.ability')],
                         ['handlers.local_gaps','handlers.equipment','handlers.building',
                          'handlers.dorm','handlers.ability'])
        for name in modules:
            importlib.import_module(name)
        codec,seed=load_dependencies(ROOT/'05-protocol'/'endpoints.json',SERVER/'data'/'new_account_seed.json')
        directory=Path(self.temporary.name)
        query,game=free_port(),free_port()
        while game==query:
            game=free_port()
        server=LocalServer(codec,self.store,seed,'127.0.0.1',query,game,directory/'started.jsonl')
        asyncio.run(start_and_close(server))
        events=[json.loads(line) for line in (directory/'started.jsonl').read_text(encoding='utf-8').splitlines()]
        started=next(event for event in events if event['event']=='started')
        for name in EQUIP_NAMES+NAMES+PANEL_REQUESTS:
            self.assertIn(name,started['handlers'])


class LocalGapSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=SERVER/'tests',prefix='gap-socket-')
        directory=Path(self.temporary.name)
        self.store=Store(directory/'state.sqlite3')
        self.events=directory/"events.jsonl"
        query,game=free_port(),free_port()
        while game==query:
            game=free_port()
        self.server=LocalServer(CODEC,self.store,SEED,'127.0.0.1',query,game,self.events,assembly_timeout=0.2)
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
        return CODEC.decode_frame(body[9:])
    async def send(self,writer,name,fields):
        writer.write(encode_packet(CODEC.encode_frame(name,fields)))
        await writer.drain()
    async def login(self):
        qr,qw=await self.connect(self.server.query_port)
        await self.send(qw,'ClientProto:QueryAccount',{'account':'gap-socket','SvnVersion':'3.3.0','pwd':'not-persisted'})
        account=await self.read_reply(qr)
        uid=account.fields['uid']
        await self.send(qw,'ClientProto:PreLoginGame',{'uid':uid,'distinctId':'local'})
        pre=await self.read_reply(qr)
        self.assertEqual(pre.fields['port'],self.server.game_port)
        gr,gw=await self.connect(self.server.game_port)
        await self.send(gw,'ClientProto:LoginGame',{'uid':uid,'key':pre.fields['key'],'SvnVersion':'3.3.0'})
        self.assertEqual((await self.read_reply(gr)).name,"LoginProto:LoginGame")
        return uid,gr,gw
    async def test_observed_requests_answer_and_keep_the_session(self):
        uid,reader,writer=await self.login()
        for name,fields,reply_name in REQUESTS:
            await self.send(writer,name,fields)
            reply=await self.read_reply(reader)
            if reply_name is None:
                self.assertEqual(reply.name,'SystemProto:Tips')
                self.assertEqual(reply.fields['strId'],'GeneralTips')
                self.assertEqual(reply.fields['opName'],name)
                self.assertEqual(reply.fields['opId'],CODEC.schemas[name]['opcode'])
                self.assertEqual(len(reply.fields['args']),1)
                self.assertEqual(reply.fields['args'][0]['type'],0)
                self.assertTrue(reply.fields['args'][0]['param'])
            else:
                self.assertEqual(reply.name,reply_name)
            await self.send(writer,'ClientProto:Heartbeat',{})
            self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')
        events=[json.loads(line) for line in self.events.read_text(encoding='utf-8').splitlines()]
        self.assertFalse([e for e in events if e['event']=='connection_closed'])
        self.assertFalse([e for e in events if e['event']=='unsupported_handler' and e.get('name') in NAMES])
        refused=[e for e in events if e['event']=='request_failed' and e.get('name') in NAMES]
        self.assertEqual(len(refused),len([1 for _,_,reply in REQUESTS if reply is None]))
        self.assertTrue(all(e['reason']=='business_rejected' and e['disposition']=='continue' for e in refused))
        answered=[e.get('name') for e in events if e['event']=='response']
        self.assertIn('ArmyProto:GetSelfPracticeInfoRet',answered)
        self.assertIn('PlayerProto:GetRankRet',answered)
        self.assertEqual(answered.count('SystemProto:Tips'),len([1 for _,_,reply in REQUESTS if reply is None]))
        requests=[e.get('name') for e in events if e['event']=='request']
        for name in NAMES:
            self.assertIn(name,requests)


    async def test_random_board_protocols_answer_over_the_socket(self):
        uid,reader,writer=await self.login()
        row={'idx':7,'ty':2,'ids':[7101001],'bg':1,
             'detail1':{'x':0,'y':0,'scale':1,'top':True,'live2d':False},
             'detail2':{'x':0,'y':0,'scale':1,'top':False,'live2d':False}}
        await self.send(writer,'PlayerProto:SetRandomPanel',{'random_panel':row})
        created=await self.read_reply(reader)
        self.assertEqual(created.name,'PlayerProto:SetRandomPanelRet')
        self.assertEqual(created.fields['random_idx'],8)
        self.assertEqual(created.fields['random_panel']['ids'],[7101001])
        # An invalid slot answers with a tip and leaves the session usable.
        await self.send(writer,'PlayerProto:SetRandomPanel',{'random_panel':dict(row,idx=3)})
        refused=await self.read_reply(reader)
        self.assertEqual(refused.name,'SystemProto:Tips')
        self.assertEqual(refused.fields['opName'],'PlayerProto:SetRandomPanel')
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')
        # GetNewPanel pushes the random frame first, GetNewPanelRet last
        # (CRoleDisplayMgr:LoginCheck runs inside the GetNewPanelRet callback).
        await self.send(writer,'PlayerProto:GetNewPanel',{})
        pushed=[await self.read_reply(reader) for _ in range(2)]
        self.assertEqual([frame.name for frame in pushed],
                         ['PlayerProto:GetRandomPanelRet','PlayerProto:GetNewPanelRet'])
        self.assertTrue(pushed[0].fields['finish'])
        # The map decoder keys on the decoded idx value (an int), not the stored
        # string key; the client reads v.idx, so both agree on the board.
        self.assertEqual(sorted(pushed[0].fields['random_panels']),[7])
        self.assertEqual(pushed[0].fields['random_idx'],8)
        # The board exists but is not selected: random=0 omits random_panel.
        self.assertEqual(pushed[1].fields['random'],0)
        self.assertNotIn('random_panel',pushed[1].fields)
        await self.send(writer,'PlayerProto:SetNewPanel',{'random':1,'using':7,'setting':0})
        selected=await self.read_reply(reader)
        self.assertEqual(selected.name,'PlayerProto:GetNewPanelRet')
        self.assertEqual((selected.fields['random'],selected.fields['using']),(1,7))
        self.assertEqual(selected.fields['random_panel']['idx'],7)
        # Removing the selected board falls back to the six-slot selection.
        await self.send(writer,'PlayerProto:RemoveRandomPanel',{'idx':7})
        self.assertEqual((await self.read_reply(reader)).fields['idx'],7)
        await self.send(writer,'PlayerProto:GetNewPanel',{})
        after=[await self.read_reply(reader) for _ in range(2)]
        self.assertEqual(after[0].fields['random_panels'],{})
        self.assertEqual((after[1].fields['random'],after[1].fields['using']),(0,1))
        # random=1 without a live random board is refused, never echoed as success.
        await self.send(writer,'PlayerProto:SetNewPanel',{'random':1,'using':7,'setting':0})
        self.assertEqual((await self.read_reply(reader)).name,'SystemProto:Tips')
        await self.send(writer,'ClientProto:Heartbeat',{})
        self.assertEqual((await self.read_reply(reader)).name,'LoginProto:Heartbeat')


if __name__=='__main__':
    unittest.main()
