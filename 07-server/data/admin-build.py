"""Snapshot bounded, data-only catalogs for offline control/mail operations."""
from pathlib import Path
import importlib.util
import json

HERE = Path(__file__).resolve().parent

def main():
    spec = importlib.util.spec_from_file_location('gacha_builder', HERE / 'gacha-build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    items, si = module.table('cfgItemInfo.lua')
    cards, sc = module.table('cfgCardData.lua')
    hot, sh = module.table('cfgCfgPlrHot.lua')
    settings, ss = module.table('cfgglobal_setting.lua')
    # Item type5 identifies actual obtainable card templates; card_type1 and
    # base_card reject monster/forms. Commander variants use SetSex, not grants.
    roles = {}
    for item in items.values():
        cfgid = item.get('dy_value1')
        card = cards.get(cfgid, {})
        if (item.get('type') == 5 and card.get('base_card') is True and
                card.get('card_type') == 1 and card.get('role_id') and
                card.get('quality') in (3, 4, 5, 6) and
                cfgid not in (71010, 71020, 71013, 71023)):
            roles[cfgid] = card
    fields = ('id', 'name', 'type', 'quality', 'auto_use', 'upperLimit',
              'nExpiry', 'sExpiry', 'expiry', 'isGmForbid', 'dy_value1')
    outputs = {
        'admin-items.json': {key: {field: row[field] for field in fields if field in row}
                             for key, row in items.items()},
        'admin-role-templates.json': roles,
        'admin-source.json': {'sources': [si, sc, sh, ss], 'hot': hot,
                             'tp_max': int(settings['g_TPMax']['value']),
                             'tp_recover_time': int(settings['g_TPRecoverTime']['value']),
                             'counts': {'items': len(items), 'roles': len(roles)}},
    }
    for filename, value in outputs.items():
        (HERE / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(outputs['admin-source.json']['counts']))

if __name__ == '__main__':
    main()
