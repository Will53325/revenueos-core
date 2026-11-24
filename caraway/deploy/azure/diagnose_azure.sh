#!/bin/bash
# Caraway capacity check from Azure's side — READ-ONLY.
#
# Run from Azure Cloud Shell (bash) or any machine with `az login` done:
#   bash diagnose_azure.sh <resource-group> <vm-name> [days=30]
#
# Uses only `az ... show/list` and Azure Monitor metric reads. It does not start, stop,
# resize, tag, or reconfigure anything, and does not touch RileyAI.
# Output: a summary plus a verdict, saved to ./caraway-azure-diagnostic-<timestamp>.txt
set -u
RG="${1:?usage: diagnose_azure.sh <resource-group> <vm-name> [days]}"
VM="${2:?usage: diagnose_azure.sh <resource-group> <vm-name> [days]}"
DAYS="${3:-30}"
OUT="caraway-azure-diagnostic-$(date -u +%Y%m%dT%H%M%SZ).txt"
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v az >/dev/null || { echo "Azure CLI not found. Use Azure Cloud Shell (portal > >_ icon)."; exit 1; }
az account show >/dev/null 2>&1 || { echo "Run: az login"; exit 1; }
exec > >(tee "$OUT") 2>&1
hr(){ printf '\n==== %s ====\n' "$1"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

hr "VM $VM in $RG ($(date -u +%FT%TZ))"
az vm show -g "$RG" -n "$VM" -o json > "$TMP/vm.json" || exit 1
VMID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' "$TMP/vm.json")
python3 - "$TMP/vm.json" <<'PY'
import json, sys
v = json.load(open(sys.argv[1]))
hp, sp = v["hardwareProfile"], v["storageProfile"]
img = sp.get("imageReference") or {}
print("size:", hp["vmSize"], "| location:", v["location"], "| priority:", v.get("priority") or "Regular")
print("os:", sp["osDisk"]["osType"], "|", img.get("publisher"), img.get("offer"), img.get("sku"), img.get("exactVersion") or img.get("version"))
print("os disk:", sp["osDisk"].get("diskSizeGb"), "GB", (sp["osDisk"].get("managedDisk") or {}).get("storageAccountType"))
for d in sp.get("dataDisks", []):
    print("data disk:", d["name"], d.get("diskSizeGb"), "GB lun", d["lun"])
PY
SIZE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["hardwareProfile"]["vmSize"])' "$TMP/vm.json")
LOC=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["location"])' "$TMP/vm.json")
echo "power state: $(az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus | [0]" -o tsv)"
az vm list-sizes -l "$LOC" --query "[?name=='$SIZE'] | [0].{vCPU:numberOfCores, memoryMB:memoryInMb, maxDataDisks:maxDataDiskCount, tempDiskMB:resourceDiskSizeInMb}" -o table

hr "Azure Monitor: last $DAYS days (hourly)"
START=$(date -u -d "-$DAYS days" +%FT%TZ 2>/dev/null || date -u -v-"$DAYS"d +%FT%TZ)
METRICS="Percentage CPU,Available Memory Bytes,OS Disk IOPS Consumed Percentage,CPU Credits Remaining,CPU Credits Consumed"
az monitor metrics list --resource "$VMID" --metric "$METRICS" --interval PT1H --start-time "$START" \
   --aggregation Average Maximum Minimum -o json > "$TMP/metrics.json" 2>"$TMP/metrics.err" || cat "$TMP/metrics.err"
python3 - "$TMP/metrics.json" "$TMP/vm.json" "$SIZE" <<'PY'
import json, sys, statistics
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    print("no metric data"); sys.exit(0)
def series(name, agg):
    for v in m.get("value", []):
        if v["name"]["value"] == name:
            return [p[agg] for ts in v["timeseries"] for p in ts["data"] if p.get(agg) is not None]
    return []
def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None
cpu_avg, cpu_max = series("Percentage CPU", "average"), series("Percentage CPU", "maximum")
mem_min = series("Available Memory Bytes", "minimum")
credits = series("CPU Credits Remaining", "minimum")
summary = {}
if cpu_avg:
    summary.update(cpu_avg=round(statistics.mean(cpu_avg), 1), cpu_p95_hourly_avg=round(pct(cpu_avg, .95), 1),
                   cpu_p99_hourly_max=round(pct(cpu_max, .99), 1), cpu_peak=round(max(cpu_max), 1))
    print(f"CPU %: mean {summary['cpu_avg']} | p95 of hourly avg {summary['cpu_p95_hourly_avg']} | "
          f"p99 of hourly max {summary['cpu_p99_hourly_max']} | peak {summary['cpu_peak']}  ({len(cpu_avg)} hours)")
if mem_min:
    summary["mem_avail_min_mb"] = int(min(mem_min) / 2**20); summary["mem_avail_p05_mb"] = int(pct(mem_min, .05) / 2**20)
    print(f"Available memory: lowest {summary['mem_avail_min_mb']} MB | 5th percentile {summary['mem_avail_p05_mb']} MB")
else:
    print("Available memory metric missing (older VM agent). Rely on diagnose_vm.sh sampling.")
iops = series("OS Disk IOPS Consumed Percentage", "maximum")
if iops:
    print(f"OS disk IOPS consumed %: p95 {round(pct(iops, .95), 1)} | peak {round(max(iops), 1)}")
if credits:
    summary["credits_min"] = round(min(credits), 1)
    print(f"B-series CPU credits remaining: lowest {summary['credits_min']}  (0 means the VM was throttled to baseline)")
json.dump(summary, open(sys.argv[1] + ".summary", "w"))
PY

hr "Disks attached to the VM"
az disk list -g "$RG" --query "[?managedBy && contains(managedBy, '/$VM')].{name:name, sizeGB:diskSizeGb, sku:sku.name, state:diskState}" -o table

hr "Network exposure (NSG inbound rules) — Caraway needs NO new inbound rules"
NIC=$(az vm show -g "$RG" -n "$VM" --query "networkProfile.networkInterfaces[0].id" -o tsv)
NSG=$(az network nic show --ids "$NIC" --query "networkSecurityGroup.id" -o tsv 2>/dev/null)
if [ -n "$NSG" ]; then
  az network nsg rule list --ids "$NSG" 2>/dev/null --query "[?direction=='Inbound'].{name:name, port:destinationPortRange, src:sourceAddressPrefix, access:access, prio:priority}" -o table \
  || az network nsg show --ids "$NSG" --query "securityRules[?direction=='Inbound'].{name:name, port:destinationPortRange, src:sourceAddressPrefix, access:access}" -o table
else
  echo "No NIC-level NSG (check subnet NSG in the portal)."
fi

hr "Backup status (read-only)"
az backup protection check-vm --vm "$VMID" -o tsv 2>/dev/null && echo "^ Recovery Services vault protecting this VM (empty = none)" || echo "Could not query Azure Backup (permissions or not configured)."

hr "Live list prices ($LOC)"
python3 "$HERE/cost_estimate.py" --region "$LOC" --current "$SIZE" 2>/dev/null || echo "cost_estimate.py failed (needs internet access to prices.azure.com)"

hr "VERDICT (Azure Monitor side)"
python3 - "$TMP/metrics.json.summary" "$SIZE" "$LOC" <<'PY'
import json, subprocess, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    s = {}
size, loc = sys.argv[2], sys.argv[3]
info = json.loads(subprocess.run(["az", "vm", "list-sizes", "-l", loc, "--query", f"[?name=='{size}'] | [0]", "-o", "json"],
                                 capture_output=True, text=True).stdout or "{}")
total_mb, vcpu = info.get("memoryInMb", 0), info.get("numberOfCores", 0)
reserve = max(1024, int(total_mb * 0.15))
avail = s.get("mem_avail_p05_mb")
CARAWAY, OLLAMA3B, OLLAMA1B = 400, 3000, 1800
burstable = size.split("_")[1].startswith("B") if "_" in size else False
reasons, verdict = [], "RED"
cpu_quiet = s.get("cpu_p95_hourly_avg") is not None and s["cpu_p95_hourly_avg"] < 35 and s.get("cpu_p99_hourly_max", 100) < 75
if burstable and s.get("credits_min") is not None and s["credits_min"] < 50:
    reasons.append(f"B-series credits fell to {s['credits_min']}: extra CPU work would push RileyAI into throttling.")
    cpu_quiet = False
if avail is None:
    reasons.append("No memory metric: use diagnose_vm.sh results for RAM.")
else:
    head = avail - reserve
    reasons.append(f"RAM headroom (5th-percentile available minus {reserve} MB reserve for RileyAI): {head} MB")
    if head >= CARAWAY + OLLAMA3B and cpu_quiet and vcpu >= 4:
        verdict = "GREEN"
    elif head >= CARAWAY + OLLAMA1B and cpu_quiet:
        verdict = "YELLOW+AI"
    elif head >= CARAWAY + 512:
        verdict = "YELLOW"
reasons.append(f"vCPU {vcpu}, CPU quiet enough: {cpu_quiet}, burstable: {burstable}")
for r in reasons:
    print("-", r)
print({"GREEN": "\nVERDICT: GREEN — co-locate Caraway + Ollama (qwen2.5:3b) with hard caps and an off-peak AI window.",
       "YELLOW+AI": "\nVERDICT: YELLOW+AI — co-locate with qwen2.5:1.5b, off-peak window, hard caps.",
       "YELLOW": "\nVERDICT: YELLOW — co-locate Caraway without Ollama (rules-only).",
       "RED": "\nVERDICT: RED — do not co-locate. Use a separate small VM (see price table)."}[verdict])
print("Combine with diagnose_vm.sh: the stricter of the two verdicts wins.")
PY
echo; echo "Saved: $OUT"
