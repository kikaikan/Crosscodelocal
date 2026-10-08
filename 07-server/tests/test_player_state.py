import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

SERVER=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SERVER))
from server_core import Context, LocalServer, load_dependencies
from database import Store, StorageError
from handlers import player_state as domain
from protocol_codec import readable

CODEC,SEED=load_dependencies(SERVER.parent/'05-protocol/endpoints.json',SERVER/'data/new_account_seed.json')


class PlayerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=SERVER/'tests',prefix='profile-')
        self.path=Path(self.temp.name)/'state.sqlite3'
        self.store=Store(self.path)
        self.uid=self.store.create_account('profile-test',SEED)['uid']
        self.ctx=Context(LocalServer(CODEC,self.store,SEED),'game',self.uid,True)

    def tearDown(self):
        self.store.close()
        assert Path(self.temp.name).resolve().is_relative_to((SERVER/'tests').resolve())
        self.temp.cleanup()

    def call(self,handler,fields):
        replies=asyncio.run(handler(self.ctx,fields))
        for reply in replies:
            inner=CODEC.encode_frame(reply.name,reply.fields)
            frames,tail=CODEC.decode_stream(inner)
            self.assertFalse(tail)
            self.assertEqual(frames[0].name,reply.name)
        return replies

    def wire_call(self,name,fields):
        frame=CODEC.decode_frame(CODEC.encode_frame(name,fields))
        replies=asyncio.run(self.ctx.server.dispatch(self.ctx,frame))
        decoded=[]
        for reply in replies:
            decoded.append(readable(CODEC.decode_frame(CODEC.encode_frame(reply.name,reply.fields)).fields))
        return replies,decoded

    def test_settings_client_data_and_deletion_persist(self):
        self.call(domain.setting,{'equip_state':True})
        # New accounts now inherit the collection unlock's plot_data; assert that
        # seed shape, then drop the key to keep the empty-default fallback covered.
        seeded=self.call(domain.get_client_data,{'key':'plot_data'})[0].fields
        self.assertEqual(seeded['type'],3)
        self.assertTrue(json.loads(seeded['data'])['line_1'])
        with self.store.transaction(self.uid) as tx:
            tx.state['client_data'].pop('plot_data',None)
        initial=self.call(domain.get_client_data,{'key':'plot_data'})[0].fields
        self.assertEqual(initial,{'key':'plot_data','type':3,'data':'{}'})
        self.call(domain.set_client_data,{'key':'new_player_fight','type':1,'data':'1'})
        self.store.close()
        self.store=Store(self.path); self.ctx.server.store=self.store
        self.assertTrue(self.store.get_player(self.uid)['settings']['equip_state'])
        self.assertEqual(self.call(domain.get_client_data,{'key':'new_player_fight'})[0].fields['data'],'1')
        self.call(domain.set_client_data,{'key':'new_player_fight','type':4})
        self.assertEqual(self.call(domain.get_client_data,{'key':'new_player_fight'})[0].fields['type'],4)

    def test_atomic_team_batch_rejects_unowned_fighter_and_keeps_original(self):
        state=self.store.get_player(self.uid)
        good=deepcopy(state['teams'][0]); good['name']='本地编队'
        bad=deepcopy(state['teams'][1]); bad['data']=[{'cid':9999,'index':1,'row':2,'col':2}]; bad['leader']=9999
        with self.assertRaises(StorageError):
            self.call(domain.mult_set_team,{'infos':[good,bad]})
        self.assertEqual(self.store.get_player(self.uid),state)
        result=self.call(domain.set_team,{'info':good})
        self.assertEqual(result[0].fields['info']['name'],'本地编队')

    def test_lock_rollback_and_profile_name_setup_are_once_only(self):
        before=self.store.get_player(self.uid)
        with self.assertRaises(StorageError):
            self.call(domain.card_lock,{'ops':[{'cid':1,'lock':1},{'cid':777,'lock':0}]})
        self.assertEqual(self.store.get_player(self.uid),before)
        self.call(domain.set_player_name,{'name':'离线队长','index':2,'month':2,'day':29,'use_vid':1})
        state=self.store.get_player(self.uid)
        self.assertEqual(state['player']['name'],'离线队长')
        self.assertEqual(state['cards'][0]['cfgid'],71020)
        self.assertEqual(state['cards'][0]['cid'],1)
        self.assertEqual(state['login']['can_modify_name'],0)
        self.assertEqual(state['login']['sel_card_ix'],2)
        with self.assertRaises(StorageError):
            self.call(domain.set_player_name,{'name':'再次设置','index':1,'month':1,'day':1})

    def test_board_click_is_recorded_and_returns_no_fabricated_reply(self):
        replies=self.call(domain.click_board,{})
        self.assertTrue(all(reply.name.startswith('TaskProto:') for reply in replies))
        self.assertEqual(self.store.get_player(self.uid)['local_events']['board_clicks'],1)

    def test_rogue_window_ack_persists_without_unlock_and_name_check_still_answers(self):
        before=self.store.get_player(self.uid)['progress']
        replies=self.call(domain.rogue_window,{'ty':1,'value':1791073579})
        self.assertEqual(replies[0].fields,{'ty':1,'value':1791073579})
        self.store.close()
        self.store=Store(self.path);self.ctx.server.store=self.store
        state=self.store.get_player(self.uid)
        self.assertEqual(state['ui_preferences']['rogue_t_window']['win1'],1791073579)
        self.assertEqual(state['progress'],before)
        self.assertFalse(self.call(domain.name_check,{'name':'离线队长'})[0].fields['isUse'])
        with self.assertRaises(StorageError):
            self.call(domain.rogue_window,{'ty':3,'value':1})

    def test_ai_strategy_zero_to_four_round_trip_apply_switch_and_persist(self):
        _,empty=self.wire_call('PlayerProto:GetAIStrategy',{'cid':[1]})
        self.assertEqual(empty[0]['data'],[{'cid':1,'tStrategyData':{}}])
        strategy={1:[101,2],3:{1:7,3:9},'bOverLoad':True}
        replies,_=self.wire_call('PlayerProto:SetAIStrategy',{'data':[{
            'nTeamIndex':1,'nCardIndex':1,'nStrategyIndex':0,
            'tStrategyData':strategy,'bApply':True}]})
        self.assertEqual(replies[0].fields,{'ret':True})
        state=self.store.get_player(self.uid)
        self.assertEqual(state['teams'][0]['data'][0]['nStrategyIndex'],0)
        self.assertEqual(state['ai_strategies']['1']['0']['1'],[101,2])
        self.store.close()
        self.store=Store(self.path); self.ctx.server.store=self.store
        _,loaded=self.wire_call('PlayerProto:GetAIStrategy',{'cid':[1]})
        restored=loaded[0]['data'][0]['tStrategyData'][0]
        self.assertEqual(restored[1],[101,2])
        self.assertEqual(restored[3],{1:7,3:9})
        self.assertTrue(restored['bOverLoad'])
        self.wire_call('PlayerProto:SwitchAIStrategy',{
            'nTeamIndex':1,'nCardIndex':1,'nStrategyIndex':4})
        self.assertEqual(self.store.get_player(self.uid)['teams'][0]['data'][0]['nStrategyIndex'],4)

    def test_ai_strategy_batch_is_atomic_and_rejects_out_of_range_indices(self):
        before=self.store.get_player(self.uid)
        payload={'data':[
            {'nTeamIndex':1,'nCardIndex':1,'nStrategyIndex':0,
             'tStrategyData':{1:[1]},'bApply':True},
            {'nTeamIndex':1,'nCardIndex':1,'nStrategyIndex':5,
             'tStrategyData':{1:[2]},'bApply':True}]}
        with self.assertRaises(StorageError):
            self.wire_call('PlayerProto:SetAIStrategy',payload)
        self.assertEqual(self.store.get_player(self.uid),before)
        with self.assertRaises(StorageError):
            self.call(domain.switch_ai_strategy,{
                'nTeamIndex':1,'nCardIndex':1,'nStrategyIndex':-1})
        with self.assertRaises(StorageError):
            self.wire_call('PlayerProto:SwitchAIStrategy',{
                'nTeamIndex':1,'nCardIndex':1,'nStrategyIndex':5})
        with self.assertRaises(StorageError):
            self.wire_call('PlayerProto:GetAIStrategy',{'cid':[999999]})


if __name__=='__main__':
    unittest.main()
