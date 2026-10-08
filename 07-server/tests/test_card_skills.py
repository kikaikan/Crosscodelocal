"""Owned-card skill types: the client filters its skill panel by sSkillData.type.

Evidence: cfgskill.lua carries SkillMainType in 'main_type', and the official
PlayerProto:CardAdd frames (05-protocol/samples/tcp/session1-stream02-s2c-frame0092..0097)
send {id, exp, type} with type == main_type.  Without it CharacterCardsData:GetSkillsForShow
returns nothing, the 武装技能 panel is empty and RoleInfo.lua:833 throws when the skill
button is pressed.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path
import re
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
from database import StorageError
from server_core import load_dependencies
from card_roles_service import (build_card_skills, card_skill_type, skill_types,
                                synchronize, synchronize_card_skill_types)
import reply_chunks

CODEC, SEED = load_dependencies(ROOT / "05-protocol" / "endpoints.json",
                                SERVER / "data" / "new_account_seed.json")
LUA = ROOT / "03-unpack" / "lua" / "device-luascripts"
OFFICIAL = ROOT / "05-protocol" / "samples" / "tcp"
# (skill id, type) taken verbatim from the decoded official CardAdd frames.
OFFICIAL_TYPE_SAMPLES = {500100401: 3, 500101304: 1, 4500104: 2, 700501303: 1,
                         4304903: 2, 301200201: 1}


def source_main_types():
    """main_type per id, parsed from the client skill table the same way the generator does."""
    text = (LUA / "cfgskill.lua").read_text(encoding="utf-8-sig")
    rows = {}
    for match in re.finditer(r"\[(\d+)\]=\{", text):
        start = match.end() - 1
        depth, position = 0, start
        while position < len(text):
            char = text[position]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            position += 1
        found = re.search(r'\["main_type"\]\s*=\s*(-?\d+)', text[start:position + 1])
        if found:
            rows[int(match.group(1))] = int(found.group(1))
    return rows


class CardSkillTypeTests(unittest.TestCase):
    def test_generated_map_matches_the_official_card_add_frames(self):
        rows = skill_types()
        for skill, expected in OFFICIAL_TYPE_SAMPLES.items():
            self.assertEqual(rows.get(skill), expected)
        # Every forwarded value must also exist in the client table with the same value.
        client = source_main_types()
        self.assertTrue(client)
        for skill in OFFICIAL_TYPE_SAMPLES:
            self.assertEqual(client[skill], rows[skill])
        self.assertEqual(len(rows), len(client))
        self.assertGreater(len(rows), 8000)  # the table ships thousands of skills

    def test_build_card_skills_omits_type_for_ids_missing_from_the_client_table(self):
        built = build_card_skills([710100101, 4710101])
        self.assertEqual(built["710100101"], {"id": 710100101, "exp": 0, "type": 1})
        self.assertEqual(built["4710101"], {"id": 4710101, "exp": 0, "type": 2})
        # cfgCardData.lua card 30210 lists skills that cfgskill.lua does not contain; sending a
        # type for them would make the client index a nil config.
        cut = build_card_skills([302100101, 4302101])
        self.assertEqual(cut["302100101"], {"id": 302100101, "exp": 0})
        self.assertIsNone(card_skill_type(302100101))
        with self.assertRaises(TypeError):
            build_card_skills([None])

    def test_legacy_save_is_repaired_without_overwriting_unknown_ids(self):
        state = {"cards": [
            {"cfgid": 71010, "skills": {"710100101": {"id": 710100101, "exp": 5}}},
            {"cfgid": 30210, "skills": {"302100101": {"id": 302100101, "exp": 0, "type": 1}}},
        ]}
        changed = synchronize_card_skill_types(state)
        self.assertEqual(len(changed), 2)
        self.assertEqual(state["cards"][0]["skills"]["710100101"],
                         {"id": 710100101, "exp": 5, "type": 1})
        self.assertEqual(state["cards"][1]["skills"]["302100101"], {"id": 302100101, "exp": 0})
        # Idempotent: a second pass has nothing left to repair.
        self.assertEqual(synchronize_card_skill_types(state), [])

    def test_malformed_skill_entries_are_rejected(self):
        for broken in ({"cards": [{"skills": []}]},
                       {"cards": [{"skills": {"1": 7}}]},
                       {"cards": [{"skills": {"1": {"exp": 0}}}]}):
            with self.assertRaises(StorageError):
                synchronize_card_skill_types(broken)

    def test_legacy_cards_are_repaired_before_they_reach_the_card_add_wire(self):
        # data/new_account_seed.json was generated before this repair existed, and any save
        # written by the old build has the same shape; the login-time repair is what makes
        # the wire payload correct, so the test starts from the legacy shape.
        cards = deepcopy(SEED["cards"])
        for card in cards:
            for entry in card["skills"].values():
                entry.pop("type", None)
        self.assertTrue(synchronize_card_skill_types({"cards": cards}))
        for card in cards:
            for entry in card["skills"].values():
                expected = card_skill_type(entry["id"])
                if expected is None:
                    self.assertNotIn("type", entry)
                else:
                    self.assertEqual(entry.get("type"), expected)
        frames = reply_chunks.card_add(CODEC, cards, len(cards), SEED["max_card_size"])
        self.assertTrue(frames)
        decoded = []
        for frame in frames:
            raw = CODEC.encode_frame(frame.name, frame.fields)
            messages, tail = CODEC.decode_stream(raw)
            self.assertFalse(tail)
            decoded.extend(messages[0].fields["cards"])
        self.assertTrue(decoded)
        typed = [entry for card in decoded for entry in card["skills"].values() if "type" in entry]
        self.assertTrue(typed)
        for entry in typed:
            self.assertEqual(entry["type"], card_skill_type(entry["id"]))

    def test_official_frames_are_still_readable_with_this_codec(self):
        """The mapping is only trustworthy while the captured frames decode to the same shape."""
        frame = OFFICIAL / "session1-stream02-s2c-frame0092.decoded.json"
        sample = json.loads(frame.read_text(encoding="utf-8"))
        self.assertEqual(sample["name"], "PlayerProto:CardAdd")
        skills = sample["fields"]["cards"][0]["skills"]
        self.assertTrue(skills)
        for entry in skills.values():
            self.assertEqual(set(entry), {"id", "exp", "type"})
            self.assertEqual(entry["type"], skill_types()[entry["id"]])


if __name__ == "__main__":
    unittest.main()
