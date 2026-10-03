"""Command line validation for a complete course manifest."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from cabinet.domains.manifests import load_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a SQL cabinet JSON manifest")
    parser.add_argument("manifest", nargs="?", type=Path, default=Path("manifest.course.json"))
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    print(f"Manifest: {args.manifest}")
    print(f"Default hard deadline: {manifest.hard_deadline or 'not set'} ({manifest.timezone})")
    print(f"Penalty: {manifest.policy.formula}")
    print(f"Homeworks: {len(manifest.homeworks)}")
    for homework in manifest.homeworks:
        points = sum((task.points for task in homework.tasks), Decimal(0))
        print(
            f"  {homework.id}: {len(homework.tasks)} tasks, {points} points, "
            f"soft deadline {homework.soft_deadline}, hard deadline {homework.hard_deadline}"
        )


if __name__ == "__main__":
    main()
