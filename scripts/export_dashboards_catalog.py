"""
Write the dashboards metric catalog (services/dashboards_catalog.build_catalog)
to a JSON file.

dashkit's `scripts/export-tools.ts` reads this snapshot to generate the AI tool
definitions Birdy ships in ai/tools/dashkit_tools.json: their parameter schemas
embed the catalog's metric keys, dimensions and window presets as enums. Re-run
(via dashkit's scripts/sync_dashkit_schema.sh) whenever the catalog changes.

Run with:  python -m scripts.export_dashboards_catalog <out-path>
"""

import json
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.dashboards_catalog import build_catalog


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m scripts.export_dashboards_catalog <out-path>", file=sys.stderr)
        return 2
    out = os.path.abspath(argv[0])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(build_catalog(), f, indent=2)
        f.write("\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
