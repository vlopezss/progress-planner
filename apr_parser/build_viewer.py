"""
Build the final standalone viewer.html by injecting viewer_data.json into
viewer_template.html. Run export_for_viewer.py first if dev.db has changed.

Usage:
    python3 build_viewer.py
    python3 build_viewer.py --data custom.json --template my_template.html --out custom_viewer.html
"""

import argparse
import json


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="viewer_data.json")
    ap.add_argument("--template", default="viewer_template.html")
    ap.add_argument("--out", default="viewer.html")
    args = ap.parse_args()

    with open(args.data) as f:
        data = json.load(f)

    with open(args.template) as f:
        template = f.read()

    injected = template.replace("__VIEWER_DATA__", json.dumps(data))

    with open(args.out, "w") as f:
        f.write(injected)

    print(f"Built {args.out} ({len(injected):,} bytes) from {args.data} + {args.template}")


if __name__ == "__main__":
    main()
