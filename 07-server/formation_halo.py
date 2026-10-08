"""Default formation halos for direct SingleFight entry (Halo.lua:83-325).

Personal halo upgrades remain unsupported. This applies formation bonuses to a
fresh entry copy; owned card HP and previously saved encounters are unchanged.
"""
from copy import deepcopy
from functools import lru_cache
import math

from database import StorageError
from seed_generator import selected_record, first_array_record


@lru_cache(maxsize=512)
def source_record(filename, key):
    if filename not in ('cfgcfgHalo.lua', 'cfgcfgHaloCoordinate.lua'):
        raise StorageError('Unsupported formation halo source')
    try:
        record, _ = first_array_record(filename) if key == 1 else selected_record(filename, key)
        return record
    except (OSError, ValueError) as error:
        raise StorageError('Invalid formation halo source: ' + filename) from error


def indexed(table, key):
    if isinstance(table, list):
        return table[key - 1] if isinstance(key, int) and 1 <= key <= len(table) else None
    return table.get(key) if isinstance(table, dict) else None


def apply(cards, configs, levels, breaks, footprint):
    """Sum each emitter once across a recipient's footprint, excluding itself.

Halo:CalcAttr uses level/break base, before chip or sub-talent bonuses, for
attack/maxhp/defense. Other properties receive an additive bonus.
"""
    result = deepcopy(cards)
    emitters = []
    for card in result:
        data = card['data']
        cfg = configs[str(data['id'])]
        data['nClass'] = cfg.get('nClass', 0)
        if data.get('haloInfo'):
            raise StorageError('Personal halo upgrades are not reconstructed yet')
        ids = cfg.get('halo') or []
        if not ids:
            continue
        halo = source_record('cfgcfgHalo.lua', ids[0])
        if not halo:
            raise StorageError('Missing configured formation halo')
        attrs = indexed(halo['infos'], 1)
        offsets = indexed(halo.get('newCoorHalo'), ids[0])
        if not offsets:
            coordinate = source_record('cfgcfgHaloCoordinate.lua', attrs['coorId'])['coordinate']
            offsets = [[r - coordinate[0][0], c - coordinate[0][1]] for r, c in coordinate[1:]]
        cells = {(card['row'] + r, card['col'] + c) for r, c in offsets
                 if 1 <= card['row'] + r <= 3 and 1 <= card['col'] + c <= 3}
        emitters.append((data['id'], cells, halo.get('nClass') or [0], attrs))

    for card in result:
        data = card['data']
        cfg = configs[str(data['id'])]
        cells = footprint(cfg, card['row'], card['col'])
        percents, fixed = {}, {}
        for emitter_id, coverage, classes, attrs in emitters:
            if emitter_id == data['id'] or not cells.intersection(coverage):
                continue
            if 0 not in classes and data['nClass'] not in classes:
                continue
            for key, value in attrs.get('percents', {}).items():
                percents[key] = percents.get(key, 0) + value
            for key, value in attrs.get('fixedAttr', {}).items():
                fixed[key] = fixed.get(key, 0) + value
        if percents or fixed:
            card['bInHalo'] = True
        for key, value in percents.items():
            if key in ('attack', 'maxhp', 'defense'):
                if data.get('level') is not None and not data.get('isMonster'):
                    base = cfg[key] * levels[str(data['level'])].get(key, 1) * breaks[str(data['break_level'])].get(key, 1)
                    data[key] += math.floor(base * value)
                else:
                    data[key] = math.floor(data[key] * (1 + value))
            else:
                data[key] += value
        for key, value in fixed.items():
            data[{'attack_fixed': 'attack', 'maxhp_fixed': 'maxhp', 'defense_fixed': 'defense'}[key]] += value
        # Fresh direct encounters begin at full halo-adjusted HP.
        data['hp'] = data['maxhp']
    return result
