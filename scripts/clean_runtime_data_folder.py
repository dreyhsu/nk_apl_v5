#!/usr/bin/env python3
"""Remove JSON files and *_result.jpg images directly inside an input folder.

Usage: python scripts/clean_test_folder.py /path/to/folder
Add --dry-run to preview the files without deleting them.
"""

import argparse
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder to clean (not recursive)")
    parser.add_argument("--dry-run", action="store_true", help="List files without deleting them")
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error(f"Not a folder: {args.folder}")

    count = 0
    errors = 0
    for path in sorted(args.folder.iterdir()):
        if not path.is_file():
            continue
        if not (path.suffix.lower() == ".json"
                or (path.suffix.lower() == ".jpg" and path.stem.endswith("_result"))):
            continue
        if args.dry_run:
            print(f"Would delete: {path}")
        else:
            try:
                path.unlink()
            except OSError as exc:
                print(f"Cannot delete {path}: {exc}")
                errors += 1
                continue
            print(f"Deleted: {path}")
        count += 1

    print(f"{'Would delete' if args.dry_run else 'Deleted'} {count} file(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
