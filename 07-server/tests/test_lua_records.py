"""Lua record extraction reads long-bracket strings from real config tables.

Live incident 2026-10-05 11:33: keyed_config('cfgskill.lua', 302001301) raised
"Selected record contains an unsupported Lua long string", so the whole
one-key talent batch was rejected. These tests pin the parser layer (every
record of the real table), not a hardcoded skip for a few ids.
"""
from pathlib import Path
import re
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import equip_service
from config_codec import parse_lua_table
from handlers import shop, tasks
from handlers.cards_items import keyed_config
from seed_generator import (LUA_DIR, balanced_table, normalize_long_strings,
                            python_data, selected_record)

CFGSKILL = "cfgskill.lua"
CFGSKILL_RECORDS = 8951
# Skill ids owned by the live save (uid 900000002) whose records the pre-fix
# extractor rejected with "unsupported Lua long string"; all 28 failed before.
PRE_FIX_REJECTED = (
    4201401, 4201501, 4402001, 4500201, 100100201, 100200201, 100200301,
    100300201, 100301301, 100400201, 100400301, 100401301, 102400301,
    102401301, 200700201, 201100201, 201400301, 201401301, 302001301,
    302200301, 302201301, 305000201, 402200301, 600900201, 601100201,
    602800201, 703400301, 703401301,
)

_SOURCES = {}


def raw_source(filename):
    """Read one read-only client config once per test process."""
    if filename not in _SOURCES:
        _SOURCES[filename] = (LUA_DIR / filename).read_text("utf-8-sig")
    return _SOURCES[filename]


def keyed_records(filename):
    """Yield (key, index) for every keyed record in raw config text."""
    for match in re.finditer(r"\[(\d+)\]\s*=\s*\{", raw_source(filename)):
        yield int(match.group(1)), match.end() - 1


class LongStringLexingTests(unittest.TestCase):
    def test_long_bracket_levels_are_normalized(self):
        cases = {
            '{s=[[plain {braces} and "quotes"]]}': 'plain {braces} and "quotes"',
            "{s=[=[one ]] two]=]}": "one ]] two",
            "{s=[==[ outer ]=] inner ]==]}": " outer ]=] inner ",
            "{s=[[\nfirst line\nsecond line]]}": "first line\nsecond line",
            "{a={s=[[nested } table]]}, b=2}": "nested } table",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                row = python_data(parse_lua_table(balanced_table(source, 0)))
                if "a" in row:
                    row = row["a"]
                self.assertEqual(row["s"], expected)

    def test_brackets_inside_ordinary_strings_are_untouched(self):
        row = python_data(parse_lua_table(balanced_table('{s="[[not a long string]]"}', 0)))
        self.assertEqual(row["s"], "[[not a long string]]")

    def test_values_after_a_long_string_are_still_read(self):
        row = python_data(parse_lua_table(balanced_table("{s=[[a]b]], next=7}", 0)))
        self.assertEqual((row["s"], row["next"]), ("a]b", 7))

    def test_unterminated_long_strings_are_still_rejected(self):
        for source in ("{s=[[never closed", "{s=[=[never closed}", "{s=[=[closed]=] , t=[["):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    balanced_table(source, 0)


class SharedNormalizerTests(unittest.TestCase):
    def test_every_reader_shares_one_long_string_implementation(self):
        self.assertIs(tasks.normalize_long_strings, normalize_long_strings)
        self.assertIs(shop.normalize_long_strings, normalize_long_strings)
        self.assertIs(equip_service.normalize_long_strings, normalize_long_strings)


class CfgSkillRecordTests(unittest.TestCase):
    def test_every_cfgskill_record_is_extracted(self):
        records = list(keyed_records(CFGSKILL))
        keys = [key for key, _ in records]
        self.assertEqual(len(keys), CFGSKILL_RECORDS)
        self.assertEqual(len(set(keys)), CFGSKILL_RECORDS)
        text = raw_source(CFGSKILL)
        for key, start in records:
            with self.subTest(key=key):
                row = python_data(parse_lua_table(balanced_table(text, start)))
                self.assertEqual(row["id"], key)

    def test_previously_rejected_live_save_skill_ids_now_extract(self):
        for skill_id in PRE_FIX_REJECTED:
            with self.subTest(skill_id=skill_id):
                record, evidence = selected_record(CFGSKILL, skill_id)
                self.assertEqual(record["id"], skill_id)
                self.assertEqual(evidence["key"], skill_id)
                self.assertEqual(len(evidence["file_sha256"]), 64)
                self.assertGreater(evidence["line"], 0)

    def test_keyed_config_reads_long_string_record_fields(self):
        record = keyed_config(CFGSKILL, 302001301)
        self.assertEqual(record["id"], 302001301)
        self.assertEqual(record["lv"], 1)
        self.assertEqual(record["next_id"], 302001302)
        self.assertEqual(record["main_type"], 1)
        text = raw_source(CFGSKILL)
        start = text.index("[302001301]={")
        literal = re.search(r'\["desc"\]=\[\[(.*?)\]\]', text[start:], re.S).group(1)
        self.assertEqual(record["desc"], literal.lstrip("\r\n"))
        self.assertIn("\n", record["desc"])


class OtherLongStringCatalogTests(unittest.TestCase):
    def test_shop_catalogs_with_long_strings_still_parse(self):
        for filename, minimum in (("cfgCfgCommodity.lua", 1300), ("cfgItemInfo.lua", 4000)):
            with self.subTest(filename=filename):
                rows = shop.catalog(filename)
                self.assertGreaterEqual(len(rows), minimum)
                self.assertTrue(all(isinstance(row, dict) and "id" in row for row in rows.values()))
                self.assertTrue(any(isinstance(value, str) and "\n" in value
                                    for row in rows.values() for value in row.values()))

    def test_equipment_records_with_long_strings_still_parse(self):
        for filename, key in (("cfgCfgEquip.lua", 2260402), ("cfgCfgEquipSkill.lua", 21307)):
            with self.subTest(filename=filename):
                row = equip_service.config_record(filename, key)
                self.assertEqual(row["id"], key)
                self.assertTrue(any(isinstance(value, str) and "\n" in value
                                    for value in row.values()))

    def test_card_data_record_with_long_string_still_parses(self):
        record, _ = selected_record("cfgCardData.lua", 906201)
        self.assertEqual(record["id"], 906201)


if __name__ == "__main__":
    unittest.main()
