"""Procurement source adapters.

Each opportunity source exposes NAME, DESCRIPTION, REQUIRES_KEY and
`fetch(conn) -> list[dict]` of normalized records (see base.normalized).
Intelligence sources expose `sync(conn)` and write their own tables.
Adapters only read public data; none of them can submit anything.
"""
from . import forecasts, nyc_city_record, sam_gov, socrata_bids, usaspending

OPPORTUNITY_SOURCES = {
    "nyc_city_record": nyc_city_record,
    "sam_gov": sam_gov,
    **socrata_bids.SOURCES,
}
INTEL_SOURCES = {
    "usaspending": usaspending,
    "forecasts": forecasts,
}
