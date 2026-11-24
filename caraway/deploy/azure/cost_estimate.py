#!/usr/bin/env python3
"""Live Azure list prices for Caraway hosting options (read-only, no login needed).

Uses the public Azure Retail Prices API (https://prices.azure.com). Prints monthly
cost (730 h) for: the current VM size (if given), candidate sizes, spot and reserved
pricing where published, a small data disk, and blob backup storage.
List prices only: your agreement, credits, taxes and existing reservations change the real bill.

  python3 cost_estimate.py --region eastus --current Standard_B2ms
"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://prices.azure.com/api/retail/prices"
HOURS = 730
CANDIDATES = [
    # (sku, vCPU, RAM GB, note)
    ("Standard_B1ms", 1, 2, "Caraway without local AI"),
    ("Standard_B2ats_v2", 2, 1, "too little RAM for Ollama"),
    ("Standard_B2als_v2", 2, 4, "Caraway + qwen2.5:1.5b"),
    ("Standard_B2s", 2, 4, "Caraway + qwen2.5:1.5b"),
    ("Standard_B2as_v2", 2, 8, "Caraway + qwen2.5:3b"),
    ("Standard_B2ms", 2, 8, "Caraway + qwen2.5:3b"),
    ("Standard_B4als_v2", 4, 8, "faster inference"),
    ("Standard_D2as_v5", 2, 8, "non-burstable"),
    ("Standard_D4as_v5", 4, 16, "non-burstable, room for 7B models"),
]


def _get(url: str) -> dict:
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise
            time.sleep(min(60, 5 * 2 ** attempt))
    raise RuntimeError("unreachable")


def query(flt: str) -> list:
    items, url = [], API + "?" + urllib.parse.urlencode({"$filter": flt})
    while url:
        time.sleep(1.0)  # be polite to the public API
        d = _get(url)
        items += d.get("Items", [])
        url = d.get("NextPageLink")
    return items


def vm_prices(region: str, sku: str) -> dict:
    out = {}
    for i in query(f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and armSkuName eq '{sku}'"):
        if (not i["productName"].startswith("Virtual Machines") or "Windows" in i["productName"]
                or "Low Priority" in i["skuName"] or i["type"] not in ("Consumption", "Reservation")):
            continue
        kind = i["type"]
        if kind == "Consumption":
            key = "spot" if "Spot" in i["skuName"] else "payg"
            out[key] = round(i["retailPrice"] * HOURS, 2)
        elif kind == "Reservation":
            term = i.get("reservationTerm", "")
            years = 3 if "3" in term else 1
            out[f"reserved_{years}y"] = round(i["retailPrice"] / (12 * years), 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="eastus")
    ap.add_argument("--current", help="current VM size, e.g. Standard_B2ms")
    a = ap.parse_args()
    print(f"Azure list prices, {a.region}, Linux, USD per month (730 h). Verify in the Azure pricing calculator.\n")
    print(f"{'size':<20}{'vCPU':>5}{'RAM':>5}  {'pay-go':>8}{'1y RI':>8}{'3y RI':>8}{'spot':>8}  note")
    rows = list(CANDIDATES)
    if a.current and a.current not in [r[0] for r in rows]:
        rows.insert(0, (a.current, "?", "?", "CURRENT (Riley VM)"))
    for sku, cpu, ram, note in rows:
        p = vm_prices(a.region, sku)
        f = lambda k: f"{p[k]:>8.2f}" if k in p else f"{'-':>8}"
        tag = "  <- current" if sku == a.current else ""
        print(f"{sku:<20}{cpu:>5}{ram:>5}  {f('payg')}{f('reserved_1y')}{f('reserved_3y')}{f('spot')}  {note}{tag}")
    disk = [i for i in query(f"serviceName eq 'Storage' and armRegionName eq '{a.region}' and skuName eq 'E4 LRS' "
                             "and productName eq 'Standard SSD Managed Disks'") if i["unitOfMeasure"] == "1/Month"]
    blob = [i for i in query(f"serviceName eq 'Storage' and armRegionName eq '{a.region}' and productName eq 'Blob Storage' "
                             "and skuName eq 'Cool LRS' and meterName eq 'Cool LRS Data Stored'")]
    print()
    if disk:
        print(f"Standard SSD E4 (32 GiB) data disk: ${disk[0]['retailPrice']:.2f}/month")
    if blob:
        tiers = sorted(blob, key=lambda i: i.get("tierMinimumUnits", 0))
        gb = tiers[0]["retailPrice"]
        print(f"Blob Cool LRS backups: ${gb:.4f}/GB-month -> 14 x 2 MB daily backups ~= ${gb * 0.03:.4f}/month (+ tiny write fees)")
    print("\nCo-locating on the existing VM adds no compute cost; only an optional data disk and backup storage.")


if __name__ == "__main__":
    main()
