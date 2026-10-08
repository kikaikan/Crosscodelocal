"""SQLite storage for new local accounts; no official account import."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time

class StorageError(ValueError):
    pass

class StorageBusy(StorageError):
    """Transient SQLite write-lock contention; the identical request may be retried.

    error_policy.retryable() reads the class attribute without importing this
    module, so it answers with SystemProto:Tips instead of closing the socket.
    """
    retryable = True

# Both writers share one SQLite file: this server process and the control
# gateway (02-tools/scripts/control_gateway.py:89). Keep the wait short because
# a synchronous wait blocks the single asyncio event loop and every connection's
# heartbeat; a competing transaction was measured at ~16 ms.
SQLITE_BUSY_TIMEOUT_MS = 1000

def storage_busy(error):
    """Return a StorageBusy for SQLite lock contention, or None for other errors."""
    if not isinstance(error, sqlite3.OperationalError):
        return None
    if (getattr(error, 'sqlite_errorname', '') in
            ('SQLITE_BUSY', 'SQLITE_BUSY_SNAPSHOT', 'SQLITE_LOCKED', 'SQLITE_LOCKED_SHAREDCACHE')
            or 'locked' in str(error).lower()):
        return StorageBusy('本地存档正忙，请稍后重试。')
    return None

class PlayerTxn:
    # PlayerClient.lua:326-378 and cfgglobal_setting.lua g_ArmyCoinId /
    # g_AbilityCoinId: these resources have authoritative player/login fields.
    # Their inventory entries are mirrors used by bag pushes and cost queries.
    CURRENCY_ITEMS = {'gold': '10001', 'diamond': '10002', 'army_coin': '10010',
                      'ability_num': '10020', 'BIND_DIAMOND': '10040'}
    CURRENCY_SECTIONS = {'ability_num': 'login', 'BIND_DIAMOND': 'login'}
    def __init__(self, state):
        self.state = state
        self.sync_currency_items(state)

    @classmethod
    def sync_currency_items(cls, state):
        """Render canonical fields into item mirrors without awarding resources.

        Current player/login values take precedence over an inconsistent old
        mirror. If a newly supported canonical field is absent, an existing
        local inventory balance initializes it. This never imports an account.
        """
        for key, item in cls.CURRENCY_ITEMS.items():
            section = state[cls.CURRENCY_SECTIONS.get(key, 'player')]
            value = int(section.get(key, state['inventory'].get(item, 0)))
            if value < 0 or value > 2147483647:
                raise StorageError('Invalid persisted currency balance')
            section[key] = value
            state['inventory'][item] = value

    def currency(self, key):
        if key not in self.CURRENCY_ITEMS and key != 'hot':
            raise StorageError('Unsupported local currency')
        section = self.CURRENCY_SECTIONS.get(key, 'player')
        return int(self.state[section].get(key, 0))

    def add_currency(self, key, delta):
        if key not in self.CURRENCY_ITEMS and key != 'hot':
            raise StorageError('Unsupported local currency')
        value = self.currency(key) + int(delta)
        if value < 0 or value > 2147483647:
            raise StorageError('Insufficient currency or integer overflow')
        self.state[self.CURRENCY_SECTIONS.get(key, 'player')][key] = value
        if key in self.CURRENCY_ITEMS:
            self.state['inventory'][self.CURRENCY_ITEMS[key]] = value
        return value

    def item_count(self, cfgid):
        item = str(int(cfgid))
        key = next((key for key, value in self.CURRENCY_ITEMS.items() if value == item), None)
        return self.currency(key) if key else int(self.state['inventory'].get(item, 0))

    def add_item(self, cfgid, delta):
        key = str(int(cfgid))
        currency = next((k for k,v in self.CURRENCY_ITEMS.items() if v==key),None)
        if currency:
            return self.add_currency(currency,delta)
        value = self.item_count(cfgid) + int(delta)
        if value < 0 or value > 2147483647:
            raise StorageError('Insufficient items or integer overflow')
        if value:
            self.state['inventory'][key] = value
        else:
            self.state['inventory'].pop(key, None)
        return value

    def add_card(self, cfgid, fields=None):
        used = {int(c['cid']) for c in self.state['cards']}
        cid = max(1, int(self.state.get('next_card_id', 1)))
        while cid in used:
            cid += 1
        # Client calculators index CardBreak/CardIntensify tables from 1.
        card = {'cfgid': int(cfgid), 'cid': cid, 'level': 1, 'break_level': 1,
                'intensify_level': 1, 'exp': 0, 'skills': {}, 'equip_ids': {},
                'equips': [], 'is_new': True, 'get_cnt': 1, 'ctime': int(time.time())}
        card.update(deepcopy(fields or {}))
        card['cfgid'], card['cid'] = int(cfgid), cid
        self.state['cards'].append(card)
        self.state['next_card_id'] = cid + 1
        return deepcopy(card)

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None,
                                          timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        try:
            # Already-WAL databases answer without taking the lock; a first
            # conversion can contend, so it is translated below like the rest.
            self.connection.execute('PRAGMA journal_mode=WAL')
            # CREATE TABLE IF NOT EXISTS is a no-op that takes no write lock once
            # the schema exists; the conditional metadata write below keeps Store()
            # read-only in the steady state. An unconditional INSERT here used to
            # block on the write lock for the whole busy timeout, and the control
            # gateway builds a fresh Store for every POST.
            self.connection.executescript('''
              CREATE TABLE IF NOT EXISTS accounts (
                uid INTEGER PRIMARY KEY, account_name TEXT UNIQUE NOT NULL,
                created_at INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS tickets (
                digest TEXT PRIMARY KEY, uid INTEGER NOT NULL REFERENCES accounts(uid),
                expires_at INTEGER NOT NULL);
            ''')
            if self.connection.execute(
                    "SELECT value FROM metadata WHERE key='next_uid'").fetchone() is None:
                self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('next_uid',900000001)")
        except sqlite3.OperationalError as error:
            # A failed constructor must not leak the file handle it just opened.
            self.connection.close()
            busy = storage_busy(error)
            if busy is None:
                raise
            raise busy from error

    @staticmethod
    def validate_seed(seed):
        if not isinstance(seed, dict) or seed.get('schema_version') != 1:
            raise StorageError('A schema_version=1 new-account seed is required')
        for key, kind in [('player',dict),('login',dict),('inventory',dict),
                          ('cards',list),('teams',list),('progress',dict),('client_data',dict)]:
            if not isinstance(seed.get(key), kind):
                raise StorageError('Seed missing state: ' + key)
        if int(seed['player'].get('level', 0)) != 1 or any(int(c.get('level',0)) != 1 for c in seed['cards']):
            raise StorageError('Player and starter cards must start at level 1')
        forbidden = {'token','access_token','refresh_token','session','sessionid',
                     'session_id','password','pwd','openid','sdk_token'}
        def check(value):
            if isinstance(value, dict):
                for key,item in value.items():
                    if str(key).lower() in forbidden:
                        raise StorageError('Seed must not include credentials')
                    check(item)
            elif isinstance(value,list):
                for item in value:
                    check(item)
        check(seed)

    def account_by_name(self, name):
        row = self.connection.execute('SELECT uid,account_name,created_at FROM accounts WHERE account_name=?',(name,)).fetchone()
        return dict(row) if row else None

    def create_account(self, name, seed):
        name = name.strip()
        if not name or len(name.encode()) > 256:
            raise StorageError('Invalid local account name')
        self.validate_seed(seed)
        existing = self.account_by_name(name)
        if existing:
            return existing
        state,now = deepcopy(seed),int(time.time())
        PlayerTxn.sync_currency_items(state)
        self.begin_immediate()
        try:
            # Another local SDK/server process may have created it while we waited.
            existing = self.account_by_name(name)
            if existing:
                self.commit()
                return existing
            uid = self.connection.execute("SELECT value FROM metadata WHERE key='next_uid'").fetchone()[0]
            self.connection.execute("UPDATE metadata SET value=value+1 WHERE key='next_uid'")
            state['player'].update(uid=uid,currtime=now,create_time=now,tpBeginTime=now)
            for card in state['cards']:
                card['ctime'] = now
            for role in state.get('card_roles',[]):
                role.setdefault('data',{})['t_create'] = now
            state['player']['name'] = state['player'].get('name') or 'Local Commander'
            self.connection.execute('INSERT INTO accounts(uid,account_name,created_at,state_json) VALUES (?,?,?,?)',
                                    (uid,name,now,json.dumps(state,ensure_ascii=False,separators=(',',':'))))
            self.commit()
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise
        return {'uid':uid,'account_name':name,'created_at':now}

    def get_player(self, uid):
        row = self.connection.execute('SELECT state_json FROM accounts WHERE uid=?',(int(uid),)).fetchone()
        if not row:
            raise StorageError('Unknown local player')
        state = json.loads(row[0])
        PlayerTxn.sync_currency_items(state)
        return state

    def begin_immediate(self):
        """Take the write lock, reporting contention as the retryable StorageBusy."""
        try:
            self.connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            busy = storage_busy(error)
            if busy is None:
                raise
            raise busy from error

    def commit(self):
        """Commit the current write transaction, mapping lock contention to StorageBusy."""
        try:
            self.connection.execute('COMMIT')
        except sqlite3.OperationalError as error:
            busy = storage_busy(error)
            if busy is None:
                raise
            raise busy from error

    @contextmanager
    def transaction(self, uid):
        """Synchronous BEGIN IMMEDIATE; do not await inside this context.

        Lock contention raises StorageBusy instead of a bare OperationalError so
        the request answers with a retryable tip and the session stays open.
        """
        if self.connection.in_transaction:
            raise StorageError('Nested transactions are not allowed')
        self.begin_immediate()
        try:
            transaction = PlayerTxn(self.get_player(uid))
            yield transaction
            self.connection.execute('UPDATE accounts SET state_json=?,revision=revision+1 WHERE uid=?',
                                    (json.dumps(transaction.state,ensure_ascii=False,separators=(',',':')),int(uid)))
            self.commit()
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise

    def issue_ticket(self, uid, lifetime=1800):
        self.get_player(uid)
        token,now = secrets.token_hex(24),int(time.time())
        self.connection.execute('DELETE FROM tickets WHERE expires_at<?',(now,))
        self.connection.execute('INSERT INTO tickets VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),int(uid),now+lifetime))
        return token

    def validate_ticket(self, uid, token):
        if not isinstance(token,str) or len(token)>256:
            return False
        row = self.connection.execute('SELECT uid,expires_at FROM tickets WHERE digest=?',
                                      (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return bool(row and row[0]==int(uid) and row[1]>=int(time.time()))

    def close(self):
        self.connection.close()
