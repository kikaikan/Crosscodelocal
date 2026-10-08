"""Build local progression/battle data from extracted tables, never execute Lua."""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('gacha_data_builder', HERE / 'gacha-build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

def main():
    sources, outputs = [], {}
    for source, output in [('cfgMainLine.lua', 'progression-stages.json'),
                           ('cfgMonsterGroup.lua', 'battle-monster-groups.json'),
                           ('cfgMonsterFormation.lua', 'battle-monster-formations.json'),
                           ('cfgCardData.lua', 'battle-card-config.json'),
                           ('cfgCardLevel.lua', 'battle-card-level.json'),
                           ('cfgCardBreak.lua', 'battle-card-break.json'),
                           ('cfgCardIntensify.lua', 'battle-card-intensify.json'),
                           ('cfgCfgPlrUpgrade.lua', 'progression-player-level.json'),
                           ('cfgCfgPlrSkillGroup.lua', 'battle-commander-skills.json'),
                           ('cfgCfgEquip.lua', 'progression-equips.json'),
                           ('cfgCfgEquipRandSkill.lua', 'progression-equip-rand-skills.json'),
                           ('cfgCfgEquipRandSkillLv.lua', 'progression-equip-rand-levels.json'),
                           ('cfgCfgCardBreakLimitLv.lua', 'battle-card-level-cap.json'),
                           ('cfgCfgSectionStarReward.lua', 'progression-star-rewards.json'),
                           ('cfgGuide.lua', 'progression-guides.json'),
                           ('cfgStoryInfo.lua', 'progression-story-info.json'),
                           ('cfgDungeonGroup.lua', 'progression-groups.json')]:
        value, evidence = builder.table(source)
        if isinstance(value, list):
            value = {row['id']: row for row in value if isinstance(row, dict)}
        outputs[output] = value
        sources.append(evidence)
    stages = outputs['progression-stages.json']
    all_monsters, evidence = builder.table('cfgMonsterData.lua')
    sources.append(evidence)
    npc_ids = set()
    for stage in stages.values():
        npc_ids.update(stage.get('arrNPC', []))
        force_npc = stage.get('forceNPC')
        if isinstance(force_npc, int):
            npc_ids.add(force_npc)
        elif isinstance(force_npc, list):
            npc_ids.update(force_npc)
        for team in stage.get('arrForceTeam', []):
            for entry in team:
                if entry.get('bIsNpc'):
                    identifiers = entry.get('nForceID', [])
                    npc_ids.update(identifiers if isinstance(identifiers, list) else [identifiers])
    outputs['battle-npc-config.json'] = {key: all_monsters[key] for key in npc_ids if key in all_monsters}
    rewards, evidence = builder.table('cfgRewardInfo.lua')
    sources.append(evidence)
    wanted = {int(row['reward']) for row in stages.values() if row.get('reward')}
    stack = list(wanted)
    while stack:
        key = stack.pop()
        for item in rewards.get(key, {}).get('item', []):
            if item.get('type') == 1 and item['id'] not in wanted:
                wanted.add(item['id'])
                stack.append(item['id'])
    outputs['progression-rewards.json'] = {key: rewards[key] for key in wanted if key in rewards}
    settings, evidence = builder.table('cfgglobal_setting.lua')
    sources.append(evidence)
    outputs['progression-settings.json'] = settings
    outputs['progression-sources.json'] = {'sources': sources,
        'policy': 'Local client SingleFightMgrServer runs actual combat; server checks session and applies local table rewards.',
        'stage_count': len(stages), 'reward_nodes': len(wanted), 'missing_rewards': sorted(wanted - set(rewards))}
    outputs['progression-sources.json']['npc_count'] = len(outputs['battle-npc-config.json'])
    outputs['progression-sources.json']['missing_npcs'] = sorted(npc_ids - set(all_monsters))
    for name, value in outputs.items():
        (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(outputs['progression-sources.json'] | {'sources': len(sources)}))

if __name__ == '__main__':
    main()
