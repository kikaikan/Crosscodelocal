"""Retain source tactical maps and reward closure without executing Lua."""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tactical_source_builder', HERE / 'gacha-build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

def main():
    stages = json.loads((HERE / 'progression-stages.json').read_text(encoding='utf-8'))
    groups = json.loads((HERE / 'battle-monster-groups.json').read_text(encoding='utf-8'))
    sources, maps, absent = [], {}, []
    wanted = set()
    for stage in stages.values():
        if stage.get('nGroupID') or stage.get('sub_type') == 1:
            continue
        name = 'Dungeon_' + str(stage['id']) + '.lua'
        if not (builder.PROJECT / '03-unpack/lua/device-luascripts' / name).exists():
            absent.append(stage['id'])
            continue
        dungeon, evidence = builder.table(name)
        sources.append(evidence)
        geometry, evidence = builder.table('Map_' + str(dungeon['mapid']) + '.lua')
        sources.append(evidence)
        reasons = []
        if stage['type'] != 8:
            reasons.append('mode-specific map rules are not reconstructed')
        if stage.get('jWinCon') != [1]:
            reasons.append('unsupported win condition')
        for monster in dungeon.get('monsters', []):
            cfg = groups.get(str(monster['id']))
            if not cfg or cfg.get('nStep', 0) or monster.get('wave', 1) != 1:
                reasons.append('missing group, moving monster, or future monster wave')
        for prop in dungeon.get('props', []):
            if prop.get('type') not in [1, 2, 5, 26, 28] or prop.get('perpetual') or prop.get('nStep', 0):
                reasons.append('unsupported map mechanism')
            if prop.get('type') in [5, 26, 28]:
                wanted.update(prop.get('param', [])[:1])
        submaps = geometry.get('sub_maps', {})
        layers = submaps if isinstance(submaps, list) else submaps.values()
        for layer in layers:
            for cell in layer.get('datas', {}).values():
                if cell.get('type', 0) not in [0, 1] or cell.get('hole_type'):
                    reasons.append('unsupported terrain mechanism')
        maps[stage['id']] = {'dungeon': dungeon, 'map': geometry,
                             'unreconstructed': sorted(set(reasons))}
    rewards, evidence = builder.table('cfgRewardInfo.lua')
    sources.append(evidence)
    stack = list(wanted)
    while stack:
        identifier = stack.pop()
        for row in rewards.get(identifier, {}).get('item', []):
            if row.get('type') == 1 and row['id'] not in wanted:
                wanted.add(row['id'])
                stack.append(row['id'])
    outputs = {'battle-tactical-maps.json': maps,
        'battle-tactical-rewards.json': {key: rewards[key] for key in wanted if key in rewards},
        'battle-tactical-sources.json': {'sources': sources, 'map_count': len(maps),
            'eligible_map_count': sum(not value['unreconstructed'] for value in maps.values()),
            'missing_scripts': sorted(absent), 'missing_rewards': sorted(wanted - set(rewards)),
            'policy': 'Static source catalog; local movement/encounter policy is identified separately in tactical notes.'}}
    for name, value in outputs.items():
        (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in outputs['battle-tactical-sources.json'].items() if key != 'sources'}))

if __name__ == '__main__':
    main()
