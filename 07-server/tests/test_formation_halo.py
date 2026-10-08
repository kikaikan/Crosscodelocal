"""Formation HP from real source configuration; no live-save mutations."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from handlers import battle
import formation_halo


class FormationHaloTests(unittest.TestCase):
    def test_source_five_member_direct_entry_and_snapshot_are_independent(self):
        positions = [(78020, 2, 1), (60150, 3, 1), (30500, 3, 2), (75020, 3, 3), (60160, 1, 3)]
        cards = []
        for i, (cfgid, row, col) in enumerate(positions, 1):
            card = {'cid': i, 'cfgid': cfgid, 'level': 90, 'break_level': 7, 'intensify_level': 1}
            cards.append(battle.fight_card({'player': {'uid': 1}}, card, row, col, i, 1, 0))
        raw = {'data': cards}
        before = deepcopy(raw)
        result = battle.entry_payload({'id': 1104, 'nGroupID': 101041}, 1, raw)
        self.assertEqual(raw, before)
        self.assertEqual([c['data']['maxhp'] for c in cards], [17279, 16599, 17970, 16763, 16423])
        self.assertEqual([c['data']['maxhp'] for c in result['data']['data']], [18488, 17760, 20485, 17936, 17572])
        self.assertEqual([c['data']['hp'] for c in result['data']['data']], [18488, 17760, 20485, 17936, 17572])
        self.assertEqual(result, battle.entry_payload({'id': 1104, 'nGroupID': 101041}, 1, raw))

    def test_multicell_overlap_deduplicates_each_emitter_and_filters_class_and_self(self):
        configs = {'10': {'maxhp': 100, 'nClass': 2, 'halo': [10]},
                   '20': {'maxhp': 100, 'nClass': 2, 'halo': [20]},
                   '30': {'maxhp': 100, 'nClass': 2, 'halo': [30]},
                   '40': {'maxhp': 100, 'nClass': 2, 'halo': [40]}}
        cards = [{'row': 1, 'col': i, 'data': {'id': i * 10, 'maxhp': 100, 'hp': 100,
                 'level': 1, 'break_level': 1}} for i in range(1, 5)]
        # Two valid emitters both cover BOTH cells of the recipient at (1,1).
        # Its own halo and a class-3-only emitter must not contribute.
        halos = {i: {'nClass': [3] if i == 40 else [0], 'infos': [{'percents': {'maxhp': .07}}],
                     'newCoorHalo': {i: [[0, 1 - i // 10], [1, 1 - i // 10]]}}
                 for i in (10, 20, 30, 40)}
        with patch.object(formation_halo, 'source_record', side_effect=lambda filename, key: halos[key]):
            result = formation_halo.apply(cards, configs, {'1': {}}, {'1': {}},
                lambda cfg, row, col: {(row, col), (row + 1, col)})
        self.assertEqual(result[0]['data']['maxhp'], 114)
        self.assertTrue(result[0]['bInHalo'])

    def test_percent_uses_unrounded_growth_base_before_personal_modifiers(self):
        configs = {'10': {'maxhp': 100.9, 'nClass': 0}, '20': {'nClass': 0, 'halo': [20]}}
        cards = [{'row': 1, 'col': 1, 'data': {'id': 10, 'maxhp': 150, 'hp': 150, 'level': 1, 'break_level': 1}},
                 {'row': 2, 'col': 1, 'data': {'id': 20, 'maxhp': 50, 'hp': 50}}]
        halo = {'infos': [{'percents': {'maxhp': .1}}], 'newCoorHalo': {20: [[-1, 0]]}}
        with patch.object(formation_halo, 'source_record', return_value=halo):
            result = formation_halo.apply(cards, configs, {'1': {'maxhp': 2}}, {'1': {}},
                lambda cfg, row, col: {(row, col)})
        self.assertEqual(result[0]['data']['maxhp'], 170)  # floor(100.9 * 2 * .1), not 15.
        self.assertEqual(result[1]['data']['maxhp'], 50)
        self.assertNotIn('bInHalo', result[1])
