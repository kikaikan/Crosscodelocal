"""Read-only tutorial audit; writes only reports under 90-notes.

Configured-view protocol references are candidates, not a proven runtime call graph.
No client requests, live Store, listeners, or writable save connection are used.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LUA = ROOT / '03-unpack/lua/device-luascripts'
OUT = ROOT / '90-notes'


def read(path):
    return (ROOT / path).read_text('utf-8-sig')


def site(path, needle):
    matches = [(i, line.strip()) for i, line in enumerate(read(path).splitlines(), 1) if needle in line]
    if len(matches) != 1:
        raise ValueError(f'Expected unique evidence: {path}: {needle}: {len(matches)}')
    line, text = matches[0]
    return {'file': path, 'line': line, 'text': text}


def link(evidence):
    return f"`{evidence['file']}:{evidence['line']}`"


class _SavedPlayerStore:
    """Minimal read-only Store view over one account's saved player state."""

    def __init__(self, player):
        self._player = player

    def get_player(self, uid):
        return self._player


class _SavedPlayerContext:
    """Minimal Context replacement for render-only handler calls; never writes."""

    def __init__(self, uid, player):
        self.uid = uid
        self.store = _SavedPlayerStore(player)

    def require_login(self):
        return self.uid


def saved_reads(uid, state):
    """Render the three loading-critical reads for one saved account.

    BuildingProto:BuildsList / :BuildsBaseInfo moved from initialization.READS to
    handlers.building, and DormProto:GetOpenDorm lives in handlers.dorm, so render
    them through the owning handlers instead of initialization.render_read().
    The state is an in-memory deep copy; pre-seeding the buildings key keeps
    building.read_state() on its read-only path (no Store transaction).
    """
    building = importlib.import_module('handlers.building')
    dorm = importlib.import_module('handlers.dorm')
    player = deepcopy(state)
    if not building.valid_state(player.get(building.STATE_KEY)):
        player[building.STATE_KEY] = building.initial_state()
    context = _SavedPlayerContext(uid, player)
    rendered = (
        ('BuildingProto:BuildsList', building.builds_list),
        ('BuildingProto:BuildsBaseInfo', building.builds_base_info),
        ('DormProto:GetOpenDorm', dorm.get_open_dorm),
    )
    return {name: asyncio.run(handler(context, {}))[0].fields for name, handler in rendered}


def main():
    sys.path.insert(0, str(ROOT / '07-server'))
    core = importlib.import_module('server_core')
    launcher = read('start_local.ps1')
    body = re.search(r'\[string\[\]\]\s*\$Handlers\s*=\s*@\(([^)]*)\)', launcher).group(1)
    modules = re.findall(r"'([^']+)'", body)
    for module in modules:
        importlib.import_module(module)
    from handlers import initialization, progression
    schema = json.loads(read('05-protocol/endpoints.json'))
    outgoing = {entry['name']: entry for entry in schema['schemas']
                if entry.get('client_send_literal') is True}

    # Strip Lua comments before collecting literal messages / method calls.
    # Preserve newlines so source locations remain accurate.
    def uncomment(text):
        text = re.sub(r'--\[(=*)\[.*?\]\1\]', lambda m: '\n' * m[0].count('\n'), text, flags=re.S)
        return re.sub(r'--[^\n]*', '', text)

    texts = {p.stem: uncomment(p.read_text('utf-8-sig')) for p in LUA.glob('*.lua')}
    method_messages = defaultdict(set)
    for stem, text in texts.items():
        definitions = list(re.finditer(r'function\s+([A-Za-z_][\w]*):([A-Za-z_][\w]*)\s*\(', text))
        for i, match in enumerate(definitions):
            end = definitions[i + 1].start() if i + 1 < len(definitions) else len(text)
            for name in re.findall(r'["\']([A-Za-z_][\w]*:[A-Za-z_][\w]*)["\']', text[match.end():end]):
                if name in outgoing:
                    method_messages[match.group(1) + ':' + match.group(2)].add(name)

    raw = progression.GUIDES
    by_group = defaultdict(list)
    ungrouped = []
    for row in raw.values():
        if row.get('group'):
            by_group[row['group']].append(row)
        else:
            ungrouped.append(row)
    inventories = []
    all_candidates = {}
    aliases = {'RoleUpBreak': 'RoleUpBreakView', 'Matrix': 'MatrixView', 'Dorm': 'DormView',
               'Fight': 'FightView', 'Battle': 'BattleMgr', 'Menu': 'MenuView'}
    common = ['PlayerProto:GetClientData', 'PlayerProto:SetClientData']
    for group, steps in sorted(by_group.items()):
        steps = sorted(steps, key=lambda row: row['id'])
        views = sorted({row.get('view_open') for row in steps if row.get('view_open')})
        scenes = sorted({row.get('trigger_scene') for row in steps if row.get('trigger_scene')})
        names = sorted({row.get('name', '').split('_')[0] for row in steps if row.get('name')})
        files = set()
        for view in views:
            for name in (view, view + 'View', aliases.get(view)):
                if name in texts:
                    files.add(name)
        # Family files include helpers and item components used by configured views.
        # Their references can be reached through ordinary UI, but are not all
        # necessarily exercised by the precise tutorial click sequence.
        families = set()
        for view in views:
            for family in ('Matrix', 'Dorm', 'Create', 'RogueT', 'RogueS', 'Rogue', 'Colosseum',
                           'VirtualCataclysm', 'Exercise', 'RoleEquip', 'PlayerAbility', 'TotalBattle'):
                if view.startswith(family):
                    families.add(family)
        for family in families:
            files.update(name for name in texts if name.startswith(family) and not name.startswith('cfg'))
        if group in (50, 60, 1110, 1120, 1130):
            files.update(name for name in ('TeamMgr', 'TeamConfirm', 'AIPrefabSetting', 'DungeonMgr') if name in texts)
        if group in (1070, 1080, 1090, 1100):
            files.update(name for name in texts if name.startswith(('RoleUp', 'RoleSkill', 'RoleTalent', 'RoleInfo')))
            files.update(name for name in ('RoleMgr', 'RoleCenter') if name in texts)
        if 'Fight' in scenes:
            files.update(name for name in ('FightView', 'FightClient', 'SingleFightMgr') if name in texts)
        if 'Battle' in scenes:
            files.update(name for name in ('BattleMgr', 'BattleCharacterMgr', 'FightFormation') if name in texts)
        if group in (1170, 1175):
            files.update(name for name in texts if name.startswith('Tower') and not name.startswith('cfg'))
        candidates = defaultdict(list)
        for name in sorted(files):
            for n, line in enumerate(texts[name].splitlines(), 1):
                found = set(re.findall(r'["\']([A-Za-z_][\w]*:[A-Za-z_][\w]*)["\']', line))
                for owner, method in re.findall(r'([A-Za-z_][\w]*):([A-Za-z_][\w]*)\s*\(', line):
                    found.update(method_messages.get(owner + ':' + method, []))
                for request in sorted(found):
                    if request in outgoing:
                        ev = {'file': f'03-unpack/lua/device-luascripts/{name}.lua', 'line': n}
                        candidates[request].append(ev)
        records = []
        for request, locations in sorted(candidates.items()):
            record = {'request': request, 'opcode': outgoing[request]['opcode'],
                      'registered': request in core.HANDLERS, 'references': locations}
            records.append(record)
            all_candidates.setdefault(request, {'registered': record['registered'], 'groups': []})['groups'].append(group)
        inventories.append({'group': group, 'step_ids': [row['id'] for row in steps], 'views': views,
                            'scenes': scenes, 'names': names, 'source_files': sorted(files),
                            'common_requests': common, 'feature_candidates': records})

    evidence = {
        'matrix_weight': site('03-unpack/lua/device-luascripts/MatrixView.lua', 'Loading_Weight_Apply, "matrix_scene_enter"'),
        'matrix_empty': site('03-unpack/lua/device-luascripts/MatrixView.lua', '基地没有建筑数据'),
        'matrix_close': site('03-unpack/lua/device-luascripts/MatrixView.lua', 'Loading_Weight_Update, "matrix_scene_enter"'),
        'matrix_defaults': site('07-server/handlers/building.py', "@register('BuildingProto:BuildsList')"),
        'power_defaults': site('07-server/handlers/building.py', "@register('BuildingProto:BuildsBaseInfo')"),
        'power_nil': site('03-unpack/lua/device-luascripts/MatrixScene.lua', 'Cfgs.CfgBGobalPower:GetByID(runingLv).name'),
        'dorm_wait': site('03-unpack/lua/device-luascripts/DormRoom.lua', '-- 房间数据未获取'),
        'guide_wait': site('03-unpack/lua/device-luascripts/GuideMgr.lua', 'if(not self.guideData or not DungeonMgr:GetAlDungeonDatas())then'),
        'guide_write': site('03-unpack/lua/device-luascripts/GuideMgr.lua', 'PlayerProto:SetClientData(self:GetGuideKey(),guideData)'),
        'guide_validate': site('07-server/handlers/progression.py', "raise StorageError('Completed tutorials cannot be removed')"),
        'unsupported': site('07-server/server_core.py', 'raise UnknownRequest(frame.name,frame.opcode)'),
        'request_error_tip': site('07-server/server_core.py', 'replies = [Reply(TIPS_MESSAGE,tip)]'),
        'dorm_fid': site('07-server/handlers/dorm.py', 'return {"fid": fid} if fid else {}'),
        'dorm_owner': site('03-unpack/lua/device-luascripts/DormMgr.lua', 'if (fid) then'),
    }
    important = ['BuildingProto:BuildsBaseInfo', 'BuildingProto:BuildsList', 'BuildingProto:AssualtInfo',
                 'BuildingProto:BuildCreate', 'BuildingProto:Upgrade', 'BuildingProto:BuildSetRole',
                 'BuildingProto:GetBuildUpdate', 'DormProto:GetOpenDorm', 'DormProto:GetDorm',
                 'DormProto:GetSelfTheme', 'DormProto:ModFurniture', 'DormProto:Open',
                 'DormProto:UseGift', 'DormProto:BuyFurniture', 'EquipProto:EquipUp', 'EquipProto:EquipUps',
                 'EquipProto:EquipDown', 'PlayerProto:GetAIStrategy', 'PlayerProto:SetAIStrategy',
                 'PlayerProto:MultSetTeamData', 'PlayerProto:CardCreate', 'PlayerProto:FirstCardCreate',
                 'PlayerProto:FirstCardCreateAffirm', 'TaskProto:GetReward', 'PlayerProto:CardUpgrade',
                 'AbilityProto:AddAbility', 'AbilityProto:ResetAbility', 'AbilityProto:SkillGroupUpgrade', 'AbilityProto:SkillGroupUse',
                 'ArmyProto:GetSelfPracticeInfo', 'FightProtocol:EnterFightDuplicate', 'FightProtocol:OnFightOver']
    registered = {name: name in core.HANDLERS for name in important}
    database = ROOT / '07-server/data/players.sqlite3'
    summaries = []
    if database.exists():
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        try:
            for uid, raw_state in connection.execute('SELECT uid,state_json FROM accounts'):
                state = json.loads(raw_state)
                reads = saved_reads(uid, state)
                parsed = json.loads(state.get('client_data', {}).get('guide_data_key', {}).get('data', '{}'))
                parsed['k120'] = 1
                try:
                    validation = {'success': True, 'groups': progression.validate_guide_progress(deepcopy(state), parsed)}
                except Exception as error:
                    validation = {'success': False, 'exception': type(error).__name__, 'detail': str(error)}
                summaries.append({'uid': uid, 'level': state['player'].get('level'),
                                  'cleared_stages': state.get('progress', {}).get('cleared_stages', []),
                                  'completed_guides': state.get('progress', {}).get('completed_guides', []),
                                  'active_battle_present': 'active_battle' in state,
                                  'reads': reads, 'guide120_validation_on_copy': validation})
        finally:
            connection.close()

    log_raw = (ROOT / '07-server/logs/server.jsonl').read_bytes()
    events, invalid = [], []
    for n, line in enumerate(log_raw.decode('utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            invalid.append(n)
            continue
        events.append({'line': n, **event})
    errors = []
    last_request = {}
    counts = Counter()
    for event in events:
        key = (event.get('role'), event.get('uid'))
        if event['event'] == 'request':
            last_request[key] = event
            counts[event.get('name')] += 1
        elif event['event'] in ('unsupported_handler', 'connection_rejected', 'handler_failure',
                                'battle_entry_rejected', 'battle_report_rejected', 'request_failed', 'connection_closed'):
            row = dict(event)
            row['macau_time'] = datetime.fromtimestamp(event['time_ms'] / 1000, timezone(timedelta(hours=8))).isoformat()
            if key in last_request:
                request = last_request[key]
                row['nearest_same_role_uid_request'] = {k: request.get(k) for k in ('line', 'name', 'opcode')}
            errors.append(row)
        elif event['event'] == 'disconnect':
            last_request.pop(key, None)
    result = {'scope': 'All configured tutorial groups plus dormitory shared base entry',
              'limits': ['Static UI-family candidates are not a proven tutorial call graph.',
                         'Current default startup registry; custom running handler flags may differ.',
                         'No device reproduction or packet fields were captured by this audit.',
                         'Protocol-name logs cannot alone identify a stage or a client-data key.',
                         'Nearest same role/UID request is an association, not a unique connection ID.'],
              'counts': {'rows': len(raw), 'groups': len(inventories), 'grouped_rows': sum(len(x) for x in by_group.values()),
                         'ungrouped_rows': len(ungrouped), 'handlers': len(core.HANDLERS)},
              'launcher_modules': modules, 'important_request_registration': registered,
              'groups': inventories, 'ungrouped_steps': ungrouped, 'candidate_requests': all_candidates,
              'evidence': evidence, 'save_summaries': summaries,
              'critical_source_hashes': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                  'start_local.ps1', '07-server/server_core.py', '07-server/error_policy.py',
                  '07-server/handlers/initialization.py', '07-server/handlers/progression.py',
                  '03-unpack/lua/device-luascripts/cfgGuide.lua',
                  '03-unpack/lua/device-luascripts/MatrixView.lua',
                  '03-unpack/lua/device-luascripts/DormMgr.lua')},
              'log': {'bytes': len(log_raw), 'sha256': hashlib.sha256(log_raw).hexdigest(),
                      'valid_events': len(events), 'invalid_lines': invalid, 'errors': errors,
                      'request_counts': dict(counts), 'last_time_ms': events[-1]['time_ms'] if events else None}}
    OUT.mkdir(exist_ok=True)
    (OUT / 'tutorial-loading-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    missing_important = [name for name, enabled in registered.items() if not enabled]
    missing_candidates = sorted({item['request'] for group in inventories
                                 for item in group['feature_candidates'] if not item['registered']})
    complete_groups = [group['group'] for group in inventories if group['feature_candidates']
                       and all(item['registered'] for item in group['feature_candidates'])]
    incomplete_groups = [group['group'] for group in inventories
                         if any(not item['registered'] for item in group['feature_candidates'])]
    no_candidate_groups = [group['group'] for group in inventories if not group['feature_candidates']]
    group_stats = {group['group']: (len(group['feature_candidates']),
                                    sum(1 for item in group['feature_candidates'] if not item['registered']))
                   for group in inventories}
    error_events = Counter(error['event'] for error in errors)
    family_missing = Counter(name.split(':')[0] for name in missing_candidates)
    generated_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')

    def group_missing_text(groups):
        candidates = sum(group_stats.get(number, (0, 0))[0] for number in groups)
        missing = sum(group_stats.get(number, (0, 0))[1] for number in groups)
        return f"组内候选 {candidates}／未登记 {missing}（机器）"

    md = ['# 所有教程联网与加载卡点检查', '',
          f"生成时间：{generated_at}（Asia/Macau）。只读检查源码、现有日志和SQLite，未操作客户端或改动存档。", '',
          '## 输入指纹（复算用）', '',
          '| 输入 | sha256 |', '| --- | --- |']
    md += [f"| `{path}` | `{digest}` |" for path, digest in sorted(result['critical_source_hashes'].items())]
    md += [f"| `07-server/logs/server.jsonl` | `{result['log']['sha256']}` |", '']
    md += [
          f"配置共{len(raw)}项，其中{sum(len(x) for x in by_group.values())}项有分组、覆盖{len(inventories)}组；另有{len(ungrouped)}项未分组步骤。按最新源码动态导入默认启动配置的{len(modules)}模块，注册{len(core.HANDLERS)}个handler。这不是对运行中旧进程的能力证明。",
          f"机器统计：重要请求 {len(registered) - len(missing_important)}/{len(registered)} 已登记；{len(inventories)} 组中静态候选全覆盖 {len(complete_groups)} 组、至少缺 1 个 handler {len(incomplete_groups)} 组、无静态候选 {len(no_candidate_groups)} 组；去重缺口请求 {len(missing_candidates)} 个；日志窗口有效事件 {len(events)} 个，其中失败事件 {sum(error_events.values())} 条。各数据源见下表与同名 JSON。",
          '本报告覆盖所有已配置教程组；后期模式教程与序章教程分列，未把后期未实现接口说成序章必经请求。', '',
          '## 已确认的关键问题', '',
          f"1. **默认启动注册表仍缺 {len(missing_important)} 个重要请求。** " + '、'.join(f"`{name}`" for name in missing_important) + "。机器来源：JSON `important_request_registration`（动态导入默认启动模块后的 `core.HANDLERS`）。缺 handler 意味着该请求只会得到未实现提示、对应业务动作无法完成；这是静态注册事实，不代表某条教程必经该请求。",
          f"2. **教程组候选缺口集中在少数协议族。** {len(incomplete_groups)}/{len(inventories)} 组至少缺 1 个候选 handler，去重缺口请求 {len(missing_candidates)} 个；按族计数前五位：" + '、'.join(f"`{family}` {count}" for family, count in family_missing.most_common(5)) + "。机器来源：JSON `groups[].feature_candidates[].registered`。候选请求来自配置与 Lua 的静态扫描，不等于实机调用图。",
          f"3. **基地与宿舍的加载阻塞点在当前源码中已有实现，存档渲染可复核。** " + '；'.join(
              f"UID {saved['uid']} 渲染 BuildsList {len(saved['reads']['BuildingProto:BuildsList']['builds'])} 条、runTypeCfgId={saved['reads']['BuildingProto:BuildsBaseInfo']['runTypeCfgId']}、本人 GetOpenDorm {'带' if 'fid' in saved['reads']['DormProto:GetOpenDorm'] else '不带'} fid"
              for saved in summaries) + "。机器来源：JSON `save_summaries[].reads`（用当前 handler 在存档内存副本上渲染，不写库）。相关源码唯一证据行：" + '、'.join(link(evidence[key]) for key in ('matrix_defaults', 'power_defaults', 'dorm_fid')) + "。",
          f"4. **历史日志里的失败事件不能直接当作现存缺口。** 本窗口失败事件 {sum(error_events.values())} 条：" + '、'.join(f"`{event}` {count}" for event, count in sorted(error_events.items())) + "。其中一部分发生在旧策略或后来补齐的接口上，必须与本报告“重要请求当前登记状态”表对照。机器来源：JSON `log.errors`。",
          '',
          '### 人工结论（非自动统计，已标注依据）', '',
          f"【人工结论】基地／宿舍的持续加载机制来自 Lua：{link(evidence['matrix_weight'])}、{link(evidence['matrix_empty'])}、{link(evidence['matrix_close'])}；电力档位为 0 会触发 nil 索引：{link(evidence['power_nil'])}；本人宿舍回包若补 `fid=0` 会被 DormMgr 归入好友列表：{link(evidence['dorm_fid'])}、{link(evidence['dorm_owner'])}。当前服务端实现与存档渲染已不触发这些分支（见第 3 条机器数据），但**是否在真机解除加载仍未验证**：本审计没有设备证据（见 JSON `limits`）。", '',
          '## 按教程功能汇总', '',
          '本表按功能域**人工归类**（非自动统计）；“组内候选／未登记”由代码按组号汇总自机器字段 `groups[].feature_candidates[].registered`，其余判断一律标注【人工结论】。', '',
          '| 教程功能／组 | 发送或等待的内容 | 检查结果 |', '| --- | --- | --- |',
          f"| 开场、技能、破盾、合体／1、10、30、70、80、90、1410 | GetClientData/SetClientData；模拟战斗内部命令 | {group_missing_text([1, 10, 30, 70, 80, 90, 1410])}；【人工结论】开场特殊战斗与训练主要本地模拟，不能把每次攻击当作 TCP 请求；进度读取仍需回包 |",
          f"| 抽卡与首次构建／40、113 | CardFactoryInfo、CardCreate、FirstCardCreate、首次记录与确认 | {group_missing_text([40, 113])}；【人工结论】核心请求已登记（机器 `important_request_registration`），领取、持久化和界面完成仍须实机验证 |",
          f"| 编队、AI、支援／50、1110、1120 | SetTeamData/MultSetTeamData、GetAIStrategy/SetAIStrategy；选择战术还可能 SkillGroupUse | {group_missing_text([50, 1110, 1120])}；【人工结论】核心编队与 AI 已登记；SkillGroupUse 仍缺 handler（见第 1 条），远程支援不能只看一个编队回包 |",
          f"| 出击、普通关卡、剧情／60、105、115、1130、1140 及地图提示 | EnterFightDuplicate、OnFightOver、QuitDuplicate；地图分支 EnterDuplicate/MoveTo 等 | {group_missing_text([60, 105, 115, 1130, 1140])}；【人工结论】普通直接战斗已登记；入口或结算失败若没有对应业务回调，仍会超时或阻断教程 |",
          f"| 任务／110 | GetTasksData 及结束标记、GetReward | {group_missing_text([110])}；【人工结论】核心请求已登记；尚未达成或重复领取需核对拒绝回包与等待解除 |",
          f"| 角色信息、升级、跃升、技能、天赋／1070、1080、1090、1100、1420 | SetCardInfo、CardUpgrade、CardBreak、CardSkillUpgrade、MainTalentUpgrade；部分分支 UpgradeSubTalent/SetUseSubTalent | {group_missing_text([1070, 1080, 1090, 1100, 1420])}；【人工结论】裸卡核心操作已登记；复杂天赋与训练附带操作仍有缺口 |",
          f"| 芯片／1095 | EquipUp/EquipUps/EquipDown | {group_missing_text([1095])}；【人工结论】这三个请求当前均已登记（机器 `important_request_registration`），业务完成仍需实机验证 |",
          f"| 基地／120，宿舍共用入口 | 建筑列表与状态；BuildCreate、Upgrade、GetDorm 等 | {group_missing_text([120])}；【人工结论】建筑与宿舍读取／动作请求均已登记，存档渲染见 `save_summaries[].reads`；实机是否解除加载未验证 |",
          f"| 战术能力／1150、1160 | GetAbility/GetSkillGroup、AddAbility/ResetAbility/SkillGroupUpgrade/SkillGroupUse | {group_missing_text([1150, 1160])}；【人工结论】读取已登记，ResetAbility/SkillGroupUpgrade/SkillGroupUse 仍缺 handler（见第 1 条） |",
          f"| 演习／130、140 | GetPracticeInfo、GetSelfPracticeInfo 等 | {group_missing_text([130, 140])}；【人工结论】基础空读取不等于有可交互对手；GetSelfPracticeInfo 当前已登记（机器 `important_request_registration`） |",
          f"| 爬塔、Rogue、乱斗、宠物、总力战、虚拟灾厄等后续组 | 下表与 JSON 逐组列出 | 【人工结论】多数仅提供初始空读取，玩法动作未完整实现；缺口集中在 FightProtocol（见第 2 条）。这些不属于当前序章必经流程 |",
          f"| 旧冷却提示／1000 | CoolView 配置；旧 CardCool 相关回调已注释 | {group_missing_text([1000])}，即无静态候选（机器 `groups[].feature_candidates` 为空）；【人工结论】提取 Lua 中未找到 CoolView 实现，不能据配置断言当前版本可达 |", '',
          '## 共用等待和断线机制', '',
          '本节为【人工结论】的机制梳理（依据：下述 Lua/Python 行引用与 JSON `log.errors`），不是自动统计。', '',
          '- 登录等待装备初始化和`GetClientData(plot_data)`；GetClientDataRet分发Init_Plot_Data才继续登录。',
          '- 新号进入时读`new_player_fight_state`；开场特殊战斗通过CreateSimulateFight模拟，逐回合操作主要在本地。结束保存状态3，SetClientData没有独立确认协议。',
          f"- 引导初始化需要`guide_data_key`和DungeonMgr副本数据同时存在，才能释放`guide_init`。证据：{link(evidence['guide_wait'])}。",
          '- 每组完成会SetClientData写guide_data_key；服务端校验已知组、单组新增、顺序、等级和通关条件。写接口不需要SetClientDataRet。若校验拒绝，本地已记录而服务端未保存，重登会重复教程；旧策略可能断线，最新源码会给提示。',
          '- 普通序章战斗：EnterFightDuplicate→EntryDupResult/SingleFight→客户端本地模拟→OnFightOver→FightOver/进度奖励。0-4配置nGroupID=100041，走直接战斗入口；不能凭配置含map就判断是战术地图。',
          '- NetWait通常有3秒/副本5秒超时；Loading_Weight未释放则属于另一种持续加载。Lua事件回调异常也可在心跳正常时阻止加载视图关闭。',
          f"- 审计期间server_core异常处理被其他项目编辑更新。最新源码按错误分类，未实现请求和部分业务／程序异常给`SystemProto:Tips`保持连接；协议、鉴权或传输类致命错误仍会关闭。提示不等于SingleFight、GetDormRet或建筑更新，也不自动证明对应加载已解除。证据：{link(evidence['unsupported'])}、{link(evidence['request_error_tip'])}。运行中进程是否已重载未验证。", '',
          '## 本地存档与证据边界', '']
    for saved in summaries:
        guide120_completed = 120 in saved['completed_guides']
        md.extend([f"- UID {saved['uid']}：等级{saved['level']}（机器 `save_summaries[].level`），已通关{saved['cleared_stages']}（`cleared_stages`），已完成教程组{saved['completed_guides']}（`completed_guides`）；组120 {'已记录完成' if guide120_completed else '未记录完成'}。",
                   f"- 当前存档渲染建筑列表：`{json.dumps(saved['reads']['BuildingProto:BuildsList'], ensure_ascii=False)}`；基地电力档位`{saved['reads']['BuildingProto:BuildsBaseInfo']['runTypeCfgId']}`；宿舍列表`{json.dumps(saved['reads']['DormProto:GetOpenDorm'], ensure_ascii=False)}`。数据源：`save_summaries[].reads`（当前 handler 在存档内存副本上渲染，不写库）。",
                   f"- 在内存副本中对组120补报的校验结果：`{json.dumps(saved['guide120_validation_on_copy'], ensure_ascii=False)}`（机器 `guide120_validation_on_copy`）。这只说明进度校验接受该输入，不证明基地教程或设施业务在真机可完成。"])
    md += ['', f"日志快照：{len(log_raw)}字节，SHA256 `{result['log']['sha256']}`，有效事件{len(events)}，无法解析的行{invalid}（未跳过后续有效事件）。失败事件 {sum(error_events.values())} 条：" + '、'.join(f"`{event}` {count}" for event, count in sorted(error_events.items())) + "；请求名计数见 JSON `log.request_counts`。",
           '【人工结论】历史失败里有 EnterFightDuplicate 的 TypeError、SetCardInfo/GetAIStrategy 等后来补齐的接口，不能把历史失败直接视为现存缺口；日志不记录关卡 ID 或 GetClientData key，所以不能把历史 TypeError 断言为 0-4。依据：JSON `log.errors` 与当前 `important_request_registration` 对照，真实存档 `cleared_stages` 记录 1004、1005 通关。', '',
           '## 重要请求当前登记状态', '', '| 请求 | 默认启动状态 |', '| --- | --- |']
    md += [f"| `{name}` | {'已登记，仍需核对业务及回调' if enabled else '未登记，无法完成业务动作'} |" for name, enabled in registered.items()]
    md += ['', '## 所有教程组索引', '',
           '下表候选请求来自配置页面及同功能Lua文件中的静态协议调用，并用endpoints.json的client_send_literal筛选方向；包含页面附带操作，不能保证每个请求都被该组教程点击。具体来源、opcode与逐条登记状态见JSON的groups。全部组共用GetClientData/SetClientData。空候选代表此有限扫描未找到，不能证明不联网。', '',
           '| 组 | 步数 | 功能名 | 配置页面 | 请求候选／其中未登记 |', '| --- | --- | --- | --- | --- |']
    for item in inventories:
        missing = [r['request'] for r in item['feature_candidates'] if not r['registered']]
        md.append(f"| {item['group']} | {len(item['step_ids'])} | {', '.join(item['names']) or '场景提示'} | {', '.join(item['views']) or '场景内提示'} | {len(item['feature_candidates'])} / {len(missing)} |")
    md += ['', '## 联网候选请求总表', '',
           '这里的组号表示配置页面或同功能文件中的静态关联；例如Menu附带的基地刷新不等于抽卡教程必发。未登记表示默认启动缺handler，不表示该教程当前一定能触达该动作。', '',
           '| 请求／opcode | 当前登记 | 静态关联组 | 首个调用出处 |', '| --- | --- | --- | --- |']
    first_reference = {}
    for group in inventories:
        for item in group['feature_candidates']:
            first_reference.setdefault(item['request'], item['references'][0])
    for name, item in sorted(all_candidates.items()):
        md.append(f"| `{name}` / {outgoing[name]['opcode']} | {'有handler' if item['registered'] else '缺handler'} | {', '.join(map(str, item['groups']))} | {link(first_reference[name])} |")
    md += ['', '## 未分组特殊步骤', '', '| ID | 配置名称 | 所在线 | 页面 |', '| --- | --- | --- | --- |']
    for row in sorted(ungrouped, key=lambda row: row['id']):
        md.append(f"| {row['id']} | {row.get('name', '')} | {row.get('line', '')} | {row.get('view_open', '')} |")
    md += ['', '## 处理顺序', '',
           '1. 源码与注册表层已完成（未实机验证）：基地建筑列表与电力档位、宿舍房间与本人 fid 语义、芯片模块启用。依据：第 3 条机器数据与 `important_request_registration`。',
           '2. 待办：能力修改类请求（ResetAbility/SkillGroupUpgrade/SkillGroupUse）等仍缺 handler 的项，按第 2 条缺口分布排序推进。',
           '3. 核对普通教程的任务、抽卡、编队、升级、AI 回包；门槛或资源拒绝时给出可恢复提示并解除对应等待。',
           '4. 再处理爬塔、Rogue、乱斗、宠物、总力战、虚拟灾厄等活动专属教程。',
           '5. 实机逐组检查，记录教程 ID、最近请求、加载权重和 Lua 堆栈，分别验收成功、拒绝、重连与重启继续。', '',
           '本次完成检查和可复算证据整理，未补功能、重启服务、修改客户端或跳过教程。']
    (OUT / 'tutorial-loading-audit.md').write_text('\n'.join(md) + '\n', 'utf-8')
    print(json.dumps({'reports': ['90-notes/tutorial-loading-audit.md', '90-notes/tutorial-loading-audit.json'],
                      'counts': result['counts'], 'save_summaries': summaries}, ensure_ascii=False))


if __name__ == '__main__':
    main()
