from __future__ import annotations

import argparse
import json
from pathlib import Path

from sourceflow.ingest import collect, digest


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUTS = [
    ROOT / "examples/ingestion/x.json",
    ROOT / "examples/ingestion/xiaohongshu.json",
    ROOT / "examples/ingestion/github.json",
    ROOT / "examples/ingestion/bilibili.json",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize local source samples and create a Markdown digest")
    parser.add_argument("inputs", nargs="*", type=Path, help="platform JSON files; defaults to four documented samples")
    parser.add_argument("--output", type=Path, default=ROOT / "data/normalized/samples.json")
    parser.add_argument("--digest", type=Path, default=ROOT / "data/digest.md")
    args = parser.parse_args()

    records = collect(args.inputs or DEFAULT_INPUTS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.digest.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.digest.write_text(digest(records), encoding="utf-8")
    print(f"{len(records)} records → {args.output}; digest → {args.digest}")


if __name__ == "__main__":
    main()
