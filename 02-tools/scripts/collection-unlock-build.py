"""Build the local collection-unlock table (档案回忆/插画 + 心间私语 全开放).

Sources (read-only authority, never modified):
  03-unpack/lua/device-luascripts/cfgCfgArchiveMultiPicture.lua  插画看板（144 条）
  03-unpack/lua/device-luascripts/cfgCfgASMR.lua                 心间私语（7 条）
  03-unpack/lua/device-luascripts/cfgCfgArchiveStory.lua         档案分组的 infos[].story_id
  07-server/data/progression-story-info.json                     StoryInfo（line/storyType）
  03-unpack/lua/device-luascripts/cfgItemInfo.lua                道具类型校验（type 16 / 27）

Output: 07-server/data/collection-unlock.json

客户端解锁判定（只读实证）:
  * 插画 MulPicInfo:IsHad()  = BagMgr:GetCount(cfg.itemId) > 0        → inventory 写 1 即可
  * 心间私语 ASMRData:IsBuy() = BagMgr:GetCount(cfg.item) > 0         → inventory 写 1 即可
  * 回忆 PlotMgr:IsPlayed    = story_id <= plot_data['line_'..line]   → 按线写「该线被引用的最大 story_id」

本产物只收录 CfgArchiveStory 实际引用到的线；未被引用的线（4/22/10051-10075/20000 等）不写，
也不使用每条线的 StoryInfo 最大值（line7 的 20517、line9 的 20716 未被引用）。

Reproduce:  python -B 02-tools/scripts/collection-unlock-build.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / '07-server'))
sys.path.insert(0, str(PROJECT / '02-tools/scripts'))

from config_codec import parse_lua_table                      # noqa: E402
from seed_generator import LUA_DIR, balanced_table, python_data  # noqa: E402
from handlers.progression import STORIES                       # noqa: E402
from handlers.shop import catalog                              # noqa: E402

PICTURE_ITEM_TYPE = 16
ASMR_ITEM_TYPE = 27


def generic(name):
    """Parse one fixed-path _G[...]={...} table that is not in the shop whitelist."""
    text = (LUA_DIR / name).read_text('utf-8-sig')
    match = re.search(r'_G\[[^\]]+\]\s*=\s*\{', text)
    if match is None:
        raise SystemExit('Missing static configuration table: ' + name)
    return python_data(parse_lua_table(balanced_table(text, match.end() - 1)))


def source(name):
    return {'file': name, 'sha256': hashlib.sha256((LUA_DIR / name).read_bytes()).hexdigest()}


def checked(items, identifier, kind, label):
    """The item must exist in CfgItemInfo with the exact type the client checks."""
    row = items.get(int(identifier))
    if row is None or int(row.get('type', -1)) != kind:
        raise SystemExit('Item %s is not a type-%s %s in cfgItemInfo' % (identifier, kind, label))
    return int(identifier)


def rows(table):
    return list(table.values()) if isinstance(table, dict) else list(table)


def main():
    pictures = catalog('cfgCfgArchiveMultiPicture.lua')
    items = catalog('cfgItemInfo.lua')
    asmr = generic('cfgCfgASMR.lua')
    story = generic('cfgCfgArchiveStory.lua')
    picture_ids = sorted({checked(items, row['itemId'], PICTURE_ITEM_TYPE, 'archive picture')
                          for row in rows(pictures)})
    asmr_ids = sorted({checked(items, row['item'], ASMR_ITEM_TYPE, 'ASMR entry') for row in rows(asmr)})
    plot_lines = {}
    infos = 0
    for group in rows(story):
        for info in group.get('infos', []):
            infos += 1
            story_id = int(info['story_id'])
            record = STORIES.get(str(story_id))
            if record is None:
                raise SystemExit('Story %s is missing from progression-story-info' % story_id)
            line = int(record.get('line', record.get('storyType', 1)))
            plot_lines[line] = max(plot_lines.get(line, 0), story_id)
    output = {
        'items': sorted(set(picture_ids) | set(asmr_ids)),
        'plot_lines': {str(line): plot_lines[line] for line in sorted(plot_lines)},
        'sources': {
            'cfgCfgArchiveMultiPicture.lua': {'sha256': source('cfgCfgArchiveMultiPicture.lua')['sha256'],
                                              'entries': len(rows(pictures)), 'items': len(picture_ids)},
            'cfgCfgASMR.lua': {'sha256': source('cfgCfgASMR.lua')['sha256'],
                               'entries': len(rows(asmr)), 'items': len(asmr_ids)},
            'cfgCfgArchiveStory.lua': {'sha256': source('cfgCfgArchiveStory.lua')['sha256'],
                                       'groups': len(rows(story)), 'infos': infos,
                                       'lines': len(plot_lines)},
            'cfgItemInfo.lua': {'sha256': source('cfgItemInfo.lua')['sha256'],
                                'picture_type': PICTURE_ITEM_TYPE, 'asmr_type': ASMR_ITEM_TYPE},
            'progression-story-info.json': {'sha256': hashlib.sha256(
                (PROJECT / '07-server/data/progression-story-info.json').read_bytes()).hexdigest(),
                'entries': len(STORIES)},
        },
    }
    destination = PROJECT / '07-server/data/collection-unlock.json'
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                           encoding='utf-8')
    print(json.dumps({'path': destination.relative_to(PROJECT).as_posix(),
                      'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                      'items': len(output['items']), 'plot_lines': len(output['plot_lines']),
                      'story_infos': infos}, ensure_ascii=False))


if __name__ == '__main__':
    main()
