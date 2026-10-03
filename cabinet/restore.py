from __future__ import annotations

import argparse
from pathlib import Path

from cabinet.backups import restore_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore cabinet SQLite database from backup")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--confirm", action="store_true", help="replace configured CABINET_DB_PATH")
    args = parser.parse_args()
    if not args.confirm:
        parser.error(
            "Pass --confirm after stopping the cabinet service and selecting the correct backup"
        )
    target = restore_backup(args.backup)
    Path(str(target) + "-wal").unlink(missing_ok=True)
    Path(str(target) + "-shm").unlink(missing_ok=True)
    print(target)


if __name__ == "__main__":
    main()
