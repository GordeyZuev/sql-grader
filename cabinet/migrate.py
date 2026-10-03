"""Apply the cabinet's idempotent SQLite schema migrations."""

from cabinet.infrastructure.sqlite_store import DB_PATH, initialize


def main() -> None:
    initialize()
    print(f"Cabinet database is ready: {DB_PATH}")


if __name__ == "__main__":
    main()
