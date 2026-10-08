"""The recovered 'T[]' field spelling must be readable, not a fatal frame.

sFurniture.childID is the only such field (05-protocol/endpoints.json). The client
writes it when furniture is stacked (03-unpack/lua/device-luascripts/DormFurniture.lua:668-671),
and a decode error closes the connection before the request ever reaches a handler.
"""
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(ROOT / '02-tools' / 'scripts'))
from protocol_codec import IVProtoCodec, WireConfig, bracket_array

SCHEMA = json.loads((ROOT / '05-protocol' / 'endpoints.json').read_text(encoding='utf-8'))
CODEC = IVProtoCodec(SCHEMA, WireConfig('little'))

def furniture(identifier, child_ids=None):
    row = {'id': identifier, 'point': {'x': 0.5, 'y': 1.25, 'z': -2.0},
           'planeType': 1, 'rotateY': 90, 'parentID': 0}
    if child_ids is not None:
        row['childID'] = child_ids
    return row

class BracketArrayTests(unittest.TestCase):
    def test_alias_maps_only_the_bracket_spelling(self):
        self.assertEqual(bracket_array('int[]'), 'array|int')
        self.assertEqual(bracket_array('uint[]'), 'array|uint')
        self.assertEqual(bracket_array('map|sFurniture|id'), 'map|sFurniture|id')
        self.assertEqual(bracket_array('array|byte'), 'array|byte')

    def test_schema_declares_the_bracket_field(self):
        fields = {field['name']: field['type'] for field in
                  next(row for row in SCHEMA['schemas'] if row['name'] == 'sFurniture')['fields']}
        self.assertEqual(fields['childID'], 'int[]')

    def test_synthetic_value_covers_the_bracket_field(self):
        from protocol_codec import synthetic_struct
        schemas = {row['name']: row for row in SCHEMA['schemas']}
        self.assertEqual(synthetic_struct('sFurniture', schemas)['childID'], [-123456])

    def test_stacked_furniture_request_round_trips_byte_exact(self):
        payload = {'id': 101, 'img': 'theme_01.png',
                   'furnitures': {5: furniture(5, [6, 7]), 6: furniture(6), 7: furniture(7)}}
        raw = CODEC.encode_frame('DormProto:ModFurniture', payload)
        frame = CODEC.decode_frame(raw)
        self.assertEqual(frame.name, 'DormProto:ModFurniture')
        self.assertEqual(CODEC.encode_frame(frame.name, frame.fields), raw)
        rows = frame.fields['furnitures']
        self.assertEqual(rows[5]['childID'], [6, 7])
        self.assertNotIn('childID', rows[6])

    def test_reply_with_bracket_field_round_trips_byte_exact(self):
        payload = {'builds': [], 'is_finish': True}
        raw = CODEC.encode_frame('BuildingProto:BuildsListRet', payload)
        self.assertEqual(CODEC.decode_frame(raw).name, 'BuildingProto:BuildsListRet')

if __name__ == '__main__':
    unittest.main()
