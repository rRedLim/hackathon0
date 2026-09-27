#!/usr/bin/env bash
# Нагрузочный тест backend в контейнере с лимитами 2 vCPU / 2 ГБ.
# Требуется hey: go install github.com/rakyll/hey@latest
# Запуск: loadtest/run.sh [длительность=20s] [конкурентность=64]
set -euo pipefail
DUR=${1:-20s}
C=${2:-64}
HEY=${HEY:-$(command -v hey || echo "$HOME/go/bin/hey")}
# ядра сервиса; генератор нагрузки — на остальных физических ядрах (без SMT-соседей ядер сервиса),
# чтобы не отнимать у сервиса CPU
SVC_CPUS=${SVC_CPUS:-0,1}
expand() { tr ',' '\n' | while IFS=- read -r a b; do seq "$a" "${b:-$a}"; done; }
busy=$(for c in $(echo "$SVC_CPUS" | expand); do
  cat "/sys/devices/system/cpu/cpu$c/topology/thread_siblings_list" 2>/dev/null || echo "$c"; done | expand | sort -un)
free=$(seq 0 $(( $(nproc) - 1 )) | grep -vxF -f <(echo "$busy") | paste -sd, -)
TASKSET=""; command -v taskset >/dev/null && [ -n "$free" ] && TASKSET="taskset -c $free"
echo "сервис: CPU $SVC_CPUS (с SMT-соседями: $(echo $busy | tr ' ' ',')); генератор нагрузки: CPU ${free:-любые}" >&2
IMG=${IMG:-tram-forecast-backend:1.0}
NAME=${NAME:-tram-loadtest}
PORT=${PORT:-18080}
B="http://localhost:$PORT/api/v1"
OUT=${OUT:-loadtest/results}
mkdir -p "$OUT"

docker rm -f $NAME >/dev/null 2>&1 || true
# --cpuset-cpus жёстко ограничивает 2 ядрами (квота --cpus игнорируется некоторыми планировщиками, напр. sched_ext)
docker run -d --name $NAME --cpus=2 --cpuset-cpus="$SVC_CPUS" --memory=2g --memory-swap=2g -p $PORT:8080 "$IMG" >/dev/null
trap 'docker rm -f $NAME >/dev/null 2>&1 || true' EXIT
until curl -sf "http://localhost:$PORT/actuator/health/readiness" >/dev/null; do sleep 1; done

declare -A URLS=(
  [1_day_route_hourly]="$B/forecast?routes=7&from=2025-11-10&to=2025-11-10"
  [2_scenario_month_daily]="$B/forecast?granularity=day&from=2025-12-01&to=2025-12-31&precip=2025-12-05:15&monthMult=12:1.03&routeMult=17:0.95&event=7%7C50:2025-12-20:2025-12-21:0.5:10-18"
  [3_stop_segment_hourly]="$B/forecast?segment=1:0:3-10&from=2025-11-10&to=2025-11-16"
  [4_year_monthly]="$B/forecast?horizon=year&granularity=month"
  [5_map_day_all_stops]="$B/map?date=2025-11-10"
  [6_summary_day]="$B/summary?date=2025-11-10"
)

cpu_usec() { docker exec $NAME cat /sys/fs/cgroup/cpu.stat | awk '/usage_usec/{print $2}'; }
mem() { docker exec $NAME cat /sys/fs/cgroup/memory.current; }

# прогрев JIT
for k in "${!URLS[@]}"; do $TASKSET "$HEY" -z 5s -c 32 "${URLS[$k]}" >/dev/null; done

printf "| %-24s | %9s | %8s | %8s | %8s | %7s | %8s | %s |\n" "сценарий" "RPS" "p50, мс" "p95, мс" "p99, мс" "CPU, %" "RAM, МБ" "коды"
for k in $(printf "%s\n" "${!URLS[@]}" | sort); do
  for mode in max rate; do
    args=(-z "$DUR" -c "$C")
    label="$k (max)"
    if [ $mode = rate ]; then args=(-z "$DUR" -c 50 -q 10); label="$k (500 rps)"; fi
    c0=$(cpu_usec); t0=$(date +%s%N)
    $TASKSET "$HEY" "${args[@]}" "${URLS[$k]}" > "$OUT/$k.$mode.txt"
    c1=$(cpu_usec); t1=$(date +%s%N)
    cpu=$(awk -v a="$c0" -v b="$c1" -v s="$t0" -v e="$t1" 'BEGIN{printf "%.0f", (b-a)/((e-s)/1000)/2*100}')
    rps=$(awk '/Requests\/sec/{printf "%.0f",$2}' "$OUT/$k.$mode.txt")
    p() { awk -v q="$1%%" '$1==q{printf "%.1f",$3*1000}' "$OUT/$k.$mode.txt"; }
    codes=$(awk '/Status code distribution/{f=1;next} f&&/\[/{printf "%s%s ",$1,$2}' "$OUT/$k.$mode.txt")
    printf "| %-24s | %9s | %8s | %8s | %8s | %7s | %8s | %s |\n" "$label" "$rps" "$(p 50)" "$(p 95)" "$(p 99)" "$cpu" "$(( $(mem) / 1048576 ))" "$codes"
  done
done
