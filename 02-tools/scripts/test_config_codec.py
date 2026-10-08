"""Meaningful offline regression and rejection tests for the config decoder."""

from pathlib import Path
import unittest

import config_codec as codec


class ConfigCodecTests(unittest.TestCase):
    def test_document_known_sample(self):
        encrypted = "s1e1XTxW7JyRbmpeldJCTPsEvyJWmcclRTJdPi9FQf=="
        expected = b'{[1]={["index"]=1,["order"]=1}}'
        self.assertEqual(codec.decode_payload(encrypted), expected)
        self.assertEqual(codec.encode_payload(expected), encrypted)
        parsed = codec.parse_lua_table(expected.decode("utf-8"))
        self.assertEqual(parsed["$lua_table"][0]["key"], 1)
        self.assertEqual(parsed["$lua_table"][0]["value"]["$lua_table"][0], {"key": "index", "value": 1})

    def test_file_bom_and_unicode_round_trip(self):
        plain = '{message="交错战线", enabled=true}'.encode("utf-8")
        for bom in (False, True):
            raw = codec.encode_file_bytes(plain, bom)
            self.assertEqual(codec.decode_file_bytes(raw), (plain, bom))

    def test_permutation_is_involution_at_all_short_lengths(self):
        for length in range(257):
            original = "".join(chr(33 + i % 90) for i in range(length))
            self.assertEqual(codec.swap_characters(codec.swap_characters(original)), original)

    def test_strict_base64_rejections(self):
        for restored in ("Zg==\n", "Zg===", "Zg", "Zh==", "Zg==junk", "@!=="):
            with self.subTest(restored=restored), self.assertRaises(ValueError):
                codec.decode_payload(codec.swap_characters(restored))
        with self.assertRaises(ValueError):
            codec.decode_payload("非ASCII")

    def test_parse_data_types_and_scalar_keys(self):
        result = codec.parse_lua_table('{[1]="one", [true]=false, named={2,3}, ["nilvalue"]=nil;}')
        entries = result["$lua_table"]
        self.assertEqual(entries[0], {"key": 1, "value": "one"})
        self.assertEqual(entries[1], {"key": True, "value": False})
        self.assertEqual(entries[2]["value"]["$lua_table"], [{"key": 1, "value": 2}, {"key": 2, "value": 3}])
        self.assertIsNone(entries[3]["value"])

    def test_reject_executable_or_ambiguous_lua(self):
        dangerous = (
            '{x=os.execute("anything")}', '{x=function() end}', '{x=1+2}',
            '{}; os.execute("anything")', '{[nil]=1}', '{[{}]=1}',
            '{[1]=2,[1.0]=3}', '{x=1,x=2}', '{--comment\nx=1}',
            '{x=1e999}', '{x="unterminated}', 'return {}', '{x=[[long string]]}',
        )
        for text in dangerous:
            with self.subTest(text=text), self.assertRaises(ValueError):
                codec.parse_lua_table(text)

    def test_parser_resource_limits(self):
        with self.assertRaises(ValueError):
            codec.LuaDataParser('{{{{1}}}}', max_depth=2).parse()
        with self.assertRaises(ValueError):
            codec.LuaDataParser('{1,2,3,4}', max_nodes=3).parse()
        with self.assertRaises(ValueError):
            codec.parse_lua_table('{x=' + '1' * 65 + '}')

    def test_device_samples_byte_exact_round_trip(self):
        root = Path(__file__).resolve().parents[2]
        samples = sorted((root / "01-device" / "files").glob("*config*.txt"))
        self.assertGreater(len(samples), 0, "Device samples required for regression")
        for sample in samples:
            with self.subTest(sample=sample.name):
                raw = sample.read_bytes()
                plain, has_bom = codec.decode_file_bytes(raw)
                parsed = codec.parse_lua_table(plain.decode("utf-8"))
                self.assertTrue(parsed["$lua_table"])
                self.assertEqual(codec.encode_file_bytes(plain, has_bom), raw)

    def test_complete_local_server_list(self):
        root = Path(__file__).resolve().parents[2]
        raw = (root / "01-device" / "sl.json").read_bytes()
        plain, has_bom = codec.decode_file_bytes(raw)
        result = codec.parse_server_list(plain)
        self.assertEqual(result["code"], 0)
        self.assertGreater(len(result["data"]), 0)
        self.assertEqual(codec.encode_file_bytes(plain, has_bom), raw)

    def test_server_list_reject_invalid_json_and_schema(self):
        for plain in (
            b'{"code":0,"code":1,"data":[]}', b'{"code":NaN,"data":[]}',
            b'{"code":0,"data":[]}', b'{"code":0,"data":[{"id":true}]}',
        ):
            with self.subTest(plain=plain), self.assertRaises(ValueError):
                codec.parse_server_list(plain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
