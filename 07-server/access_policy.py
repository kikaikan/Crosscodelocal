"""Independent offline access exceptions; progression is always authoritative."""
import json
import sqlite3
import time
from pathlib import Path
from copy import deepcopy
from contextlib import closing
from database import StorageError

DEFAULTS = {'pools': True, 'activities': True, 'illustrations': True}
POLICY_KEY = 'crosscore_ps_access_v2'
OPERATION_GATES = {
    'PlayerProto:CardCoreLv': 'special1',
    'PlayerProto:CardSkillUpgrade': 'special4',
    'PlayerProto:MainTalentUpgrade': 'special20',
    'PlayerProto:OneKeyMainTalentUpgrade': 'special20',
    'PlayerProto:UseItem': 'Bag', 'PlayerProto:UseItemList': 'Bag',
    'EquipProto:EquipUpgrade': 'special3',
    'MailProto:MailsOperate': 'MailView', 'MailProto:GetAttachMail': 'MailView',
    'TaskProto:GetReward': 'MissionView', 'TaskProto:GetRewardByType': 'MissionView',
    'TaskProto:GetRewardByTypes': 'MissionView',
}


def options(state):
    # Unmigrated in-memory fixtures retain source rules, never the old bypass.
    saved = state.get('offline_access', {})
    return {key: saved.get(key) is True for key in DEFAULTS}


def enabled(state, domain):
    return options(state).get(domain, False)


def configure(state, values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS) or any(type(v) is not bool for v in values.values()):
        raise StorageError('Access policy requires pools, activities and illustrations booleans')
    state['offline_access'] = deepcopy(values)
    state['offline_unlock_all'] = False
    client = state.setdefault('client_data', {})
    client['crosscore_ps_access'] = {'type': 1, 'data': '0'}
    client[POLICY_KEY] = {'type': 3, 'data': json.dumps(values, separators=(',', ':'))}
    state['access_policy_version'] = 2


def migrate(store):
    """Idempotent; only policy and its notification change, never game progress."""
    rows = store.connection.execute('SELECT uid FROM accounts').fetchall()
    pending = [row for row in rows if store.get_player(row[0]).get('access_policy_version') != 2 or store.get_player(row[0]).get('offline_unlock_all') is not False]
    if pending:
        destination = Path(store.path).parent / 'backups' / ('access-v2-' + str(time.time_ns()) + '.sqlite3')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as backup:
            store.connection.backup(backup)
    count = 0
    for row in pending:
        uid = row[0]
        state = store.get_player(uid)
        if state.get('access_policy_version') == 2 and state.get('offline_unlock_all') is False:
            continue
        with store.transaction(uid) as tx:
            configure(tx.state, tx.state.get('offline_access', DEFAULTS))
            tx.state.setdefault('control_pending', {})['access'] = [True]
        count += 1
    return count


def sections():
    from handlers.shop import catalog
    return catalog('cfgSection.lua')


def activity_section(section_id):
    # DungeonMgr.SectionType.Activity=3, SectionData.GetSectionType=cfg.group.
    # Do not classify daily/arena/other content by stage-ID ranges.
    return sections().get(int(section_id), {}).get('group') == 3


def activity_stage(stage):
    return activity_section(stage.get('group', 0))


def require_feature(state, name):
    from handlers.initialization import feature_open
    if not feature_open(state, name):
        raise StorageError('Feature is locked by original progression: ' + name)
