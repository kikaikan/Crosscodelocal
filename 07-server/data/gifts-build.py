"""Snapshot source member-gift recipes without executing client Lua."""
from pathlib import Path
import importlib.util
import json
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from admin_resources import item_allowed
from seed_generator import selected_record


def main():
    spec = importlib.util.spec_from_file_location('gift_source_builder', HERE / 'gacha-build.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    raw, source = builder.table('cfgItemInfo.lua')
    mails, mail_source = builder.table('cfgCfgMail.lua')
    item_text = (builder.PROJECT / source['file']).read_text('utf-8-sig')
    mail_text = (builder.PROJECT / mail_source['file']).read_text('utf-8-sig')
    settings, setting_source = selected_record('cfgglobal_setting.lua', 'g_ActivityDiffDayTime')
    recipes, templates = {}, {}
    for identifier, row in raw.items():
        if row.get('type') != 10 or row.get('dy_value1') != 3:
            continue
        match = re.search(r'\[' + str(identifier) + r'\]\s*=\s*\{', item_text)
        evidence = dict(source, key=int(identifier), line=item_text[:match.start()].count('\n') + 1)
        values = row.get('dy_arr', [])
        mail_match = re.search(r'\[' + str(values[0]) + r'\]\s*=\s*\{', mail_text)
        evidence_mail = dict(mail_source, key=values[0], line=mail_text[:mail_match.start()].count('\n') + 1)
        templates[str(values[0])] = dict(mails[values[0]], source=evidence_mail)
        daily = [{'id': reward[0], 'num': reward[1], 'type': reward[2]}
                 for reward in row.get('dy_tb', [])]
        supported = (row.get('auto_use') is True and values[1:] == [1, 0]
                     and not row.get('dy2_tb') and not row.get('dy2Times') and daily
                     and all(reward['type'] == 2 and item_allowed(reward['id']) for reward in daily))
        reason = '' if supported else (
            'dated alternate reward / member-card flags need their own rules' if row.get('dy2_tb') else
            'member-card flags / immediate purchase benefits are outside simple daily gifts' if values[1:] != [1, 0] else
            'daily type2 leaves contain object templates requiring a separate object domain')
        recipes[str(identifier)] = {'cfgid': int(identifier), 'name': row['name'], 'type': 10,
            'days': row['dy_value2'], 'daily_rewards': daily, 'mail_cfgid': values[0],
            'dy_arr': values, 'auto_use': row.get('auto_use', False), 'supported': bool(supported),
            'unsupported_reason': reason, 'source': evidence}
    output = {'version': 1, 'reset_hour_utc_plus_8': int(settings['value']),
              'source': source, 'reset_source': setting_source, 'items': recipes,
              'mail_templates': templates}
    (HERE / 'gifts-source.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'catalog': len(recipes), 'supported': [int(key) for key, row in recipes.items() if row['supported']]}))


if __name__ == '__main__':
    main()
