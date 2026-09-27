#!/usr/bin/env bash
# Горизонтальное масштабирование: пропускная способность через nginx-балансировщик при 1, 2, 3 репликах backend.
# Каждая реплика — отдельный контейнер 2 логических CPU / 2 ГБ, закреплённый за своими ядрами (--cpuset-cpus);
# балансировщик (deploy/nginx-lb.conf, тот же конфиг, что в docker-compose.scale.yml) и генератор нагрузки —
# на других физических ядрах. Требуется hey: go install github.com/rakyll/hey@latest
# Запуск: loadtest/scale.sh [длительность=20s] [конкурентность=128]
#
# Раскладка по умолчанию для 8 ядер / 16 потоков (SMT-соседи — N и N+8):
#   реплика i  → физическое ядро i (логические i,i+8): 0,8 | 1,9 | 2,10
#   балансировщик → физические ядра 3,4 (3,4,11,12)
#   hey        → физические ядра 5,6,7 (5,6,7,13,14,15)
# Ядра не пересекаются даже по SMT, поэтому реплики, балансировщик и генератор не отнимают CPU друг у друга.
set -euo pipefail
cd "$(dirname "$0")/.."
DUR=${1:-20s}
C=${2:-128}
HEY=${HEY:-$(command -v hey || echo "$HOME/go/bin/hey")}
IMG=${IMG:-tram-forecast-backend:1.0}
LB_IMG=${LB_IMG:-nginx:1.27-alpine}
read -r -a REPLICA_CPUS <<< "${REPLICA_CPUS:-0,8 1,9 2,10}"
LB_CPUS=${LB_CPUS:-3,4,11,12}
HEY_CPUS=${HEY_CPUS:-5,6,7,13,14,15}
NMAX=${NMAX:-${#REPLICA_CPUS[@]}}
PORT=${PORT:-18090}
NET=tramscale-lt
P=tramscale-lt
OUT=${OUT:-loadtest/results-scale}
B="http://127.0.0.1:$PORT/api/v1"
mkdir -p "$OUT"
export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}" NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"

declare -A URLS=(
  [1_day_route_hourly]="$B/forecast?routes=7&from=2025-11-10&to=2025-11-10"
  [4_year_monthly]="$B/forecast?horizon=year&granularity=month"
)

cleanup() { docker rm -f $(docker ps -aq --filter "name=^$P-") >/dev/null 2>&1 || true; docker network rm $NET >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create $NET >/dev/null
echo "реплики: CPU ${REPLICA_CPUS[*]:0:$NMAX}; балансировщик: CPU $LB_CPUS; hey: CPU $HEY_CPUS" >&2

cpu_usec() { docker exec "$1" cat /sys/fs/cgroup/cpu.stat | awk '/usage_usec/{print $2}'; }
# занятость ядер генератора по /proc/stat: "busy total" в тиках
host_ticks() { awk -v list=",$HEY_CPUS," '$1 ~ /^cpu[0-9]+$/ { n=substr($1,4); if (index(list, "," n ",")) { b+=$2+$3+$4+$7+$8; t+=$2+$3+$4+$5+$6+$7+$8 } } END { print b, t }' /proc/stat; }
fc_count() { docker exec "$1" wget -qO- "http://127.0.0.1:8080/actuator/metrics/http.server.requests?tag=uri:/api/v1/forecast" 2>/dev/null \
  | grep -o '"COUNT","value":[0-9.]*' | awk -F: '{printf "%d", $2}' || echo 0; }

docker run -d --name $P-lb --network $NET --cpuset-cpus="$LB_CPUS" --memory=256m -p $PORT:8080 \
  -v "$PWD/deploy/nginx-lb.conf:/etc/nginx/conf.d/default.conf:ro" "$LB_IMG" >/dev/null

SUMMARY="$OUT/summary.md"
{
  echo "# Масштабирование: 1–$NMAX реплик за nginx ($(date '+%d.%m.%Y %H:%M'))"
  echo
  echo "Реплика: 2 логических CPU / 2 ГБ; реплики на CPU ${REPLICA_CPUS[*]:0:$NMAX}; балансировщик на $LB_CPUS; hey на $HEY_CPUS."
  echo "Режим max: $C соединений, $DUR на замер после прогрева. CPU реплики — в % от её 2 CPU, балансировщика — в % от одного CPU."
  echo
  echo "| реплик | сценарий | RPS | p50, мс | p95, мс | p99, мс | CPU реплик, % | CPU lb, % | загрузка ядер hey, % | доли запросов по репликам | коды |"
  echo "|---:|---|---:|---:|---:|---:|---|---:|---:|---|---|"
} > "$SUMMARY"

for n in $(seq 1 "$NMAX"); do
  i=$((n - 1))
  docker run -d --name $P-b$n --network $NET --network-alias backend --cpuset-cpus="${REPLICA_CPUS[$i]}" \
    --memory=2g --memory-swap=2g "$IMG" >/dev/null
  until docker exec $P-b$n wget -qO- http://127.0.0.1:8080/actuator/health/readiness 2>/dev/null | grep -q UP; do sleep 1; done
  # балансировщик перечитывает DNS раз в 5 с (resolve); ждём, пока в ответах появятся все n реплик
  for _ in $(seq 30); do
    seen=$(for _ in $(seq $((n * 4))); do curl -s -o /dev/null -D - "$B/meta" | awk 'tolower($1)=="x-upstream:"{print $2}'; done | sort -u | wc -l)
    [ "$seen" -ge "$n" ] && break; sleep 1
  done
  echo "== реплик: $n (балансировщик видит $seen)" >&2
  # прогрев JIT: новой реплике нужно ~30–40 с под нагрузкой, иначе она медленнее прогретых и тормозит round-robin
  for k in "${!URLS[@]}"; do taskset -c "$HEY_CPUS" "$HEY" -z "${WARM:-20s}" -c "$C" "${URLS[$k]}" >/dev/null; done

  for k in $(printf "%s\n" "${!URLS[@]}" | sort); do
    f="$OUT/n$n.$k.txt"
    declare -a c0=() r0=()
    for j in $(seq 1 $n); do c0[$j]=$(cpu_usec $P-b$j); r0[$j]=$(fc_count $P-b$j); done
    l0=$(cpu_usec $P-lb); read -r hb0 ht0 < <(host_ticks); t0=$(date +%s%N)
    taskset -c "$HEY_CPUS" "$HEY" -z "$DUR" -c "$C" "${URLS[$k]}" > "$f"
    t1=$(date +%s%N); read -r hb1 ht1 < <(host_ticks); l1=$(cpu_usec $P-lb)
    cpus=""; dist=""; tot=0
    declare -a d=()
    for j in $(seq 1 $n); do
      c1=$(cpu_usec $P-b$j); d[$j]=$(( $(fc_count $P-b$j) - r0[$j] )); tot=$((tot + d[$j]))
      cpus+="$(awk -v a="${c0[$j]}" -v b="$c1" -v s="$t0" -v e="$t1" 'BEGIN{printf "%.0f", (b-a)/((e-s)/1000)/2*100}') / "
    done
    for j in $(seq 1 $n); do dist+="$(awk -v a="${d[$j]}" -v t="$tot" 'BEGIN{printf "%.1f", t ? a*100/t : 0}') % / "; done
    lbcpu=$(awk -v a="$l0" -v b="$l1" -v s="$t0" -v e="$t1" 'BEGIN{printf "%.0f", (b-a)/((e-s)/1000)*100}')
    heycpu=$(awk -v a="$hb0" -v b="$hb1" -v c="$ht0" -v e="$ht1" 'BEGIN{printf "%.0f", (b-a)*100/(e-c)}')
    rps=$(awk '/Requests\/sec/{printf "%.0f",$2}' "$f")
    p() { awk -v q="$1%%" '$1==q{printf "%.1f",$3*1000}' "$f"; }
    codes=$(awk '/Status code distribution/{f=1;next} f&&/\[/{printf "%s%s ",$1,$2}' "$f")
    {
      echo "replicas=$n cpu_replicas=${cpus% / } cpu_lb=$lbcpu hey_cores_busy=$heycpu forecast_requests_per_replica=${d[*]:1}"
    } >> "$f"
    row="| $n | $k | $rps | $(p 50) | $(p 95) | $(p 99) | ${cpus% / } | $lbcpu | $heycpu | ${dist% / } | $codes|"
    echo "$row" | tee -a "$SUMMARY"
  done
done
