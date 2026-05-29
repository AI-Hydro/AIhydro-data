"""
`aihydro-data` console entry-point.

Phase 1 ships a thin scaffold so `pip install -e .` registers the script.
Subcommands flesh out across later phases:

    aihydro-data fetch <variable> --geom <path> --start <date> --end <date>
    aihydro-data list-products [--variable <v>] [--region <r>]
    aihydro-data describe <product_id>
    aihydro-data doctor                     # Phase 7: env check
    aihydro-data auth <gee|stac>            # Phase 7: trigger auth flow
    aihydro-data help [<topic>]             # Phase 7: bundled help
"""
from __future__ import annotations

import argparse
import json
import sys

from aihydro_data import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aihydro-data",
        description="Global hydrology dataverse — fetch any variable, anywhere.",
    )
    parser.add_argument(
        "--version", action="version", version=f"aihydro-data {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd")

    list_p = sub.add_parser("list-products", help="Discover available products.")
    list_p.add_argument("--variable", default=None)
    list_p.add_argument("--region", default=None)
    list_p.add_argument("--source", default=None)
    list_p.add_argument("--json", action="store_true", help="Emit JSON instead of table.")

    desc_p = sub.add_parser("describe", help="Show one product's full spec.")
    desc_p.add_argument("product_id")

    args = parser.parse_args(argv)

    if args.cmd == "list-products":
        from aihydro_data import list_products
        out = list_products(
            variable=args.variable, region=args.region, source=args.source
        )
        if args.json:
            print(json.dumps([p.model_dump() for p in out], indent=2, default=str))
        else:
            if not out:
                print("(no products registered yet — try `pip install aihydro-data[all]`)")
            else:
                for p in out:
                    print(f"  {p.id:20s}  {p.variable:18s}  {p.source:10s}  {','.join(p.coverage)}")
        return 0

    if args.cmd == "describe":
        from aihydro_data import get_product
        try:
            spec = get_product(args.product_id)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(spec.model_dump(), indent=2, default=str))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
