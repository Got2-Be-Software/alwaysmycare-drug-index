#!/usr/bin/env python3
"""
build_dpd_index.py — Build the AlwaysMyCare staged DPD index (FTS5) from Health Canada's Drug Product Database.

Ported from Got2TakeMeds (owner 2026-09-11/13, #67/#68) with the output path and the user agent changed; the schema (v2)
and the concept key are the same. Moved out of the AlwaysMyCare repository (AlwaysMyCare #152, Owner 2026-10-31): this
repository's weekly workflow publishes the index and its manifest.json as a GitHub release, which the app reads.

Downloads drug.zip, comp.zip, ingred.zip, and form.zip from the Health Canada DPD extract
(marketed products only), then writes a fresh drugindex.sqlite with the FTS5 virtual table
expected by AlwaysMyCareModel/Canada/DPDIndex.swift.

SCHEMA v2 (Task 2 / Decision B1 — concept-grouped search):

    CREATE VIRTUAL TABLE drugs USING fts5(
        drug_code   UNINDEXED,   -- DPD DRUG_CODE (drug.txt field[0]); the index primary key
        brand_name,              -- searchable (trigram)
        descriptor  UNINDEXED,   -- synthesized "150MG/CAPSULE" display string
        company_name,            -- searchable (trigram)
        din         UNINDEXED,   -- DRUG_IDENTIFICATION_NUMBER (drug.txt field[3]) — the
                                 --   Medication Identifier (FR-ADD-015); DISTINCT from drug_code
        ingredients,             -- searchable; "|"-joined base active-ingredient names
        dosage_form UNINDEXED,   -- PHARMACEUTICAL_FORM (form.txt field[2])
        concept_key UNINDEXED,   -- normalized grouping key: sorted base ingredients + "#" + form
        source      UNINDEXED,   -- provenance tag; always "DPD" here (Tier 1 is DPD + Manual)
        tokenize = "trigram"
    )

Why the extra columns exist:
  - drug_code vs din: VERIFIED against the DPD flat files — drug.txt field[0] is the DPD
    DRUG_CODE (e.g. "9") and field[3] is the DIN (e.g. "00015741"). They are DISTINCT. The
    DIN is the Medication Identifier each Strength must carry (FR-ADD-015).
  - ingredients: ALL active ingredients (base names, salt/solvate suffix stripped), not just
    the first — combination products (2882 of ~13.5k rows) must retain their full set so they
    group into their own concept, distinct from any single-ingredient concept.
  - concept_key: computed here so grouping is stable and identical between builds and Swift.
    Normalization: each ingredient's base name (text before the first "(" — the salt form),
    uppercased/trimmed, deduped, sorted, "+"-joined, then "#" + uppercased dosage form. This
    means e.g. "ATORVASTATIN (ATORVASTATIN CALCIUM)" and
    "ATORVASTATIN (ATORVASTATIN CALCIUM PROPYLENE GLYCOL SOLVATE)" both normalize to
    "ATORVASTATIN" and group together.

descriptor is still built by combining active-ingredient strength+unit (from ingred.txt) with
the pharmaceutical form (from form.txt): e.g. "150MG/CAPSULE", "88MCG/TABLET". Falls back to
the DESCRIPTOR field in drug.txt (field[5]) when it is non-empty and no ingred+form data is
available.

`PRAGMA user_version` is set to SCHEMA_VERSION so a consumer / OTA update can detect the shape.

The DPD extract files are UTF-8, comma-delimited. drug.zip contains marketed products only
— no status filtering needed (cancelled/dormant are in _ia.zip and _dr.zip).

Output: dist/drugindex.sqlite (~9.5 MB, ~13,500 rows)

Usage:
    python3 scripts/build_dpd_index.py            # the full index, from the DPD extracts
"""

import csv
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

# Bump when the FTS5 column shape changes. Stored via PRAGMA user_version and read by any
# consumer that needs to detect an old-vs-new index (e.g. an OTA-downgraded file).
SCHEMA_VERSION = 2

# The v2 FTS5 shape. brand_name and ingredients are searchable (so brandless generics are
# reachable by ingredient); everything else is UNINDEXED metadata.
CREATE_TABLE_SQL = (
    "CREATE VIRTUAL TABLE drugs USING fts5("
    "drug_code UNINDEXED, brand_name, descriptor UNINDEXED, company_name, "
    "din UNINDEXED, ingredients, dosage_form UNINDEXED, concept_key UNINDEXED, "
    "source UNINDEXED, tokenize = 'trigram')"
)

# ── Config ─────────────────────────────────────────────────────────────────────

DPD_BASE = "https://www.canada.ca/content/dam/hc-sc/documents/services/drug-product-database/"
DRUG_ZIP_URL  = DPD_BASE + "drug.zip"
COMP_ZIP_URL  = DPD_BASE + "comp.zip"
INGRED_ZIP_URL = DPD_BASE + "ingred.zip"
FORM_ZIP_URL  = DPD_BASE + "form.zip"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "dist", "drugindex.sqlite")
ENCODING = "utf-8"
DELIMITER = ","

# ── Download ────────────────────────────────────────────────────────────────────
# urllib times out against the canada.ca CDN; curl handles it reliably.

def download_zip(url: str, label: str) -> bytes:
    print(f"  Downloading {label} …")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["curl", "-L", "-s", "--max-time", "120", "--connect-timeout", "30",
             "-A", "AlwaysMyCare/1.0 dpd-index-builder",
             url, "-o", tmp_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr}")
        with open(tmp_path, "rb") as f:
            data = f.read()
        print(f"    {len(data):,} bytes downloaded.")
        return data
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ── Parse DPD flat files ────────────────────────────────────────────────────────
#
# drug.zip → drug.txt  (UTF-8, comma-delimited, no header row)
#   field[0] DRUG_CODE   (e.g. "9")           — DPD's own key; the index primary key
#   field[3] DIN         (e.g. "00015741")    — DRUG_IDENTIFICATION_NUMBER; DISTINCT from field[0]
#   field[4] BRAND_NAME
#   field[5] DESCRIPTOR   (packaging note; non-empty for ~13% of rows)
#
# comp.zip → comp.txt
#   field[0] DRUG_CODE
#   field[2] COMPANY_CODE  (numeric — NOT the name)
#   field[3] COMPANY_NAME
#
# ingred.zip → ingred.txt
#   field[0] DRUG_CODE
#   field[2] INGREDIENT    (e.g. "ATORVASTATIN (ATORVASTATIN CALCIUM)") — may carry a
#                            parenthetical salt/solvate suffix; the base name precedes the "("
#   field[4] STRENGTH      (e.g. "150")
#   field[5] STRENGTH_UNIT (e.g. "MG")
#   NOTE: a drug_code can have MULTIPLE rows here — one per active ingredient (combination
#   products). All are retained; the previous "first ingredient only" behaviour was lossy.
#
# form.zip → form.txt
#   field[0] DRUG_CODE
#   field[2] PHARMACEUTICAL_FORM  (e.g. "CAPSULE", "TABLET")

def base_ingredient_name(raw: str) -> str:
    """Strip a trailing parenthetical salt/solvate form and normalize.

    "ATORVASTATIN (ATORVASTATIN CALCIUM)" -> "ATORVASTATIN". MUST mirror
    SQLiteDrugIndex.baseIngredientName in Swift so build-time and any runtime fallback key agree.
    """
    name = raw
    paren = name.find("(")
    if paren != -1:
        name = name[:paren]
    return name.strip().upper()


def compute_concept_key(ingredient_names: list[str], dosage_form: str) -> str:
    """Sorted, deduped base ingredients + "#" + uppercased form.

    e.g. (["PERINDOPRIL", "INDAPAMIDE"], "TABLET") -> "INDAPAMIDE+PERINDOPRIL#TABLET".
    MUST mirror SQLiteDrugIndex.conceptKey(ingredients:dosageForm:) in Swift.
    """
    bases = sorted({base_ingredient_name(n) for n in ingredient_names if base_ingredient_name(n)})
    form = (dosage_form or "").strip().upper()
    return "+".join(bases) + "#" + form


def read_first_txt_from_zip(zip_bytes: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        txt_files = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not txt_files:
            raise FileNotFoundError(f"No .txt file found. Contents: {zf.namelist()}")
        raw = zf.read(txt_files[0])
        text = raw.decode(ENCODING, errors="replace")
        return list(csv.reader(io.StringIO(text), delimiter=DELIMITER))


def build_index(
    drug_zip: bytes,
    comp_zip: bytes,
    ingred_zip: bytes,
    form_zip: bytes,
) -> list[tuple]:
    """Return list of 10-column tuples:
    (drug_code, brand_name, descriptor, company_name, din,
     ingredients, dosage_form, concept_key, source)  -- 9 columns; drug_code int + 8.
    """

    print("  Parsing drug.txt …")
    drug_rows = read_first_txt_from_zip(drug_zip)
    brand_names: dict[str, str] = {}
    dins: dict[str, str] = {}              # field[3] DRUG_IDENTIFICATION_NUMBER
    drug_descriptors: dict[str, str] = {}  # fallback descriptor from drug.txt field[5]
    for row in drug_rows:
        if len(row) < 5:
            continue
        code = row[0].strip()
        brand = row[4].strip()
        if code and brand and code.isdigit():
            brand_names[code] = brand
        if len(row) > 3 and row[3].strip():
            dins[code] = row[3].strip()
        if len(row) > 5 and row[5].strip():
            drug_descriptors[code] = row[5].strip()
    print(f"    {len(brand_names):,} brand names, {len(dins):,} DINs, "
          f"{len(drug_descriptors):,} drug.txt descriptors loaded.")

    print("  Parsing comp.txt …")
    comp_rows = read_first_txt_from_zip(comp_zip)
    company_names: dict[str, str] = {}
    for row in comp_rows:
        if len(row) < 4:
            continue
        code = row[0].strip()
        name = row[3].strip()  # field[3] = COMPANY_NAME; field[2] = COMPANY_CODE
        if code and name and code not in company_names:
            company_names[code] = name
    print(f"    {len(company_names):,} company names loaded.")

    print("  Parsing ingred.txt …")
    ingred_rows = read_first_txt_from_zip(ingred_zip)
    # Retain ALL active ingredients per drug_code (order preserved as encountered), each with
    # its own strength — combination products previously lost every ingredient after the first.
    ingredients_by_code: dict[str, list[str]] = {}   # base names, in file order (deduped)
    strengths_by_code: dict[str, list[str]] = {}      # "{STRENGTH}{UNIT}" per ingredient
    for row in ingred_rows:
        if len(row) < 6:
            continue
        code = row[0].strip()
        raw_name = row[2].strip()    # INGREDIENT
        strength = row[4].strip()    # STRENGTH
        unit = row[5].strip()        # STRENGTH_UNIT
        if not code:
            continue
        base = base_ingredient_name(raw_name)
        if base:
            names = ingredients_by_code.setdefault(code, [])
            if base not in names:
                names.append(base)
        if strength and unit:
            strengths_by_code.setdefault(code, []).append(f"{strength}{unit}")
    total_ingred = sum(len(v) for v in ingredients_by_code.values())
    combos = sum(1 for v in ingredients_by_code.values() if len(v) > 1)
    print(f"    {total_ingred:,} ingredient names across {len(ingredients_by_code):,} drugs "
          f"({combos:,} combination products).")

    print("  Parsing form.txt …")
    form_rows = read_first_txt_from_zip(form_zip)
    forms: dict[str, str] = {}
    for row in form_rows:
        if len(row) < 3:
            continue
        code = row[0].strip()
        form = row[2].strip()        # PHARMACEUTICAL_FORM
        if code and form and code not in forms:
            forms[code] = form
    print(f"    {len(forms):,} form entries loaded.")

    # Build descriptor per drug_code: "{STRENGTH}{UNIT}/{FORM}".
    # For combination products, join each ingredient's strength with "/" so the display carries
    # the full strength set (e.g. "4MG/1.25MG/TABLET"). Fall back to drug.txt DESCRIPTOR when
    # ingred+form are unavailable.
    def make_descriptor(code: str) -> str:
        s_list = strengths_by_code.get(code, [])
        s = "/".join(s_list)
        f = forms.get(code, "")
        if s and f:
            return f"{s}/{f}"
        if s:
            return s
        if f:
            return f
        return drug_descriptors.get(code, "")

    records = []
    for code, brand in brand_names.items():
        desc = make_descriptor(code)
        company = company_names.get(code, "")
        din = dins.get(code, "")
        names = ingredients_by_code.get(code, [])
        ingredients = "|".join(names)
        form = forms.get(code, "")
        concept_key = compute_concept_key(names, form)
        records.append((int(code), brand, desc, company, din, ingredients, form, concept_key, "DPD"))

    records.sort(key=lambda r: r[0])
    print(f"  {len(records):,} records ready for indexing.")
    return records


# ── Write SQLite FTS5 index ─────────────────────────────────────────────────────

def write_index(records: list[tuple], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp_path = output_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    print(f"  Writing {output_path} …")
    conn = sqlite3.connect(tmp_path)
    cur = conn.cursor()

    cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    cur.execute("DROP TABLE IF EXISTS drugs")
    cur.execute(CREATE_TABLE_SQL)
    cur.executemany(
        "INSERT INTO drugs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records
    )
    conn.commit()
    cur.execute("INSERT INTO drugs(drugs) VALUES('optimize')")
    conn.commit()
    conn.close()

    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(tmp_path, output_path)

    size_kb = os.path.getsize(output_path) // 1024
    print(f"  Wrote {len(records):,} rows → {output_path} ({size_kb:,} KB)")


# ── Verify ──────────────────────────────────────────────────────────────────────

def verify_index(output_path: str) -> None:
    conn = sqlite3.connect(output_path)
    cur = conn.cursor()

    cur.execute("PRAGMA user_version")
    schema_version = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM drugs")
    count = cur.fetchone()[0]

    # DIN must be present and distinct from drug_code for the vast majority of rows.
    cur.execute("SELECT COUNT(*) FROM drugs WHERE din != '' AND din IS NOT NULL")
    with_din = cur.fetchone()[0]

    # Atorvastatin should collapse to a single concept_key regardless of salt form.
    cur.execute(
        "SELECT DISTINCT concept_key FROM drugs WHERE ingredients MATCH '\"atorvastatin\"'"
    )
    ator_keys = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT drug_code, din, brand_name, descriptor, ingredients, concept_key FROM drugs "
        'WHERE brand_name MATCH \'"lipitor"\' LIMIT 8'
    )
    lipitor = cur.fetchall()

    # A combination product: expect 2+ ingredients and a "+"-joined concept_key.
    cur.execute(
        "SELECT drug_code, brand_name, ingredients, concept_key FROM drugs "
        "WHERE ingredients LIKE '%|%' LIMIT 5"
    )
    combos = cur.fetchall()

    conn.close()

    print(f"\n  Verification:")
    print(f"    Schema version (PRAGMA user_version): {schema_version}")
    print(f"    Total rows: {count:,}")
    print(f"    Rows with a DIN: {with_din:,}")
    print(f"    Distinct atorvastatin concept_keys (want 1): {ator_keys}")
    print(f"    Lipitor rows (drug_code / DIN / brand / descriptor / ingredients / concept_key):")
    for row in lipitor:
        print(f"      {row[0]:>7}  {row[1]:<10}  {row[2]:<12}  {row[3]:<18}  {row[4]:<16}  {row[5]}")
    print(f"    Sample combination products (2+ ingredients):")
    for row in combos:
        print(f"      {row[0]:>7}  {row[1]:<24}  {row[2]:<28}  {row[3]}")

    if schema_version != SCHEMA_VERSION:
        print(f"\nWARNING: user_version {schema_version} != expected {SCHEMA_VERSION}.")
        sys.exit(1)
    if count < 1000:
        print(f"\nWARNING: Only {count} rows — expected ~13,000+.")
        sys.exit(1)
    if with_din < count // 2:
        print(f"\nWARNING: only {with_din}/{count} rows carry a DIN — expected nearly all.")
        sys.exit(1)
    print(f"\n  Index looks good. Concept-grouped drug search should work in the simulator.")


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("Got2TakeMeds — DPD Drug Index Builder")
    print("=" * 60)

    drug_zip   = download_zip(DRUG_ZIP_URL,   "drug.zip (marketed products)")
    comp_zip   = download_zip(COMP_ZIP_URL,   "comp.zip (company names)")
    ingred_zip = download_zip(INGRED_ZIP_URL, "ingred.zip (strengths)")
    form_zip   = download_zip(FORM_ZIP_URL,   "form.zip (dosage forms)")
    records    = build_index(drug_zip, comp_zip, ingred_zip, form_zip)
    write_index(records, OUTPUT_PATH)
    verify_index(OUTPUT_PATH)

    print("\nDone. The weekly workflow checks it, writes manifest.json and publishes both as a release.")


if __name__ == "__main__":
    main()
