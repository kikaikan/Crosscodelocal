"""Skin source coverage, atomic ownership/selection and durable receipt tests."""
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
from server_core import Context,HANDLERS
from handlers import shop,skins
import skins_service as service
from protocol_codec import IVProtoCodec,WireConfig,CodecError

class SkinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"players.sqlite3"
        self.store=Store(self.path)
        self.seed=json.loads((HERE/"data/new_account_seed.json").read_text("utf-8"))
        self.uid=self.store.create_account("skin-local-test",self.seed)["uid"]
        self.codec=IVProtoCodec(json.loads((HERE.parent/"05-protocol/endpoints.json").read_text("utf-8")),
                                WireConfig("little",max_frame_size=65535))
        self.ctx=Context(SimpleNamespace(store=self.store,codec=self.codec),"game",self.uid,True)
        with self.store.transaction(self.uid) as tx:
            tx.state["progress"]["cleared_stages"]=[1001,1002]
            tx.state["offline_clock"]=1791093600
            tx.add_currency("diamond",2_000_000)
            tx.add_currency("gold",2_000_000)
            tx.add_item(10066,2_000_000)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def state(self):
        return self.store.get_player(self.uid)
    def fields(self,identifier,**extra):
        return {"id":identifier,"buy_time":self.state()["offline_clock"],**extra}
    def wire(self,replies):
        for reply in replies:
            if reply.name == "PlayerProto:CardUpdate":
                self.assertEqual(reply.fields["store_exp"],self.state()["store_exp"])
            raw=self.codec.encode_frame(reply.name,reply.fields)
            decoded=self.codec.decode_frame(raw)
            self.assertEqual(raw,self.codec.encode_frame(decoded.name,decoded.fields))
    async def unchanged(self,fn,fields,error=StorageError):
        before=self.state()
        revision=self.store.connection.execute("SELECT revision FROM accounts WHERE uid=?",(self.uid,)).fetchone()[0]
        if fn is skins.use_skin and self.ctx.logged_in and error is StorageError:
            replies=await fn(self.ctx,fields)
            self.wire(replies)
            self.assertEqual([r.name for r in replies],["SystemProto:Tips"])
            self.assertEqual(replies[0].fields["strId"],"GeneralTips")
            self.assertEqual(replies[0].fields["args"][0]["type"],0)
        else:
            with self.assertRaises(error):
                await fn(self.ctx,fields)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.store.connection.execute("SELECT revision FROM accounts WHERE uid=?",(self.uid,)).fetchone()[0],revision)
    def single(self,model):
        item=service.models()[str(model)]["item_id"]
        return next(p for p in service.products().values() if p["jGets"]==[[item,1,2]])
    def card(self,cfgid):
        with self.store.transaction(self.uid) as tx:
            tx.state["cards"][0]["cfgid"]=cfgid
            tx.state["cards"][0]["open_cards"]=[{"id":c["id"]} for c in service.catalog()["cards"].values() if c["role_id"] == service.card_cfg(cfgid)["role_id"]]
            tx.state["cards"][0]["skin"]=0
            tx.state["cards"][0]["skin_a"]=0
    def use(self,model,alt=False,l2d=1):
        return {"cid":1,"skin":0 if alt else model,"skin_a":model if alt else 0,
                "skinIsl2d":1 if alt else l2d,"skinIsl2d_a":l2d if alt else 1}

    def test_catalog_all_227_have_individual_price_and_local_overlay(self):
        data=service.catalog()
        self.assertEqual(data["coverage"]["source_models"],227)
        self.assertEqual(data["coverage"]["source_listed_models"],226)
        overlay=json.loads((HERE.parent/"06-client/offline-resources/skins-client-catalog.json").read_text("utf-8"))
        self.assertEqual(len(overlay["products"]),265)
        self.assertEqual(set(overlay["model_products"]),set(service.models()))
        self.assertEqual(len({p["id"] for p in overlay["products"]}),265)
        for model,meta in service.models().items():
            cfg=self.single(model)
            mapped=service.products()[overlay["model_products"][model]]
            self.assertEqual(mapped["jGets"],[[meta["item_id"],1,2]])
            self.assertEqual((cfg["group"],cfg["nType"],cfg["tabID"]),(4,3,4001))
            self.assertTrue(all(row[0]>0 and row[1]>=0 for row in cfg["jCosts"]))
        self.assertEqual(service.products()[50132]["jCosts"],[[10002,1380]])
        self.assertEqual(service.products()[710008]["jCosts"],[[10002,30]])
        self.assertEqual(service.products()[50025]["jCosts"],[[10002,0]])
        self.assertEqual(service.products()[38037002]["jCosts"],[[10001,100]])
        self.assertEqual(service.card_cfg(80370)["main_type"],4)
        self.assertTrue(any(v["excluded_source_rewards"] for v in data["policies"].values()
                            if "excluded_source_rewards" in v))
        for p in service.products().values():
            self.assertTrue(all(row[0] in service.item_models() for row in p["jGets"]))
            self.assertEqual(p["nSumBuyLimit"],1)
            self.assertTrue(all(r[0]>0 for r in p.get("orgCosts",[])))

    async def test_buy_real_currency_ownership_push_and_restart_retry(self):
        cfg=service.products()[50132]
        before=self.state()
        fields=self.fields(cfg["id"])
        replies=await shop.buy(self.ctx,fields)
        self.wire(replies)
        model=service.item_models()[cfg["jGets"][0][0]]["id"]
        self.assertIn(model,service.owned(self.state()))
        self.assertEqual(self.state()["player"]["diamond"],before["player"]["diamond"]-1380)
        self.assertNotIn(str(cfg["jGets"][0][0]),self.state()["inventory"])
        names=[r.name for r in replies]
        self.assertLess(names.index("PlayerProto:GetSkinsRet"),names.index("ShopProto:BuyRet"))
        self.assertTrue(any(row["is_add"] for r in replies if r.name=="PlayerProto:GetSkinsRet"
                            for row in r.fields["info"]))
        self.assertEqual(replies[-1].fields["info"]["can_buy_cnt"],0)
        after=self.state()
        revision=self.store.connection.execute("SELECT revision FROM accounts WHERE uid=?",(self.uid,)).fetchone()[0]
        self.store.close()
        self.store=Store(self.path);self.ctx.server.store=self.store
        retry=await shop.buy(self.ctx,fields);self.wire(retry)
        self.assertEqual(retry[-1].fields["gets"],[])
        self.assertEqual(self.state(),after)
        self.assertEqual(self.store.connection.execute("SELECT revision FROM accounts WHERE uid=?",(self.uid,)).fetchone()[0],revision)
        await self.unchanged(shop.buy,self.fields(cfg["id"],buy_time=fields["buy_time"]+1))
        self.wire(await skins.get_skins(self.ctx,{"cfgid":0}))

    async def test_failure_balance_and_wire_encoding_roll_back_everything(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_currency("diamond",-tx.state["player"]["diamond"])
        await self.unchanged(shop.buy,self.fields(50132))
        with self.store.transaction(self.uid) as tx:
            tx.add_currency("diamond",2000)
        original=self.codec.encode_frame
        def fail(name,fields):
            if name=="PlayerProto:GetSkinsRet":
                raise CodecError("injected skin wire failure")
            return original(name,fields)
        with patch.object(self.codec,"encode_frame",side_effect=fail):
            await self.unchanged(shop.buy,self.fields(50132),CodecError)
        with patch.object(service,"award",side_effect=StorageError("injected award failure")):
            await self.unchanged(shop.buy,self.fields(50132))

    async def test_free_skin_claim_once_and_count_tampering(self):
        before=self.state()["player"]["diamond"]
        result=await shop.buy(self.ctx,{"id":50025})
        self.wire(result)
        self.assertEqual(self.state()["player"]["diamond"],before)
        after=self.state()
        self.assertEqual((await shop.buy(self.ctx,{"id":50025}))[-1].fields["gets"],[])
        self.assertEqual(self.state(),after)
        await self.unchanged(shop.buy,{"id":50025,"buy_sum":2})
        await self.unchanged(shop.buy,self.fields(50132,buy_sum=2))

    async def test_every_source_model_can_buy_select_and_wire_roundtrip(self):
        # Temporary account only; validates base/alternate/special mechanical forms.
        for model,meta in service.models().items():
            cfg=self.single(model)
            replies=await shop.buy(self.ctx,self.fields(cfg["id"]))
            self.wire(replies)
            self.card(meta["select_card_id"])
            card_cfg=service.card_cfg(meta["select_card_id"])
            alt=card_cfg.get("base_card") is not True
            try:
                selected=await skins.use_skin(self.ctx,self.use(int(model),alt=alt))
            except StorageError as error:
                raise AssertionError(f"Selection model={model}, card={meta['card_id']}, cfg={card_cfg}: {error}") from error
            self.wire(selected)
            self.assertEqual(self.state()["cards"][0]["skin_a" if alt else "skin"],int(model))
        self.assertEqual(len(service.owned(self.state())),227)
        self.assertTrue(all(str(m["item_id"]) not in self.state()["inventory"]
                            for m in service.models().values()))
        self.wire(await skins.get_skins(self.ctx,{"cfgid":0}))

    async def test_select_unowned_wrong_family_invalid_live2d_and_partial_failure(self):
        meta=service.models()["8037002"]
        await self.unchanged(skins.use_skin,self.use(meta["id"]))
        await shop.buy(self.ctx,self.fields(38037002))
        await self.unchanged(skins.use_skin,self.use(meta["id"]))
        self.card(80370)
        await self.unchanged(skins.use_skin,self.use(meta["id"],l2d=2))
        fields=self.use(meta["id"]);fields["skin_a"]=1001003
        await self.unchanged(skins.use_skin,fields)
        self.wire(await skins.use_skin(self.ctx,self.use(meta["id"])))
        self.store.close();self.store=Store(self.path);self.ctx.server.store=self.store
        self.assertEqual(self.state()["cards"][0]["skin"],meta["id"])
        self.wire(await skins.use_skin(self.ctx,self.use(0)))

    async def test_alternate_slot_live2d_flag_normalized_for_unset_skin_a(self):
        # Real card 73/cfgid 78020 (卡提那·联域): the player picks skin 7802003
        # (尼克罗假日, has l2dName) and turns the dynamic toggle on. The client
        # mirrors that single switch into both slots (RoleApparel.lua:346), but
        # skin_a is nil and therefore never sent, so it defaults to 0 whose
        # resolved base model 7802001 has no l2dName.
        skin=service.models()["7802003"]
        await shop.buy(self.ctx,self.fields(self.single(skin["id"])["id"]))
        self.card(78020)
        replies=await skins.use_skin(self.ctx,{"cid":1,"skin":7802003,"skinIsl2d":2,"skinIsl2d_a":2})
        self.wire(replies)
        self.assertEqual([r.name for r in replies],["PlayerProto:CardUpdate"])
        card=self.state()["cards"][0]
        self.assertEqual((card["skin"],card["skinIsl2d"]),(7802003,2))
        self.assertEqual((card["skin_a"],card["skinIsl2d_a"]),(0,1))
        # A genuine L2D alternate selection keeps flag 2 untouched.
        replies=await skins.use_skin(self.ctx,{"cid":1,"skin":0,"skinIsl2d":1,
                                               "skin_a":7802003,"skinIsl2d_a":2})
        self.wire(replies)
        self.assertEqual([r.name for r in replies],["PlayerProto:CardUpdate"])
        card=self.state()["cards"][0]
        self.assertEqual((card["skin_a"],card["skinIsl2d_a"]),(7802003,2))

    async def test_primary_slot_live2d_flag_still_rejected_on_non_l2d_model(self):
        # The alternate normalization must not weaken the primary slot: base
        # model 7802001 has no l2dName, so asking for L2D there is still refused.
        skin=service.models()["7802003"]
        await shop.buy(self.ctx,self.fields(self.single(skin["id"])["id"]))
        self.card(78020)
        await self.unchanged(skins.use_skin,{"cid":1,"skin":0,"skinIsl2d":2,"skinIsl2d_a":1})

    async def test_paired_package_rolls_back_if_one_already_owned(self):
        cfg=service.products()[50152]
        first=service.item_models()[cfg["jGets"][0][0]]
        await shop.buy(self.ctx,self.fields(self.single(first["id"])["id"]))
        await self.unchanged(shop.buy,self.fields(cfg["id"]))
        second=service.item_models()[cfg["jGets"][1][0]]
        self.wire(await shop.buy(self.ctx,self.fields(self.single(second["id"])["id"])))
        self.assertIn(second["id"],service.owned(self.state()))

    async def test_existing_loan_deadline_preserved_purchase_and_expiry(self):
        a=service.models()["1001003"]
        b=service.models()["1001004"]
        stamp=self.state()["offline_clock"]
        with self.store.transaction(self.uid) as tx:
            tx.state["skins"]=[{"cfgid":a["card_id"],"info":[],"is_add":False,
                               "ltSkins":[{"id":a["id"],"t":stamp+1000,"nTime":stamp-50,"is_add":False}]}]
        self.assertIn(a["id"],service.owned(self.state()))
        self.assertNotIn(a["id"],service.owned(self.state(),False))
        self.wire(await shop.buy(self.ctx,self.fields(self.single(b["id"])["id"])))
        self.assertEqual(service.temporary(self.state())[a["id"]]["t"],stamp+1000)
        self.card(a["select_card_id"])
        self.wire(await skins.use_skin(self.ctx,self.use(a["id"])))
        with self.store.transaction(self.uid) as tx:
            tx.state["offline_clock"]=stamp+1001
        result=await skins.skin_expired(self.ctx,{})
        self.wire(result)
        self.assertEqual(result[-1].fields["ids"],[a["id"]])
        self.assertNotIn(a["id"],service.owned(self.state()))
        self.assertIn(b["id"],service.owned(self.state()))
        self.assertEqual(self.state()["cards"][0]["skin"],0)

    async def test_selection_encode_failure_rolls_back_card_and_owned_history(self):
        a=service.models()["1001003"]
        await shop.buy(self.ctx,self.fields(self.single(a["id"])["id"]))
        self.card(a["select_card_id"])
        original=self.codec.encode_frame
        def fail(name,fields):
            if name=="PlayerProto:CardUpdate":
                raise CodecError("injected selection wire failure")
            return original(name,fields)
        with patch.object(self.codec,"encode_frame",side_effect=fail):
            await self.unchanged(skins.use_skin,self.use(a["id"]),CodecError)

    async def test_skin_requests_auth_and_source_query_family(self):
        self.ctx.logged_in=False
        for fn,fields in ((skins.get_skins,{"cfgid":0}),(skins.use_skin,self.use(0)),(skins.skin_expired,{})):
            await self.unchanged(fn,fields)
        self.ctx.logged_in=True
        await self.unchanged(skins.get_skins,{"cfgid":99999999})
        self.assertIs(HANDLERS["PlayerProto:GetSkins"],skins.get_skins)
        self.wire(await skins.skin_expired(self.ctx,{}))

    async def test_owned_role_base_skin_break_level_is_repaired_on_login(self):
        from card_roles_service import synchronize_break_levels
        state = self.state()
        role_id = service.card_cfg(state["cards"][0]["cfgid"])["role_id"]
        with self.store.transaction(self.uid) as tx:
            role = next(row for row in tx.state["card_roles"] if row["id"] == role_id)
            role["data"]["b_lv"] = 0
            changed = synchronize_break_levels(tx.state)
        self.assertEqual([row["id"] for row in changed], [role_id])
        role = next(row for row in self.state()["card_roles"] if row["id"] == role_id)
        self.assertEqual(role["data"]["b_lv"], self.state()["cards"][0]["break_level"])
        with self.store.transaction(self.uid) as tx:
            role = next(row for row in tx.state["card_roles"] if row["id"] == role_id)
            role["data"]["b_lv"] = 7
            self.assertEqual(synchronize_break_levels(tx.state), [])
        role = next(row for row in self.state()["card_roles"] if row["id"] == role_id)
        self.assertEqual(role["data"]["b_lv"], 7)

    async def test_paired_fit_result_skin_is_selectable_for_every_family(self):
        # 同调/形切 pairings (RoleTool.GetBDSkin_a: fit_result/tTransfo/召唤) put a
        # sibling card's skin into skin_a. All 14 combinations that the old
        # changeCardIds-only candidate list permanently refused must now round-trip
        # as PlayerProto:CardUpdate and persist both slots verbatim.
        combos=[(50040,5004003,5004103),(50040,5004005,5004105),
                (50010,5001003,5001102),(70051,7005102,7005005),
                (70051,7005103,7005006),(30430,3043003,3043102),
                (30480,3048003,3048102),(30480,3048004,3048104),
                (60321,6032103,6032003),(60300,6030003,6030103),
                (60300,6030004,6030104),(70050,7005005,7005102),
                (70050,7005006,7005103),(60320,6032003,6032103)]
        self.assertEqual(len(combos),14)
        for cfgid,skin,paired in combos:
            await shop.buy(self.ctx,self.fields(self.single(skin)["id"]))
            self.card(cfgid)
            replies=await skins.use_skin(self.ctx,{"cid":1,"skin":skin,"skinIsl2d":1,
                                                   "skin_a":paired,"skinIsl2d_a":1})
            self.wire(replies)
            self.assertEqual([r.name for r in replies],["PlayerProto:CardUpdate"],
                             f"cfgid={cfgid} skin={skin} paired={paired}")
            card=self.state()["cards"][0]
            self.assertEqual((card["skin"],card["skin_a"]),(skin,paired),
                             f"cfgid={cfgid} skin={skin} paired={paired}")

    async def test_paired_slot_relaxation_stays_family_scoped_and_primary_gated(self):
        # skin_a may show the client-derived paired display model without its own
        # purchase, but the relaxation stays inside the card family and the primary
        # slot remains purchase-gated.
        await shop.buy(self.ctx,self.fields(self.single(5004003)["id"]))
        self.card(50040)
        await self.unchanged(skins.use_skin,{"cid":1,"skin":5004003,"skinIsl2d":1,
                                             "skin_a":1001003,"skinIsl2d_a":1})
        await self.unchanged(skins.use_skin,{"cid":1,"skin":5004005,"skinIsl2d":1,
                                             "skin_a":5004103,"skinIsl2d_a":1})
        # Paired skin_a accepted although only the primary skin was bought.
        self.wire(await skins.use_skin(self.ctx,{"cid":1,"skin":5004003,"skinIsl2d":1,
                                                 "skin_a":5004103,"skinIsl2d_a":1}))
        # Explicit base models (card 50040/50041 intrinsic forms) still reset.
        self.wire(await skins.use_skin(self.ctx,{"cid":1,"skin":5004001,"skinIsl2d":1,
                                                 "skin_a":5004101,"skinIsl2d_a":1}))
        card=self.state()["cards"][0]
        self.assertEqual((card["skin"],card["skin_a"]),(5004001,5004101))
        # model==0 is stored as 0 (its l2d metadata still resolves from
        # candidates[0], primary card 50040's model 5004001) and must not raise.
        self.wire(await skins.use_skin(self.ctx,self.use(0)))
        card=self.state()["cards"][0]
        self.assertEqual((card["skin"],card["skin_a"]),(0,0))

if __name__=="__main__":
    unittest.main()
