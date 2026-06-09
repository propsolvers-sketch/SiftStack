"""One-off migration: move existing output/ files into the new subdir layout.

  output/datasift_upload_*.csv   → output/leads/
  output/tn_notices_*.csv        → output/raw/   (legacy name, kept verbatim)
  output/al_notices_*.csv        → output/raw/
  output/deal_analysis_*.xlsx    → output/deals/

Existing subdirectories (benchmark_tests/, madison_probate_recon/, reports/,
observability/) are left in place. Files already inside a subdirectory are
not touched.

Safe to re-run — uses Path.replace which is a no-op when source == dest.
"""

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "output"

MOVES = [
    ("datasift_upload_*.csv", "leads"),
    ("tn_notices_*.csv", "raw"),
    ("al_notices_*.csv", "raw"),
    ("deal_analysis_*.xlsx", "deals"),
]


def main() -> int:
    if not OUT.exists():
        print(f"No output/ directory at {OUT} — nothing to migrate.")
        return 0

    total_moved = 0
    for glob, subdir in MOVES:
        target = OUT / subdir
        target.mkdir(exist_ok=True)
        moved = 0
        for src in OUT.glob(glob):
            # OUT.glob is non-recursive, so anything matched is in OUT root.
            dst = target / src.name
            if dst.exists() and src.resolve() == dst.resolve():
                continue
            shutil.move(str(src), str(dst))
            moved += 1
        print(f"  {glob:30s} → {subdir + '/':12s}  ({moved} files)")
        total_moved += moved

    print()
    print(f"Total moved: {total_moved}")
    print("Subdirectory contents after migration:")
    for sub in ("leads", "raw", "deals", "reports", "observability"):
        d = OUT / sub
        if d.exists():
            count = sum(1 for _ in d.iterdir() if _.is_file())
            print(f"  output/{sub}/  ({count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
