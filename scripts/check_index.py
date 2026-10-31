#!/usr/bin/env python3
"""
check_index.py — refuse an index that must not be published, then write the release's manifest.json and build-info.json.

Usage: python3 scripts/check_index.py <index.sqlite> <version> <repository> <previous build-info.json> [<rows baseline build-info.json>]

Exits non-zero, writing nothing, when the index is missing, empty, of another schema, below the floors, or much smaller
than the release before it, or when the version is not today's UTC date or not after the previous one. The workflow
publishes nothing after a non-zero exit. (v2) The previous record is required: it is latest/build-info.json in the
repository, seeded with the 13 Sept 2026 index before the first run, so no network answer decides whether the
relative checks run. A recovery (republish) passes the source release's own build-info.json as the rows baseline, so
the bad release it replaces can't refuse it; the version must still be after the previous record.

manifest.json carries exactly the four fields the app's parser reads (DPDUpdater.Manifest in AlwaysMyCare:
version, sha256, size, url). Anything else about the build goes in build-info.json, which the app never reads.
"""

import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys

SCHEMA_VERSION = 2          # build_dpd_index.SCHEMA_VERSION; DPDIndex.schemaVersion in the app
MIN_ROWS = 10_000           # the 13 Sept 2026 build had 13,538 rows; Health Canada's marketed list moves by a few percent a year
MIN_BYTES = 7_000_000       # the same build was 9,687,040 bytes
MIN_SHARE_OF_PREVIOUS = 0.90  # a week's drop of a tenth of the marketed products is a broken extract, not the market
ASSET = "drugindex.sqlite"


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def main() -> None:
    if len(sys.argv) not in (5, 6):
        fail("usage: check_index.py <index.sqlite> <version> <repository> <previous build-info.json> [<rows baseline build-info.json>]")
    index, version, repository, previous = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    baseline = sys.argv[5] if len(sys.argv) == 6 else previous

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
        fail(f"version {version!r} is not a date (YYYY-MM-DD)")
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if version != today:
        fail(f"version {version} is not today's UTC date {today}: a release is never dated ahead or behind")
    if not os.path.isfile(index):
        fail(f"no index at {index}")
    size = os.path.getsize(index)
    if size < MIN_BYTES:
        fail(f"index is {size:,} bytes; the floor is {MIN_BYTES:,}")

    conn = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]
        with_din = conn.execute("SELECT COUNT(*) FROM drugs WHERE din != '' AND din IS NOT NULL").fetchone()[0]
        # The search the app runs (FTS5 trigram, by ingredient) answers for a medication every build has carried.
        ramipril = conn.execute("SELECT COUNT(*) FROM drugs WHERE ingredients MATCH '\"ramipril\"'").fetchone()[0]
    except sqlite3.Error as e:
        fail(f"index cannot be read: {e}")
    finally:
        conn.close()

    if schema != SCHEMA_VERSION:
        fail(f"schema {schema}; the app reads {SCHEMA_VERSION}")
    if rows < MIN_ROWS:
        fail(f"{rows:,} rows; the floor is {MIN_ROWS:,}")
    if with_din < rows * 0.95:
        fail(f"only {with_din:,} of {rows:,} rows carry a DIN")
    if ramipril == 0:
        fail("a search for ramipril finds nothing")

    try:
        with open(previous) as f:
            before_version = str(json.load(f)["version"])
        with open(baseline) as f:
            base = json.load(f)
        base_version, base_rows = str(base["version"]), int(base["rows"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        fail(f"a build record cannot be read: {e}")
    if version <= before_version:
        fail(f"version {version} is not after the previous record's {before_version}")
    if rows < base_rows * MIN_SHARE_OF_PREVIOUS:
        fail(f"{rows:,} rows against {base_rows:,} in {base_version}: more than a tenth fewer")

    sha256 = hashlib.sha256(open(index, "rb").read()).hexdigest()
    url = f"https://github.com/{repository}/releases/download/{version}/{ASSET}"
    out = os.path.dirname(os.path.abspath(index))
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump({"version": version, "sha256": sha256, "size": size, "url": url}, f, indent=2)
        f.write("\n")
    with open(os.path.join(out, "build-info.json"), "w") as f:
        json.dump({"version": version, "rows": rows, "rows_with_din": with_din, "schema": schema, "sha256": sha256, "size": size}, f, indent=2)
        f.write("\n")
    print(f"version {version} · {rows:,} rows · {size:,} bytes · sha256 {sha256}")


if __name__ == "__main__":
    main()
