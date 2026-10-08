"""Wire, calendar, login authorization and state isolation for initial reads."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import server_core
from server_core import HANDLERS
from database import StorageError
from handlers import initialization as init
from protocol_codec import IVProtoCodec, WireConfig


def stamp(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


_DEFAULT_CODEC = None


def wire_codec():
    """Shared codec for handler doubles: GetNewPanel now chunks the random snapshot."""
    global _DEFAULT_CODEC
    if _DEFAULT_CODEC is None:
        _DEFAULT_CODEC = IVProtoCodec(
            json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
            WireConfig("little", max_frame_size=65535))
    return _DEFAULT_CODEC


class FakeContext:
    def __init__(self, state, codec=None):
        self.state, self.uid, self.logged_in = state, 1, True
        self.server = SimpleNamespace(codec=codec or wire_codec())
        self.store = SimpleNamespace(get_player=lambda uid: deepcopy(self.state))
    def require_login(self):
        if not self.logged_in:
            raise StorageError("Local login required")
        return self.uid


class InitializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.codec = IVProtoCodec(json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
                                  WireConfig("little", max_frame_size=65535))
        self.owned = {name: handler for name, handler in HANDLERS.items() if handler.__module__ == init.__name__ and not name.startswith('PlayerProto:Set')}

    async def test_all_initial_reads_are_valid_wire_and_do_not_mutate_state(self):
        ctx = FakeContext(self.seed)
        before = deepcopy(ctx.state)
        with patch.object(init.time, "time", return_value=stamp("2026-10-04T12:00:00+08:00")):
            for request, handler in self.owned.items():
                with self.subTest(request=request):
                    replies = await handler(ctx, {})
                    self.assertTrue(replies)
                    for reply in replies:
                        raw = self.codec.encode_frame(reply.name, reply.fields)
                        frame = self.codec.decode_frame(raw)
                        self.assertEqual(raw, self.codec.encode_frame(frame.name, frame.fields))
        self.assertEqual(ctx.state, before)
        self.assertTrue(set(init.READS).issubset(self.owned))
        self.assertIn('ClientProto:GetSignInfo', self.owned)
        self.assertNotIn('PlayerProto:GetSkins', self.owned)  # Owned by the skin domain.

    async def test_every_read_requires_login(self):
        ctx = FakeContext(self.seed)
        ctx.logged_in = False
        for request, handler in self.owned.items():
            with self.subTest(request=request), self.assertRaises(StorageError):
                await handler(ctx, {})

    def test_config_gates_follow_progress_and_fail_closed(self):
        state = deepcopy(self.seed)
        self.assertFalse(init.feature_open(state, "ShopView"))
        self.assertFalse(init.feature_open(state, "GuildMenu"))
        self.assertFalse(init.feature_open(state, "not_a_known_feature"))
        state["progress"]["unlocked_functions"] = ["GuildMenu"]
        self.assertFalse(init.feature_open(state, "GuildMenu"))
        state["progress"]["cleared_stages"] = [1002, 1110]
        self.assertTrue(init.feature_open(state, "ShopView"))
        self.assertTrue(init.feature_open(state, "GuildMenu"))

    def test_legacy_offline_unlock_cannot_open_feature_gates(self):
        state = deepcopy(self.seed)
        state["offline_unlock_all"] = True
        before = deepcopy(state)
        self.assertFalse(init.feature_open(state, "MailView"))
        self.assertFalse(init.feature_open(state, "GuildMenu"))
        self.assertEqual(state, before)
        self.assertEqual(state["progress"]["cleared_stages"], [])

    def test_locked_read_cannot_leak_or_unlock_saved_domain_state(self):
        state = deepcopy(self.seed)
        state["initialization"] = {"GuildProto:GuildInfo": {"title": 1, "info": {"id": 99}}}
        fields = init.render_read(state, "GuildProto:GuildInfo", {})[0].fields
        self.assertEqual(fields, {"title": 0})
        state["progress"]["cleared_stages"] = [1110]
        fields = init.render_read(state, "GuildProto:GuildInfo", {})[0].fields
        self.assertEqual(fields["info"]["id"], 99)
        fields["info"]["id"] = 0
        self.assertEqual(state["initialization"]["GuildProto:GuildInfo"]["info"]["id"], 99)

    def test_reset_boundaries_at_beijing_three_and_next_month(self):
        before = init.reset_times(stamp("2026-10-01T02:59:59+08:00"))
        self.assertEqual(before["d_time"], stamp("2026-10-01T03:00:00+08:00"))
        self.assertEqual(before["m_time"], before["d_time"])
        after = init.reset_times(stamp("2026-10-01T03:00:00+08:00"))
        self.assertEqual(after["d_time"], stamp("2026-10-02T03:00:00+08:00"))
        self.assertEqual(after["m_time"], stamp("2026-11-01T03:00:00+08:00"))
        self.assertEqual(after["w_time"], stamp("2026-10-05T03:00:00+08:00"))

    def test_daily_sign_month_boundary_unclaimed_and_no_award(self):
        state = deepcopy(self.seed)
        before = init.sign_info(state, {}, stamp("2026-10-01T02:59:59+08:00"))
        after = init.sign_info(state, {}, stamp("2026-10-01T03:00:00+08:00"))
        old = next(reply for reply in before if reply.fields["id"] == 2026)
        new = next(reply for reply in after if reply.fields["id"] == 2026)
        self.assertEqual(old.fields["index"], 9)
        self.assertEqual(new.fields["index"], 10)
        self.assertEqual(new.fields["rewardsInfos"]["indexs"], {})
        self.assertNotIn("lastSingTime", new.fields["rewardsInfos"])
        self.assertTrue(after[-1].fields["is_end"])
        self.assertEqual(state, self.seed)
        self.assertNotIn("ClientProto:AddSign", self.owned)

    def test_mutations_payments_and_peer_handlers_are_not_registered(self):
        for request in ("ShopProto:Buy", "PlayerProto:PayReward", "ClientProto:AddSign",
                        "TaskProto:GetReward", "TaskProto:GetRewardByType", "PlayerProto:Setting",
                        "PlayerProto:GetClientData", "PlayerProto:SetClientData", "PlayerProto:CardCreate",
                        "PlayerProto:PlrPaneInfo", "ClientProto:InitFinish"):
            self.assertNotIn(request, self.owned)

    async def test_callback_required_nested_data_and_theme_completion(self):
        ctx = FakeContext(self.seed)
        reply = (await init.free_match_info(ctx, {}))[0]
        self.assertEqual(reply.fields["reward_info"]["get_rank_lv_id"], 0)
        themes = await init.dorm_themes(ctx, {"themeTypes": [1, 2]})
        self.assertEqual([r.fields["isFinish"] for r in themes], [False, True])
        with self.assertRaises(StorageError):
            await init.dorm_themes(ctx, {"themeTypes": list(range(17))})

    async def test_locked_colosseum_has_valid_schedule_and_empty_nested_run(self):
        ctx = FakeContext(self.seed)
        with patch.object(init.time, "time", return_value=stamp("2026-10-04T12:00:00+08:00")):
            reply = (await init.colosseum_season(ctx, {}))[0]
        self.assertIn(reply.fields["id"], init.config_table("cfgcfgColosseum.lua"))
        self.assertEqual(reply.fields["randModData"], {"randLvs": [], "selectCardData": {}, "isGet": False, "isOver": False})
        self.assertFalse(reply.fields["isRandPay"])
        self.assertEqual(reply.fields["freeCnt"], 0)
        raw = self.codec.encode_frame(reply.name, reply.fields)
        decoded = self.codec.decode_frame(raw)
        self.assertEqual(raw, self.codec.encode_frame(decoded.name, decoded.fields))
        ctx.state["colosseum"] = {str(reply.fields["id"]): {"isRandPay": True, "randModData": {"isOver": True, "isGet": False}}}
        with patch.object(init.time, "time", return_value=stamp("2026-10-04T12:00:00+08:00")):
            saved_locked = (await init.colosseum_season(ctx, {}))[0]
        self.assertFalse(saved_locked.fields["isRandPay"])
        self.assertFalse(saved_locked.fields["randModData"]["isOver"])

    async def test_closed_boss_push_initializes_timer_without_invalid_boss_id(self):
        replies = await init.world_boss_info(FakeContext(self.seed), {})
        self.assertEqual(replies[0].fields, {"list": []})
        self.assertEqual(replies[1].name, "FightProto:GlobalBossInfoRet")
        self.assertEqual(replies[1].fields, {"beginTime": 0, "endTime": 0, "hp": 0})
        decoded = self.codec.decode_frame(self.codec.encode_frame(replies[1].name, replies[1].fields))
        # Lua if proto.bossId must remain false: absent is safe, numeric 0 is true.
        self.assertNotIn("bossId", decoded.fields)

    async def test_expired_rich_man_keeps_valid_map_and_no_progress_or_rewards(self):
        ctx = FakeContext(self.seed)
        ctx.state["rich_man"] = {"1001": {"mapId": 0, "sort": 999, "throwCnt": 42, "eventList": [1]}}
        before = deepcopy(ctx.state)
        with patch.object(init.time, "time", return_value=stamp("2026-10-04T12:00:00+08:00")):
            reply = (await init.rich_man_info(ctx, {}))[0]
        self.assertEqual(reply.fields, {"cfgId": 1001, "mapId": 1001, "sort": 1, "throwCnt": 0, "eventList": []})
        cfg = init.config_table("cfgcfgMonopoly.lua")[reply.fields["cfgId"]]
        self.assertLess(cfg["nEndTime"], stamp("2026-10-04T12:00:00+08:00"))
        self.assertIn(reply.fields["mapId"], init.config_table("cfgcfgMonopolyGrid.lua"))
        self.assertEqual(ctx.state, before)

    async def test_single_commander_panel_uses_character_id_visible_top_slot(self):
        state = deepcopy(self.seed)
        state["login"].update(role_panel_id=71020, panel_id=7102001)
        replies = await init.panels(FakeContext(state), {})
        # The random-board snapshot leads: PlayerProto.lua:1377-1378 runs the
        # login rotation inside the GetNewPanelRet callback and reads it there.
        self.assertEqual(replies[0].name, "PlayerProto:GetRandomPanelRet")
        self.assertTrue(replies[0].fields["finish"])
        reply = next(item for item in replies if item.name == "PlayerProto:GetNewPanelRet")
        decoded = self.codec.decode_frame(self.codec.encode_frame(reply.name, reply.fields))
        panel = decoded.readable()["fields"]["panels"]["1"]
        self.assertEqual(panel["ids"], [7102001])
        self.assertEqual(panel["detail1"]["scale"], 1)
        self.assertTrue(panel["detail1"]["top"])
        self.assertFalse(panel["detail2"]["top"])


if __name__ == "__main__":
    unittest.main()
