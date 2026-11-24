import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "provider_database_tests", Path(__file__).parents[1] / "helpers/omni_knowledge.py")
KNOWLEDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(KNOWLEDGE)


class ProviderDatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "provider.sqlite"
        self.path.touch()
        self.store = KNOWLEDGE.KnowledgeStore(roots=[], home=directory.name, semantics=False)
        self.addCleanup(self.store.close)

    def test_setup_failure_closes_connection_and_preserves_error(self):
        connection = mock.Mock(spec=sqlite3.Connection)
        failure = sqlite3.OperationalError("Provider setup failed")
        connection.execute.side_effect = failure
        with mock.patch.object(KNOWLEDGE.sqlite3, "connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.store._readonly_provider_db(self.path)
        self.assertIs(caught.exception, failure)
        connection.close.assert_called_once_with()

    def test_successful_setup_leaves_connection_open_for_caller(self):
        connection = mock.Mock(spec=sqlite3.Connection)
        with mock.patch.object(KNOWLEDGE.sqlite3, "connect", return_value=connection) as connect:
            returned = self.store._readonly_provider_db(self.path)
        self.assertIs(returned, connection)
        self.assertIs(connection.row_factory, sqlite3.Row)
        connection.execute.assert_any_call("PRAGMA query_only=ON")
        connect.assert_called_once_with(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        connection.close.assert_not_called()

    def wal_database(self):
        self.path.unlink()
        writer = sqlite3.connect(self.path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE threads(id TEXT)")
        writer.execute("INSERT INTO threads VALUES('a')")
        writer.commit()
        writer.close()

    def test_closed_wal_database_without_sidecars_is_read_immutably(self):
        # Codex closes its databases between uses; the last writer removes the
        # -wal and -shm files, and a plain read-only open then fails.
        self.wal_database()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        connection = self.store._readonly_provider_db(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("SELECT id FROM threads").fetchone()["id"], "a")

    def test_unopenable_closed_wal_database_falls_back_to_immutable_read(self):
        # Some SQLite builds (the system Python the app runs) cannot open such a
        # database read-only at all; the fallback must then read it immutably.
        self.wal_database()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        failing = mock.Mock(spec=sqlite3.Connection)
        failing.execute.side_effect = [None, sqlite3.OperationalError("unable to open database file")]
        real_connect = sqlite3.connect
        calls = []

        def connect(target, **options):
            calls.append(target)
            return failing if len(calls) == 1 else real_connect(target, **options)
        with mock.patch.object(KNOWLEDGE.sqlite3, "connect", side_effect=connect):
            connection = self.store._readonly_provider_db(self.path)
        self.addCleanup(connection.close)
        failing.close.assert_called_once_with()
        self.assertEqual(calls[1], self.path.as_uri() + "?mode=ro&immutable=1")
        self.assertEqual(connection.execute("SELECT id FROM threads").fetchone()["id"], "a")

    def test_open_failure_with_live_wal_is_not_masked(self):
        self.wal_database()
        Path(str(self.path) + "-wal").touch()
        connection = mock.Mock(spec=sqlite3.Connection)
        connection.execute.side_effect = [None, sqlite3.OperationalError("unable to open database file")]
        with mock.patch.object(KNOWLEDGE.sqlite3, "connect", return_value=connection) as connect:
            with self.assertRaises(sqlite3.OperationalError):
                self.store._readonly_provider_db(self.path)
        connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
