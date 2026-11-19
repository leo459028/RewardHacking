"""Build the demo verification suite (v1.6.2 P3): the ``tar.gz`` to upload on ``/dashboard/verification``.

    python scripts/build_demo_suite.py --out demo_tax.tar.gz
    python scripts/build_demo_suite.py --scenario honest      # prints outcome evidence as JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.verification import demo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="where to write the suite archive")
    group.add_argument("--scenario", choices=demo.SCENARIOS, help="print the outcome evidence of one scenario")
    args = parser.parse_args(argv)
    if args.scenario:
        print(json.dumps(demo.scenario_evidence(args.scenario), ensure_ascii=False))
        return 0
    archive = demo.suite_archive()
    args.out.write_bytes(archive)
    print(json.dumps({"path": str(args.out), "bytes": len(archive), "sha256": hashlib.sha256(archive).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
