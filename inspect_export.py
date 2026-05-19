"""
Quick CLI to dump the columns + first rows of any Meta Ads export so you can
see exactly what the parser will receive. Useful if a file doesn't import cleanly.

Usage:
    python inspect_export.py path/to/file.xls
    python inspect_export.py path/to/folder/         # all .csv/.xls/.xlsx in folder
"""

import sys
from pathlib import Path

import pandas as pd

from backend.parser import parse_file


def read_any(path: Path) -> pd.DataFrame:
    name = path.name.lower()
    if name.endswith(".csv"):
        try:
            return pd.read_csv(path)
        except UnicodeDecodeError:
            return pd.read_csv(path, encoding="latin-1")
    if name.endswith(".xlsx"):
        return pd.read_excel(path, engine="openpyxl")
    if name.endswith(".xls"):
        try:
            return pd.read_excel(path, engine="xlrd")
        except Exception:
            try:
                return pd.read_html(path)[0]
            except Exception:
                return pd.read_excel(path, engine="openpyxl")
    raise ValueError(f"Unsupported: {path.name}")


def inspect(path: Path):
    print("=" * 80)
    print(path.name)
    print("=" * 80)
    df = read_any(path)
    print(f"shape : {df.shape}")
    print(f"cols  : {list(df.columns)}")
    print("\nfirst 3 rows:")
    print(df.head(3).to_string())

    # Run through our parser to see what it would actually store
    with open(path, "rb") as f:
        parsed = parse_file(f.read(), path.name)
    print(f"\nparser saw: level={parsed['report_level']}  is_daily={parsed['is_daily']}  "
          f"rows={parsed['row_count']}  period={parsed['period_start']}..{parsed['period_end']}")
    if parsed["rows"]:
        print("first normalized row:")
        for k, v in parsed["rows"][0].items():
            if k == "raw":
                continue
            print(f"  {k}: {v}")
    print()


def main():
    if len(sys.argv) < 2:
        print("usage: python inspect_export.py <file-or-folder>")
        sys.exit(1)
    target = Path(sys.argv[1])
    if target.is_dir():
        for p in sorted(target.iterdir()):
            if p.suffix.lower() in (".csv", ".xls", ".xlsx"):
                inspect(p)
    else:
        inspect(target)


if __name__ == "__main__":
    main()
