"""
Initialize the church-meeting-assistant database.

Usage:
    uv run python -m church_assistant.scripts.init_db [--drop-existing]

Reads connection details from .env (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME).
Connects to PostgreSQL and applies schema.sql.

If --drop-existing is passed, drops all existing tables first (DESTRUCTIVE).
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv


# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "church_assistant" / "db" / "schema.sql"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def load_db_config() -> dict:
    """Read DB credentials from .env."""
    load_dotenv()
    cfg = {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "5433")),
        "dbname": os.getenv("DB_NAME", "cma"),
        "user": os.getenv("DB_USER", "cma"),
        "password": os.getenv("DB_PASSWORD"),
    }
    if not cfg["password"]:
        print("ERROR: DB_PASSWORD not set in .env", file=sys.stderr)
        sys.exit(1)
    return cfg


def connect(cfg: dict) -> psycopg.Connection:
    """Connect to PostgreSQL."""
    try:
        conn = psycopg.connect(
            host=cfg["host"],
            port=cfg["port"],
            dbname=cfg["dbname"],
            user=cfg["user"],
            password=cfg["password"],
            connect_timeout=5,
        )
        conn.autocommit = False
        return conn
    except psycopg.OperationalError as e:
        print(f"ERROR: Cannot connect to PostgreSQL: {e}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Troubleshooting:", file=sys.stderr)
        print("  1. Is the container running? → docker ps | grep cma-postgres", file=sys.stderr)
        print("  2. Start container: docker-compose up -d", file=sys.stderr)
        print("  3. Check logs: docker logs cma-postgres", file=sys.stderr)
        sys.exit(2)


def drop_all_tables(conn: psycopg.Connection) -> None:
    """DESTRUCTIVE: drop all known tables."""
    tables = [
        "errors",
        "logs",
        "queries",
        "health_checks",
        "schema_version",
        "users",
    ]
    print("⚠ Dropping existing tables...")
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
            print(f"   - dropped {t}")
        # Drop views too
        for v in ["v_latest_health", "v_queue_depth", "v_stats_today"]:
            cur.execute(f"DROP VIEW IF EXISTS {v} CASCADE")
            print(f"   - dropped view {v}")
    conn.commit()


def apply_schema(conn: psycopg.Connection, schema_sql: str) -> None:
    """Execute schema.sql."""
    print(f"Applying schema from {SCHEMA_PATH}...")
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.commit()
    print("✓ Schema applied")


def verify_tables(conn: psycopg.Connection) -> None:
    """List created tables and views."""
    print()
    print("Verifying schema...")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]

        cur.execute("""
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        views = [row[0] for row in cur.fetchall()]

    print(f"  Tables ({len(tables)}):")
    for t in tables:
        print(f"    - {t}")

    print(f"  Views ({len(views)}):")
    for v in views:
        print(f"    - {v}")

    expected_tables = {"users", "queries", "logs", "errors", "health_checks", "schema_version"}
    missing = expected_tables - set(tables)
    if missing:
        print(f"  ⚠ Missing tables: {missing}")
        sys.exit(3)
    else:
        print("  ✓ All expected tables present")


def print_schema_version(conn: psycopg.Connection) -> None:
    """Print current schema version."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT version, applied_at, description
            FROM schema_version
            ORDER BY version DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            v, applied, desc = row
            print()
            print(f"Schema version: {v}")
            print(f"Applied at:     {applied}")
            print(f"Description:    {desc}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize Church Meeting Assistant database"
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="DROP all tables before applying schema (DESTRUCTIVE)",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  Church Meeting Assistant — DB Initialization")
    print("=" * 70)
    print()

    # Read config
    cfg = load_db_config()
    print(f"Connecting to: {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']}")

    # Connect
    conn = connect(cfg)
    print("✓ Connected")
    print()

    # Read schema
    if not SCHEMA_PATH.exists():
        print(f"ERROR: schema.sql not found at {SCHEMA_PATH}", file=sys.stderr)
        sys.exit(4)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    print(f"Loaded schema ({len(schema_sql)} chars)")

    # Drop if requested
    if args.drop_existing:
        confirm = input("⚠ This will DROP ALL DATA. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)
        drop_all_tables(conn)

    # Apply
    try:
        apply_schema(conn, schema_sql)
    except psycopg.Error as e:
        print(f"ERROR applying schema: {e}", file=sys.stderr)
        conn.rollback()
        sys.exit(5)

    # Verify
    verify_tables(conn)
    print_schema_version(conn)

    print()
    print("=" * 70)
    print("  DB initialization complete")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Add Pavlo as admin:")
    print("     uv run python -m church_assistant.scripts.add_user \\")
    print("         --telegram-id $PAVLO_TELEGRAM_USER_ID \\")
    print("         --name 'Pavlo Kulakovskyi' --role admin")
    print()

    conn.close()


if __name__ == "__main__":
    main()
