from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
EXPECTED_COLUMNS = {
    "economic_indicators": {"metric_key", "label", "unit"},
    "economic_observations": {"observation_date", "metric_key", "value"},
    "schema_migrations": {"version", "applied_at"},
    "enterprise_audit_log": {
        "audit_id",
        "occurred_at",
        "actor_subject",
        "actor_email",
        "actor_role",
        "action",
        "resource",
        "outcome",
        "details",
    },
}


def validate_existing_schema(cursor) -> None:
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        """,
        (list(EXPECTED_COLUMNS),),
    )
    existing: dict[str, set[str]] = {}
    for table_name, column_name in cursor.fetchall():
        existing.setdefault(table_name, set()).add(column_name)

    incompatible = []
    for table_name, required_columns in EXPECTED_COLUMNS.items():
        present_columns = existing.get(table_name)
        if present_columns is None:
            continue
        missing_columns = required_columns - present_columns
        if missing_columns:
            incompatible.append(
                f"{table_name} is missing expected columns: "
                f"{', '.join(sorted(missing_columns))}"
            )
    if incompatible:
        raise RuntimeError(
            "Existing database tables do not match this migration; no changes were applied. "
            + "; ".join(incompatible)
        )


def apply_migrations(connection) -> list[str]:
    applied_versions = []
    with connection.cursor() as cursor:
        cursor.execute("SET search_path TO public")
        validate_existing_schema(cursor)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cursor.fetchall()}

        for migration_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = migration_path.stem
            if version in applied:
                continue
            sql = migration_path.read_text(encoding="utf-8")
            cursor.execute(sql)
            cursor.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (version,),
            )
            applied_versions.append(version)
    connection.commit()
    return applied_versions


def main() -> int:
    load_dotenv(BACKEND_DIR / ".env")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not configured; no migrations were applied.", file=sys.stderr)
        return 2

    connection = None
    try:
        connection = psycopg2.connect(database_url, connect_timeout=5)
        applied = apply_migrations(connection)
    except (psycopg2.Error, OSError, RuntimeError) as exc:
        if connection is not None:
            connection.rollback()
        print(f"Migration failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    if applied:
        print(f"Applied migrations: {', '.join(applied)}")
    else:
        print("Database schema is already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
