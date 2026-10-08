"""Chip wire callbacks, original costs, atomic rollback and durable ownership."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
from database import Store,StorageError
from server_core import Context
from protocol_codec import IVProtoCodec,WireConfig,CodecError
from handlers import equipment
import equip_service as service

class EquipmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'players.sqlite3'
        self.store=Store(self.path)
        seed=json.loads((HERE/'data/new_account_seed.json').read_text('utf-8'))
        self.uid=self.store.create_account('equipment-isolated',seed)['uid']
        self.codec=IVProtoCodec(json.loads((HERE.parent/'05-protocol/endpoints.json').read_text('utf-8')),
                                WireConfig('little',max_frame_size=65535))
        self.ctx=Context(SimpleNamespace(store=self.store,codec=self.codec),'game',self.uid,True)
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('gold',1_000_000)
            tx.add_item(10021,10_000)
            tx.state['equips']=[self.chip(1),self.chip(2,2060102),self.chip(3,2000101,skills=[],num=2)]
            other=deepcopy(tx.state['cards'][0]);other['cid']=2
            tx.state['cards'].append(other)
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    @staticmethod
    def chip(sid,cfgid=2020101,skills=None,**extra):
        result={'cfgid':cfgid,'sid':sid,'level':0,'exp':0,'lock':0,
                'rand_skill_type':0,'rand_skill_value':0,'card_id':0,'is_new':1,'num':1,
                'skills':[20201] if skills is None else skills}
        result.update(extra);return result
    def state(self):return self.store.get_player(self.uid)
    def revision(self):
        return self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?',(self.uid,)).fetchone()[0]
    def wire(self,replies):
        for reply in replies:
            raw=self.codec.encode_frame(reply.name,reply.fields)
            decoded=self.codec.decode_frame(raw)
            self.assertEqual(raw,self.codec.encode_frame(decoded.name,decoded.fields))
            if reply.name=='PlayerProto:CardUpdate':
                self.assertEqual(decoded.fields['store_exp'],self.state()['store_exp'])
            if reply.name=='SystemProto:Tips':
                self.assertEqual(decoded.fields['args'][0]['type'],0)
                self.assertTrue(decoded.fields['args'][0]['param'])
    async def denied(self,fn,fields):
        before=self.state();revision=self.revision()
        result=await fn(self.ctx,fields);self.wire(result)
        self.assertEqual([r.name for r in result],['SystemProto:Tips'])
        self.assertEqual(self.state(),before);self.assertEqual(self.revision(),revision)
        return result
    async def test_source_get_equips_full_wire_fields_and_material_count(self):
        replies=service.get_equips(self.ctx);self.wire(replies)
        fields=replies[0].fields
        self.assertEqual((fields['cur_size'],fields['max_size'],fields['materialNum']),(3,500,2))
        self.assertEqual(fields['equips'][0]['level'],0)
        self.assertEqual(service.config_record('cfgCfgEquipExp.lua',1)['tInfos'][0]['nExp'],100)
        self.assertEqual(service.config_record('cfgCfgEquipSkill.lua',20202)['fGetBaseVal'],0.06)
    async def test_equip_up_down_full_card_views_hp_and_persistence(self):
        self.wire(await equipment.equip_up(self.ctx,{'equip_id':1,'target_card_id':1}))
        state=self.state();card=state['cards'][0]
        self.assertEqual(card['equip_ids'],{'1':1})
        self.assertEqual(card['equips'],[state['equips'][0]])
        self.assertEqual(state['equips'][0]['card_id'],1)
        self.store.close();self.store=Store(self.path);self.ctx.server.store=self.store
        self.assertEqual(self.state()['cards'][0]['equip_ids'],{'1':1})
        self.wire(await equipment.equip_down(self.ctx,{'equip_ids':[1]}))
        state=self.state();self.assertEqual(state['cards'][0]['equips'],[])
        self.assertEqual(state['cards'][0]['equip_ids'],{})
        self.assertEqual(state['equips'][0]['card_id'],0)
    async def test_swap_and_transfer_never_leave_orphan_or_duplicate_slot(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['equips'].append(self.chip(4,2370101))
        self.wire(await equipment.equip_up(self.ctx,{'equip_id':1,'target_card_id':1}))
        self.wire(await equipment.equip_up(self.ctx,{'equip_id':4,'target_card_id':2}))
        replies=await equipment.equip_up(self.ctx,{'equip_id':1,'target_card_id':2});self.wire(replies)
        self.assertEqual(replies[0].name,'EquipProto:EquipDownRet')
        self.assertEqual(set(replies[0].fields['equip_ids']),{1,4})
        state=self.state()
        self.assertEqual(state['cards'][0]['equip_ids'],{})
        self.assertEqual(state['cards'][1]['equip_ids'],{'1':1})
        self.assertEqual([e['sid'] for e in state['equips'] if e['card_id']==2],[1])
        self.assertEqual(replies[-1].name,'EquipProto:EquipUpRet')
    async def test_batch_two_slots_and_same_slot_refusal(self):
        self.wire(await equipment.equip_ups(self.ctx,{'equip_ids':[1,2],'target_card_id':1}))
        self.assertEqual(self.state()['cards'][0]['equip_ids'],{'1':1,'2':2})
        with self.store.transaction(self.uid) as tx:tx.state['equips'].append(self.chip(4))
        await self.denied(equipment.equip_ups,{'equip_ids':[1,4],'target_card_id':2})
    async def test_lock_and_isnew_wire_and_locked_feed_rollback(self):
        self.wire(await equipment.lock(self.ctx,{'infos':[{'sid':2,'lock':1}]}))
        await self.denied(equipment.upgrade,{'sid':1,'equip_ids':[2],'items':[]})
        self.wire(await equipment.lock(self.ctx,{'infos':[{'sid':2,'lock':0}]}))
        self.wire(await equipment.set_new(self.ctx,{'sids':[1,2],'is_new':0}))
        self.assertEqual([e['is_new'] for e in self.state()['equips'][:2]],[0,0])
        await self.denied(equipment.lock,{'infos':[{'sid':1,'lock':True}]})
    async def test_item_exp_source_cost_and_unchanged_random_skills(self):
        before=self.state();replies=await equipment.upgrade(self.ctx,{'sid':1,'items':[{'id':10021,'num':100,'type':2}]})
        self.wire(replies);state=self.state()
        self.assertEqual((state['equips'][0]['level'],state['equips'][0]['exp']),(1,0))
        self.assertEqual(state['equips'][0]['skills'],before['equips'][0]['skills'])
        self.assertEqual(state['player']['gold'],before['player']['gold']-500)
        self.assertEqual(state['inventory']['10021'],before['inventory']['10021']-100)
        self.assertEqual(replies[-1].name,'EquipProto:EquipUpgradeRet')
        self.assertEqual(replies[-1].fields['id'],1)
    async def test_chip_and_stacked_material_source_cost_and_delete_callbacks(self):
        before=self.state()
        replies=await equipment.upgrade(self.ctx,{'sid':1,'equip_ids':[2,3],
                                                 'items':[{'id':10021,'num':100,'type':2}]})
        self.wire(replies);state=self.state()
        self.assertEqual((state['equips'][0]['level'],state['equips'][0]['exp']),(2,50))
        self.assertEqual(state['player']['gold'],before['player']['gold']-2000)
        self.assertEqual([e['sid'] for e in state['equips']],[1,3])
        self.assertEqual(state['equips'][1]['num'],1)
        delete=next(r for r in replies if r.name=='EquipProto:EquipDelete')
        self.assertEqual(delete.fields['sids'],[2])
        await self.denied(equipment.upgrade,{'sid':1,'equip_ids':[2,3]})
    async def test_upgrade_equipped_chip_updates_card_snapshot(self):
        self.wire(await equipment.equip_up(self.ctx,{'equip_id':1,'target_card_id':1}))
        self.wire(await equipment.upgrade(self.ctx,{'sid':1,'items':[{'id':10021,'num':100,'type':2}]}))
        state=self.state()
        self.assertEqual(state['cards'][0]['equips'][0]['level'],1)
        self.assertEqual(state['cards'][0]['equips'][0],state['equips'][0])
    async def test_cap_surplus_matches_source_preview_then_refuses_further_cost(self):
        self.wire(await equipment.upgrade(self.ctx,{'sid':1,'items':[{'id':10021,'num':500,'type':2}]}))
        self.assertEqual((self.state()['equips'][0]['level'],self.state()['equips'][0]['exp']),(3,50))
        await self.denied(equipment.upgrade,{'sid':1,'items':[{'id':10021,'num':1,'type':2}]})
    async def test_insufficient_currency_item_and_foreign_input_rollback(self):
        with self.store.transaction(self.uid) as tx:tx.add_currency('gold',-tx.currency('gold'))
        await self.denied(equipment.upgrade,{'sid':1,'items':[{'id':10021,'num':100,'type':2}]})
        with self.store.transaction(self.uid) as tx:tx.add_currency('gold',1_000_000)
        await self.denied(equipment.upgrade,{'sid':1,'items':[{'id':10021,'num':20000,'type':2}]})
        await self.denied(equipment.upgrade,{'sid':999,'equip_ids':[2]})
        await self.denied(equipment.equip_up,{'equip_id':1,'target_card_id':999})
        await self.denied(equipment.equip_up,{'equip_id':3,'target_card_id':1})
    async def test_invalid_recipes_duplicates_target_and_equipped_feed(self):
        for f in [{'sid':1,'equip_ids':[1]}, {'sid':1,'equip_ids':[2,2]},
                  {'sid':1,'items':[{'id':10001,'num':1,'type':2}]},
                  {'sid':1,'items':[{'id':10021,'num':True,'type':2}]},
                  {'sid':1,'items':[{'id':10021,'num':1,'type':4}]}, {'sid':1}]:
            await self.denied(equipment.upgrade,f)
        self.wire(await equipment.equip_up(self.ctx,{'equip_id':2,'target_card_id':1}))
        await self.denied(equipment.upgrade,{'sid':1,'equip_ids':[2]})
    async def test_encode_failure_rolls_back_consumption_and_card_ownership(self):
        before=self.state();revision=self.revision();original=self.codec.encode_frame
        def fail(name,fields):
            if name in ('EquipProto:EquipUpRet','EquipProto:EquipUpgradeRet'):
                raise CodecError('injected wire failure')
            return original(name,fields)
        with patch.object(self.codec,'encode_frame',side_effect=fail):
            with self.assertRaises(CodecError):
                await equipment.upgrade(self.ctx,{'sid':1,'equip_ids':[2]})
            with self.assertRaises(CodecError):
                await equipment.equip_up(self.ctx,{'equip_id':1,'target_card_id':1})
        self.assertEqual(self.state(),before);self.assertEqual(self.revision(),revision)
    async def test_authentication_and_corrupt_persisted_ownership_fail_explicitly(self):
        self.ctx.logged_in=False
        with self.assertRaises(StorageError):await equipment.upgrade(self.ctx,{'sid':1,'equip_ids':[2]})
        self.ctx.logged_in=True;self.ctx.role='query'
        with self.assertRaises(StorageError):await equipment.equip_up(self.ctx,{'equip_id':1,'target_card_id':1})
        self.ctx.role='game'
        with self.store.transaction(self.uid) as tx:tx.state['equips'][0]['card_id']=999
        with self.assertRaises(StorageError):service.get_equips(self.ctx)
    async def test_strengthening_survives_restart_and_repeated_consumed_sid_cannot_charge(self):
        self.wire(await equipment.upgrade(self.ctx,{'sid':1,'equip_ids':[2]}))
        before=self.state();self.store.close();self.store=Store(self.path);self.ctx.server.store=self.store
        self.assertEqual(self.state(),before)
        await self.denied(equipment.upgrade,{'sid':1,'equip_ids':[2]})

    async def test_large_inventory_finishes_only_after_all_wire_batches(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['equips']=[self.chip(i) for i in range(1,502)]
            tx.state['max_equip_size']=600
        replies=service.get_equips(self.ctx);self.wire(replies)
        self.assertEqual([r.name for r in replies],['EquipProto:EquipAdd','EquipProto:EquipAdd','EquipProto:GetEquipsRet'])
        self.assertEqual(sum(len(r.fields['equips']) for r in replies),501)
        self.assertTrue(all(r.fields['cur_size']==501 for r in replies))
        self.assertEqual(replies[-1].fields['materialNum'],0)

    async def test_future_rewards_start_at_source_level_zero_without_migrating_owned_chips(self):
        from handlers import progression
        with self.store.transaction(self.uid) as tx:
            tx.state['equips'][0]['level']=2
            tx.state['next_equip_id']=4
            item=progression.add_equip(tx,2020101)
            self.assertEqual(item['level'],0)
            self.assertEqual(item['exp'],0)
        state=self.state()
        self.assertEqual(state['equips'][0]['level'],2)
        self.assertEqual(state['equips'][-1]['level'],0)
        self.assertEqual(state['equips'][-1]['sid'],4)
        self.wire(service.get_equips(self.ctx))
