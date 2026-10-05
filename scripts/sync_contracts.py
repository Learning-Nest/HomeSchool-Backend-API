"""Refresh the copies of shared contracts that this repo vendors from ../api-contracts (run after pulling api-contracts).

python scripts/sync_contracts.py [--contracts ../api-contracts] [--content ../content-curriculum]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", default=str(ROOT.parent / "api-contracts"))
    ap.add_argument("--content", default=str(ROOT.parent / "content-curriculum"))
    args = ap.parse_args()
    c, k = Path(args.contracts), Path(args.content)
    pairs = [
        (c / "schemas" / "activity-content.schema.json", ROOT / "app" / "content" / "activity.schema.json"),
        (
            c / "schemas" / "activity-content.v2.schema.json",
            ROOT / "app" / "content" / "activity.v2.schema.json",
        ),
        (c / "test-vectors" / "mastery" / "mastery-vectors.json", ROOT / "tests" / "vectors" / "mastery-vectors.json"),
        (c / "test-vectors" / "mastery" / "streak-vectors.json", ROOT / "tests" / "vectors" / "streak-vectors.json"),
        (k / "dist" / "bundle.json", ROOT / "seed" / "launch-bundle.json"),
    ]
    for src, dst in pairs:
        if not src.exists():
            print(f"skip (missing): {src}")
            continue
        shutil.copyfile(src, dst)
        print(f"copied {src.name} -> {dst.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
