"""PostgreSQL access: one short connection per unit of work."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


class Database:
    def __init__(self, url: str):
        self.url = url

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """A transaction: committed on normal exit, rolled back on exception."""
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            yield conn

    def migrate(self) -> None:
        with self.tx() as conn:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(path.read_text())
