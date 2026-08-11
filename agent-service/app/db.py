"""Small DB-API compatibility layer for SQLite tests and PostgreSQL runtime."""

import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID


_NAMED = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def _portable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


class Row:
    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self.columns = list(columns)
        self.values = [_portable(value) for value in values]
        self.mapping = dict(zip(self.columns, self.values))

    def __getitem__(self, key: Any) -> Any:
        return self.values[key] if isinstance(key, int) else self.mapping[key]

    def keys(self):
        return self.mapping.keys()


class Cursor:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    def _row(self, raw: Optional[Sequence[Any]]) -> Optional[Row]:
        if raw is None:
            return None
        columns = [item[0] for item in self.cursor.description]
        return Row(columns, raw)

    def fetchone(self) -> Optional[Row]:
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        columns = [item[0] for item in self.cursor.description]
        return [Row(columns, raw) for raw in self.cursor.fetchall()]


class Connection:
    """Expose one conservative query surface across the two supported stores."""

    def __init__(self, target: str) -> None:
        self.postgres = target.startswith(("postgresql://", "postgres://"))
        if self.postgres:
            import psycopg

            self.raw = psycopg.connect(target)
        else:
            self.raw = sqlite3.connect(target, check_same_thread=False)

    @property
    def integrity_error(self):
        if self.postgres:
            import psycopg

            return psycopg.IntegrityError
        return sqlite3.IntegrityError

    def execute(self, sql: str, params: Any = None) -> Cursor:
        cursor = self.raw.cursor()
        if self.postgres:
            if isinstance(params, Mapping):
                sql = _NAMED.sub(r"%(\1)s", sql)
            else:
                sql = sql.replace("?", "%s")
        cursor.execute(sql, params or ())
        return Cursor(cursor)

    def executescript(self, script: str) -> None:
        if self.postgres:
            raise RuntimeError("PostgreSQL schema must be installed with Alembic")
        self.raw.executescript(script)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()
