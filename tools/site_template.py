"""Writes the portal's site template pre-filled with chosen sites, ready to
upload on the Inventory page. `sites` is a DataFrame with a site_id column
and the inventory's columns (storage.db.SITE_COLUMNS).

Kept apart from the customer-specific workbook converter: this part is the
portal's own format and knows nothing about where the sites came from.
"""
from pathlib import Path

import pandas as pd


def write_template(sites: pd.DataFrame, out: Path, region=None, city=None, limit=None, ids=None) -> int:
    """Writes the template, pre-filled with the chosen sites. Returns how many rows it holds."""
    from portal.inventory import build_template
    from storage.db import DEFAULT_REGIONS

    chosen = sites
    if region:
        chosen = chosen[chosen["region"].str.lower() == region.lower()]
    if city:
        chosen = chosen[chosen["city"].str.lower() == city.lower()]
    if ids:
        wanted = [i.strip().upper() for i in ids.split(",") if i.strip()]
        chosen = chosen.set_index("site_id").loc[[i for i in wanted if i in set(chosen["site_id"])]].reset_index()
    if limit:
        chosen = chosen.head(limit)
    regions = sorted(set(DEFAULT_REGIONS) | {r for r in sites["region"] if r})
    rows = [{k: ("" if pd.isna(v) else str(v)) for k, v in r.items()} for r in chosen.to_dict("records")]
    out.write_bytes(build_template(regions, rows))
    return len(rows)
