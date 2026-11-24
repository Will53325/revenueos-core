#!/bin/bash
# Caraway capacity diagnostic for the existing RileyAI VM — READ-ONLY.
#
# Run on the VM as your normal user (no sudo needed):
#   bash diagnose_vm.sh                # 120 s sampling (default)
#   SAMPLE_SECONDS=600 bash diagnose_vm.sh   # longer sample during Riley's busy hours
#
# What it does: reads /proc, df, ps, ss, systemctl and (if you already have access)
# `docker ps`/`docker stats --no-stream`. It runs at the lowest CPU and IO priority.
# What it never does: install packages, start/stop/restart anything, change config,
# open ports, use sudo, or touch RileyAI files. Its only write is the report file
# (default: ~/caraway-diagnostic-<timestamp>.txt).
set -u
export LC_ALL=C
renice -n 19 $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true

SAMPLE_SECONDS="${SAMPLE_SECONDS:-120}"
OUT="${OUT:-$HOME/caraway-diagnostic-$(date -u +%Y%m%dT%H%M%SZ).txt}"
# Caraway's needs (see deploy/azure/README.md for how these were measured/estimated)
CARAWAY_RAM_MB=400          # scanner + dashboard peak (measured ~35 MB scan; generous margin)
OLLAMA_3B_RAM_MB=3000       # qwen2.5:3b Q4 loaded with 4k context (estimate)
OLLAMA_1B_RAM_MB=1800       # qwen2.5:1.5b Q4 loaded (estimate)
CARAWAY_DISK_GB=4           # app, database, 14 days of backups, logs
OLLAMA_DISK_GB=4            # model files + runtime
DOCKER_IMAGES_GB=2          # python slim + ollama images, only if containers are used

exec > >(tee "$OUT") 2>&1
hr(){ printf '\n==== %s ====\n' "$1"; }

hr "Caraway VM diagnostic (read-only) $(date -u +%FT%TZ)"
echo "host: $(hostname)   user: $(id -un)   sampling: ${SAMPLE_SECONDS}s"

hr "OS"
cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|VERSION_ID|ID)='
echo "kernel: $(uname -r)   arch: $(uname -m)"
echo "uptime: $(uptime -p 2>/dev/null || uptime)"
command -v systemctl >/dev/null && echo "init: systemd $(systemctl --version 2>/dev/null | head -1 | awk '{print $2}')" || echo "init: not systemd"
python3 -V 2>/dev/null || echo "python3: NOT INSTALLED (Caraway native install needs Python 3.9+)"

hr "CPU"
VCPU=$(nproc)
echo "vCPU: $VCPU"
grep -m1 'model name' /proc/cpuinfo
grep -q -w avx2 /proc/cpuinfo && AVX2=yes || AVX2=no
echo "AVX2 (needed for fast Ollama on CPU): $AVX2"
echo "load average (1/5/15 min): $(cut -d' ' -f1-3 /proc/loadavg)"

hr "Memory"
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree|Committed_AS):' /proc/meminfo
MEM_TOTAL_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)

hr "Disk"
df -hT -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null
ROOT_FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
echo "root free: ${ROOT_FREE_GB} GB"
for d in /mnt /datadrive /data; do [ -d "$d" ] && df -h "$d" | tail -1; done
echo "(Azure /mnt is usually the temporary disk: contents are lost on redeploy; never store Caraway data there)"

hr "Sampling CPU, memory, steal and pressure for ${SAMPLE_SECONDS}s"
read_cpu(){ awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $5+$6, $9}' /proc/stat; }
min_avail=999999999; max_busy=0; sum_busy=0; max_steal=0; n=0
read t0 i0 s0 < <(read_cpu)
end=$((SECONDS + SAMPLE_SECONDS))
while [ $SECONDS -lt $end ]; do
  sleep 5
  read t1 i1 s1 < <(read_cpu)
  dt=$((t1 - t0)); [ $dt -le 0 ] && continue
  busy=$(( (100 * (dt - (i1 - i0))) / dt )); steal=$(( (100 * (s1 - s0)) / dt ))
  avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  [ $avail -lt $min_avail ] && min_avail=$avail
  [ $busy -gt $max_busy ] && max_busy=$busy
  [ $steal -gt $max_steal ] && max_steal=$steal
  sum_busy=$((sum_busy + busy)); n=$((n + 1))
  t0=$t1; i0=$i1; s0=$s1
done
avg_busy=$(( n > 0 ? sum_busy / n : 0 ))
echo "CPU busy %: avg $avg_busy, max $max_busy   steal % max: $max_steal   (samples: $n)"
echo "MemAvailable min during sample: ${min_avail} MB of ${MEM_TOTAL_MB} MB"
for f in cpu memory io; do [ -r /proc/pressure/$f ] && echo "pressure $f: $(head -1 /proc/pressure/$f)"; done

hr "Top processes (who is using the VM today)"
ps -eo pid,user,pcpu,pmem,rss,etime,comm --sort=-rss | head -15
echo
ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | head -8

hr "Services (read-only listing)"
if command -v systemctl >/dev/null; then
  systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | awk '{print $1}' | grep -v -E '^(systemd-|dbus|cron|ssh|polkit|rsyslog|snapd|getty|user@|walinuxagent|hv-kvp|chrony|unattended|networkd|multipathd|udisks|ModemManager|serial-getty)' | head -40
fi
command -v pm2 >/dev/null && { echo "-- pm2 (read-only):"; pm2 jlist 2>/dev/null | python3 -c 'import json,sys; [print(" ", p["name"], p["pm2_env"]["status"]) for p in json.load(sys.stdin)]' 2>/dev/null || pm2 ls 2>/dev/null; }

hr "Containers"
if command -v docker >/dev/null; then
  echo "docker: $(docker --version 2>/dev/null)"
  if docker ps >/dev/null 2>&1; then
    docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
    docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null
    docker network ls --format '{{.Name}}' 2>/dev/null | tr '\n' ' '; echo
    DOCKER_PRESENT=yes
  else
    echo "docker installed but this user cannot query it (not in docker group). Not escalating."
    DOCKER_PRESENT=yes
  fi
else
  echo "docker: not installed (Caraway would use the native systemd install; installing Docker on a production VM changes firewall rules and is NOT recommended here)"
  DOCKER_PRESENT=no
fi

hr "Listening ports (to avoid collisions)"
listening(){
  if command -v ss >/dev/null; then ss -tlnH 2>/dev/null | awk '{print $4}'
  elif command -v netstat >/dev/null; then netstat -tln 2>/dev/null | awk 'NR>2{print $4}'
  else awk 'NR>1 && $4=="0A"{split($2,a,":"); print a[2]}' /proc/net/tcp /proc/net/tcp6 2>/dev/null | while read -r h; do printf ':%d\n' "0x$h"; done
  fi
}
listening | sort -u | tr '\n' ' '; echo
for p in 8787 11434 11435; do listening | grep -q ":$p\$" && echo "port $p: IN USE" || echo "port $p: free"; done
command -v ollama >/dev/null && echo "ollama binary already present: $(ollama --version 2>/dev/null)"

hr "Time sync"
timedatectl show -p NTPSynchronized -p Timezone 2>/dev/null || date

hr "CAPACITY VERDICT (from this sample; confirm with 30-day Azure Monitor data: diagnose_azure.sh)"
reserve=$(( MEM_TOTAL_MB * 15 / 100 )); [ $reserve -lt 1024 ] && reserve=1024
docker_gb=0; [ "${DOCKER_PRESENT:-no}" = yes ] && docker_gb=$DOCKER_IMAGES_GB
need_disk_full=$(( CARAWAY_DISK_GB + OLLAMA_DISK_GB + docker_gb ))
headroom_ram=$(( min_avail - reserve ))
cpu_ok=$([ $max_busy -lt 60 ] && [ $avg_busy -lt 35 ] && [ $max_steal -lt 10 ] && echo yes || echo no)
echo "RAM headroom after keeping ${reserve} MB reserve for RileyAI: ${headroom_ram} MB"
echo "Root disk free: ${ROOT_FREE_GB} GB (need ${need_disk_full} GB for Caraway + Ollama, plus 20% of disk kept free)"
echo "CPU quiet enough to share (avg<35%, max<60%, steal<10%): $cpu_ok   vCPU: $VCPU   AVX2: $AVX2"
verdict="RED"
reason="Not enough spare capacity: run Caraway on its own small VM (see README cost table)."
if [ $headroom_ram -ge $((CARAWAY_RAM_MB + OLLAMA_3B_RAM_MB)) ] && [ "$cpu_ok" = yes ] && [ $VCPU -ge 4 ] && [ "$AVX2" = yes ] && [ "${ROOT_FREE_GB:-0}" -ge $((need_disk_full + 5)) ]; then
  verdict="GREEN"; reason="Co-locate Caraway + Ollama (qwen2.5:3b) with strict CPU/memory caps and an off-peak inference window."
elif [ $headroom_ram -ge $((CARAWAY_RAM_MB + OLLAMA_1B_RAM_MB)) ] && [ "$cpu_ok" = yes ] && [ "$AVX2" = yes ] && [ "${ROOT_FREE_GB:-0}" -ge $((need_disk_full + 3)) ]; then
  verdict="YELLOW+AI"; reason="Co-locate with the smaller qwen2.5:1.5b model, inference only in an off-peak window, hard caps."
elif [ $headroom_ram -ge $((CARAWAY_RAM_MB + 512)) ] && [ "${ROOT_FREE_GB:-0}" -ge $((CARAWAY_DISK_GB + 3)) ]; then
  verdict="YELLOW"; reason="Co-locate Caraway WITHOUT Ollama (rules-only; AI summaries off). Add AI later on a separate VM if wanted."
fi
echo
echo "VERDICT: $verdict — $reason"
echo "This is a short sample. Do not deploy until diagnose_azure.sh confirms 30-day peaks (and B-series CPU credits) agree."
echo
echo "Report saved to: $OUT"
