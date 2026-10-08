"""Write-lock contention answers a retryable tip instead of a raw error."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "02-tools" / "scripts"))
import error_policy
from database import (SQLITE_BUSY_TIMEOUT_MS, StorageBusy, StorageError, Store,
                      storage_busy)


class StorageBusyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="crosscore-busy-")
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("busy-account", seed)["uid"]
        self.blocker = None

    def tearDown(self):
        self.release()
        self.store.close()
        self.directory.cleanup()

    def block_writes(self):
        """Hold the SQLite write lock the way the control gateway does."""
        self.blocker = sqlite3.connect(self.path, isolation_level=None)
        self.blocker.execute("PRAGMA busy_timeout=0")
        self.blocker.execute("BEGIN IMMEDIATE")

    def release(self):
        if self.blocker is not None:
            self.blocker.execute("ROLLBACK")
            self.blocker.close()
            self.blocker = None

    def test_busy_timeout_is_short_and_explicit(self):
        self.assertEqual(self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0],
                         SQLITE_BUSY_TIMEOUT_MS)
        self.assertLess(SQLITE_BUSY_TIMEOUT_MS, 5000)

    def test_storage_busy_only_matches_lock_contention(self):
        self.assertIsNone(storage_busy(sqlite3.OperationalError("disk I/O error")))
        self.assertIsNone(storage_busy(ValueError("database is locked")))
        busy = storage_busy(sqlite3.OperationalError("database is locked"))
        self.assertIsInstance(busy, StorageBusy)
        self.assertIsInstance(busy, StorageError)
        self.assertTrue(StorageBusy.retryable)

    def test_locked_transaction_raises_a_retryable_tip(self):
        self.block_writes()
        started = time.monotonic()
        with self.assertRaises(StorageBusy) as caught:
            with self.store.transaction(self.uid):
                self.fail("the write lock is held by the blocker")
        self.assertGreaterEqual(time.monotonic() - started, SQLITE_BUSY_TIMEOUT_MS / 1000 - 0.2)
        error = caught.exception
        self.assertTrue(type(error).retryable)
        self.assertEqual(error_policy.classify(error), (error_policy.CONTINUE, "storage_busy"))
        self.assertEqual(error_policy.client_message(error, "PlayerProto:CardsData"),
                         error_policy.BUSY_TEXT)
        self.assertFalse(self.store.connection.in_transaction)
        before = self.store.get_player(self.uid)["player"]["gold"]
        self.release()
        with self.store.transaction(self.uid) as tx:
            self.assertEqual(tx.add_currency("gold", 1), before + 1)
        self.assertEqual(self.store.get_player(self.uid)["player"]["gold"], before + 1)

    def test_store_open_does_not_wait_for_the_write_lock(self):
        self.block_writes()
        started = time.monotonic()
        reopened = Store(self.path)
        elapsed = time.monotonic() - started
        try:
            self.assertLess(elapsed, SQLITE_BUSY_TIMEOUT_MS / 1000)
            self.assertEqual(reopened.connection.execute("PRAGMA busy_timeout").fetchone()[0],
                             SQLITE_BUSY_TIMEOUT_MS)
            self.assertFalse(reopened.connection.in_transaction)
        finally:
            reopened.close()

    def test_store_open_reports_contention_on_a_fresh_archive(self):
        fresh = Path(self.directory.name) / "fresh.sqlite3"
        blocker = sqlite3.connect(fresh, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(StorageBusy):
                Store(fresh)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()


if __name__ == "__main__":
    unittest.main()
