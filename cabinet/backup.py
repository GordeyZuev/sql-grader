from __future__ import annotations

import argparse

from cabinet.backups import backup_loop, create_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an integrity-checked cabinet DB backup")
    parser.add_argument(
        "--loop", action="store_true", help="create a daily backup and retain seven days"
    )
    args = parser.parse_args()
    if args.loop:
        backup_loop()
    else:
        print(create_backup())


if __name__ == "__main__":
    main()
