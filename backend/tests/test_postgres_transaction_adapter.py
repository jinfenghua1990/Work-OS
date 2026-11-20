"""Unit tests for the PostgreSQL adapter's explicit transaction boundary.

No database and no external network are required.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from server import PostgresConnection


class FakeCursor:
    rowcount = 1


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.closed = False

    def execute(self, sql, parameters=()):
        self.statements.append((sql, parameters))
        return FakeCursor()

    def close(self):
        self.closed = True


class PostgresTransactionTests(unittest.TestCase):
    def test_successful_business_operation_commits(self):
        backend = FakeConnection()
        with PostgresConnection(backend) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO test VALUES(?)", ("SALE-001",))
        self.assertEqual([query for query, _ in backend.statements],
                         ["BEGIN", "INSERT INTO test VALUES(%s)", "COMMIT"])
        self.assertTrue(backend.closed)

    def test_exception_rolls_back_entire_business_operation(self):
        backend = FakeConnection()
        with self.assertRaisesRegex(RuntimeError, "bad sale"):
            with PostgresConnection(backend) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO test VALUES(?)", ("SALE-001",))
                raise RuntimeError("bad sale")
        self.assertEqual(backend.statements[-1][0], "ROLLBACK")
        self.assertTrue(backend.closed)

    def test_standalone_read_does_not_issue_commit(self):
        backend = FakeConnection()
        with PostgresConnection(backend) as db:
            db.execute("SELECT * FROM test")
        self.assertEqual([query for query, _ in backend.statements], ["SELECT * FROM test"])
        self.assertTrue(backend.closed)

    def test_explicit_commit_is_not_repeated_on_context_exit(self):
        backend = FakeConnection()
        with PostgresConnection(backend) as db:
            db.execute("BEGIN")
            db.execute("UPDATE x SET y=?", (1,))
            db.commit()
        self.assertEqual([query for query, _ in backend.statements],
                         ["BEGIN", "UPDATE x SET y=%s", "COMMIT"])


if __name__ == "__main__":
    unittest.main()
