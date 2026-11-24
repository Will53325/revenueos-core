"""Procurement source adapters.

Each adapter exposes `fetch(conn) -> list[dict]` of normalized opportunity
records (see base.normalized), or for intelligence sources writes its own
tables. Adapters only read public data; none of them can submit anything.
"""
from . import nyc_city_record, sam_gov, usaspending

OPPORTUNITY_SOURCES = {
    "nyc_city_record": nyc_city_record,
    "sam_gov": sam_gov,
}
INTEL_SOURCES = {
    "usaspending": usaspending,
}
