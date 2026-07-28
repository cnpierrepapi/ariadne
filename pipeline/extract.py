"""Pull real US Census ACS microdata and land it in the warehouse as raw tables.

Nothing here is generated. `folktables` fetches Public Use Microdata Sample records
straight from the Census Bureau, and we load them the way a warehouse actually
receives source data: separate person and household records that have to be joined
on a household serial number, plus a small geography reference table.

That shape matters. A single flat table would give a lineage graph with nothing in
it. Real joins across real source tables are what make the downstream lineage worth
walking, and they are what a census warehouse genuinely looks like.

The person and household record layouts are the Census Bureau's own; the column
names are PUMS codes, kept as-is rather than prettified, because an agent reading
this catalog should face the same naming a real analyst does.
"""

from __future__ import annotations

import argparse
import io
import os
import re

import pandas as pd
import psycopg2

WAREHOUSE = os.environ.get(
    "ARIADNE_WAREHOUSE_URL", "postgresql://ariadne:ariadne@localhost:5433/warehouse"
)

# Person-level PUMS columns we land. The first block is identity and geography, the
# second is what the canonical ACSIncome task actually models, the third is the
# personal and protected attributes a governance team would tag on sight, and the
# fourth is coverage and benefits.
#
# That fourth block is here because the disability literature points at it, not
# because a model needs it. Public health coverage before 65 is largely disability
# linked: Medicare under 65 requires SSDI, end stage renal disease or ALS, and the
# Supplemental Security Income programme is a disability and old age programme. A
# warehouse that serves benefits administration would carry these as a matter of
# course, which is exactly what makes them worth watching.
PERSON_COLS = [
    "SERIALNO", "SPORDER", "ST", "PUMA",
    "AGEP", "COW", "SCHL", "MAR", "OCCP", "POBP", "RELP", "WKHP", "PINCP",
    "SEX", "RAC1P", "DIS", "CIT", "NATIVITY", "ANC1P",
    "PUBCOV", "HINS3", "HINS4", "SSIP",
]

HOUSEHOLD_COLS = [
    "SERIALNO", "ST", "PUMA",
    "NP", "HINCP", "TEN", "VEH", "YBL", "BDSP",
]

# FIPS state codes, the Census Bureau's own. A reference dimension, the kind every
# warehouse has and nobody documents.
STATES = {
    1: "Alabama", 2: "Alaska", 4: "Arizona", 5: "Arkansas", 6: "California",
    8: "Colorado", 9: "Connecticut", 10: "Delaware", 11: "District of Columbia",
    12: "Florida", 13: "Georgia", 15: "Hawaii", 16: "Idaho", 17: "Illinois",
    18: "Indiana", 19: "Iowa", 20: "Kansas", 21: "Kentucky", 22: "Louisiana",
    23: "Maine", 24: "Maryland", 25: "Massachusetts", 26: "Michigan",
    27: "Minnesota", 28: "Mississippi", 29: "Missouri", 30: "Montana",
    31: "Nebraska", 32: "Nevada", 33: "New Hampshire", 34: "New Jersey",
    35: "New Mexico", 36: "New York", 37: "North Carolina", 38: "North Dakota",
    39: "Ohio", 40: "Oklahoma", 41: "Oregon", 42: "Pennsylvania",
    44: "Rhode Island", 45: "South Carolina", 46: "South Dakota", 47: "Tennessee",
    48: "Texas", 49: "Utah", 50: "Vermont", 51: "Virginia", 53: "Washington",
    54: "West Virginia", 55: "Wisconsin", 56: "Wyoming", 72: "Puerto Rico",
}

REGIONS = {
    "Northeast": {9, 23, 25, 33, 34, 36, 42, 44, 50},
    "Midwest": {17, 18, 19, 20, 26, 27, 29, 31, 38, 39, 46, 55},
    "South": {1, 5, 10, 11, 12, 13, 21, 22, 24, 28, 37, 40, 45, 47, 48, 51, 54},
    "West": {2, 4, 6, 8, 15, 16, 30, 32, 35, 41, 49, 53},
    "Territory": {72},
}


def _region(st: int) -> str:
    for name, codes in REGIONS.items():
        if st in codes:
            return name
    return "Unknown"


def fetch(states: list[str], year: str, horizon: str, cache: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    from folktables import ACSDataSource

    person = ACSDataSource(survey_year=year, horizon=horizon, survey="person",
                           root_dir=cache).get_data(states=states, download=True)
    household = ACSDataSource(survey_year=year, horizon=horizon, survey="household",
                              root_dir=cache).get_data(states=states, download=True)
    return person, household


def _select(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    have = [c for c in cols if c in df.columns]
    missing = sorted(set(cols) - set(have))
    if missing:
        print(f"  note: not in this vintage, skipped: {', '.join(missing)}")
    return df[have].copy()


def _pg_type(series: pd.Series) -> str:
    """A column type a warehouse would actually declare, not an inferred blob."""
    kind = series.dtype.kind
    if kind in "iu":
        return "bigint"
    if kind == "f":
        return "double precision"
    if kind == "b":
        return "boolean"
    return "text"


def _copy(conn, table: str, frame: pd.DataFrame) -> None:
    """Create the table with explicit types and bulk load it through COPY.

    Deliberately not pandas.to_sql. That path needs pandas and SQLAlchemy to agree
    on a version, and DataHub pins SQLAlchemy 1.4 while pandas 2.3 wants its own,
    so the detection silently falls through to the DBAPI branch and fails. COPY has
    no such coupling, is an order of magnitude faster, and makes the declared column
    types ours rather than inferred, which is what then shows up in the catalog.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise ValueError(f"refusing to build DDL for {table!r}")
    cols = ", ".join(f'"{c}" {_pg_type(frame[c])}' for c in frame.columns)
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    with conn.cursor() as cur:
        # CASCADE because the staging models are views over these tables, so a plain
        # drop fails on every run after the first and the extract is only re-runnable
        # by hand. Reloading a source table genuinely does invalidate the views built
        # on it, and `dbt run` rebuilds them immediately afterwards, which is the step
        # that follows this one in the pipeline.
        cur.execute(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
        cur.execute(f'CREATE TABLE public."{table}" ({cols})')
        cur.copy_expert(
            f'COPY public."{table}" FROM STDIN WITH (FORMAT csv, NULL \'\')', buf
        )


def load(person: pd.DataFrame, household: pd.DataFrame, year: str) -> None:
    p = _select(person, PERSON_COLS)
    p.columns = [c.lower() for c in p.columns]
    p["survey_year"] = int(year)

    h = _select(household, HOUSEHOLD_COLS)
    h.columns = [c.lower() for c in h.columns]
    h["survey_year"] = int(year)
    # one household record per serial number; PUMS repeats it per person in some
    # vintages, and a duplicated join key would silently multiply every downstream row
    h = h.drop_duplicates(subset=["serialno"])

    geo = pd.DataFrame(
        [{"state_code": k, "state_name": v, "census_region": _region(k)}
         for k, v in STATES.items()]
    )

    with psycopg2.connect(WAREHOUSE) as conn:
        for name, frame in (("raw_person", p), ("raw_household", h),
                            ("raw_geography", geo)):
            _copy(conn, name, frame)
        conn.commit()

    print(f"  raw_person     {len(p):>8,} rows, {len(p.columns)} cols")
    print(f"  raw_household  {len(h):>8,} rows, {len(h.columns)} cols")
    print(f"  raw_geography  {len(geo):>8,} rows, {len(geo.columns)} cols")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--states", nargs="+", default=["CA"])
    ap.add_argument("--year", default="2018")
    ap.add_argument("--horizon", default="1-Year")
    ap.add_argument("--cache", default=os.path.expanduser("~/ariadne/.acs-cache"))
    args = ap.parse_args()

    print(f"fetching ACS {args.year} {args.horizon} for {', '.join(args.states)}")
    person, household = fetch(args.states, args.year, args.horizon, args.cache)
    print(f"  census returned {len(person):,} person and {len(household):,} household records")
    load(person, household, args.year)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
