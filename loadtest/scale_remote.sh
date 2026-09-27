#!/usr/bin/env bash
# Горизонтальное масштабирование с разнесёнными машинами: генератор нагрузки (hey) — на этой машине,
# сервис (N реплик backend за nginx, docker-compose.scale.yml) — на удалённом сервере. Так генератор и сервис
# не делят CPU, кэш и сетевой стек одного хоста.
#
# Запуск:  loadtest/scale_remote.sh [длительность=15s] [соединений=128]
# Переменные: SSH_HOST (LANserver), TARGET_IP (192.168.1.13), REMOTE_DIR (каталог репозитория на сервере),
#   PORT (28180), REPLICA_CPUS ("1,5 2,6 3,7" — одно физическое ядро с SMT-соседом на реплику),
#   LB_CPUS (0,4), COUNTS ("1 2 3"), WARM (15s).
set -euo pipefail
DUR=${1:-15s}
C=${2:-128}
SSH_HOST=${SSH_HOST:-LANserver}
TARGET_IP=${TARGET_IP:-192.168.1.13}
REMOTE_DIR=${REMOTE_DIR:-hackathon/penis/hackathon}
PORT=${PORT:-28180}
REPLICA_CPUS=(${REPLICA_CPUS:-1,5 2,6 3,7})
LB_CPUS=${LB_CPUS:-0,4}   # nginx — на ядре с прерываниями сетевой карты (CPU0), реплики — на остальных
COUNTS=${COUNTS:-1 2 3}
WARM=${WARM:-15s}
HEY=${HEY:-$(command -v hey || echo "$HOME/go/bin/hey")}
OUT=${OUT:-loadtest/results-scale-remote}
P=tramscale-remote
mkdir -p "$OUT"
# hey (Go) ходит через HTTP(S)_PROXY для не-localhost адресов — для замера по LAN прокси отключаем
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

rc() { ssh "$SSH_HOST" "cd $REMOTE_DIR && $*"; }
compose() { rc "BACKEND_PORT=$PORT FRONTEND_PORT=$((PORT + 1)) docker compose -p $P -f docker-compose.yml -f docker-compose.scale.yml $*"; }
# загрузка ядер реплик из /proc/stat сервера: busy и total в jiffies по списку ядер
cpu_snap() { rc "awk '/^cpu[0-9]/{t=0; for(i=2;i<=NF;i++) t+=\$i; print substr(\$1,4), t-\$5-\$6, t}' /proc/stat"; }
busy_pct() {  # $1 — снимок до, $2 — после, $3 — ядра через запятую
  awk -v cpus="$3" 'BEGIN{n=split(cpus,c,","); for(i=1;i<=n;i++) want[c[i]]=1}
    FNR==NR{b[$1]=$2; t[$1]=$3; next} ($1 in want){db+=$2-b[$1]; dt+=$3-t[$1]} END{printf "%.0f", 100*db/dt}' "$1" "$2"; }

declare -A URLS=(
  [1_day_route_hourly]="http://$TARGET_IP:$PORT/api/v1/forecast?routes=7&from=2025-11-10&to=2025-11-10"
  [4_year_monthly]="http://$TARGET_IP:$PORT/api/v1/forecast?horizon=year&granularity=month"
)
trap 'compose down >/dev/null 2>&1 || true' EXIT

echo "| Реплик | Сценарий | RPS | Рост | p50, мс | p95, мс | p99, мс | Загрузка ядер реплик / nginx, % | Ошибки |" | tee "$OUT/summary.md"
echo "|---:|---|---:|---:|---:|---:|---:|---|---|" | tee -a "$OUT/summary.md"
declare -A BASE
for n in $COUNTS; do
  compose up -d --scale backend="$n" >/dev/null 2>&1
  ids=$(rc "docker ps -q --filter name=$P-backend | sort")
  i=0
  for id in $ids; do rc "docker update --cpuset-cpus ${REPLICA_CPUS[$i]} $id >/dev/null"; i=$((i + 1)); done
  rc "docker update --cpuset-cpus $LB_CPUS \$(docker ps -q --filter name=$P-lb) >/dev/null"
  until curl -sf "http://$TARGET_IP:$PORT/actuator/health/readiness" >/dev/null; do sleep 1; done
  sleep 3
  for k in $(printf "%s\n" "${!URLS[@]}" | sort); do
    "$HEY" -z "$WARM" -c "$C" "${URLS[$k]}" >/dev/null
    cpu_snap > "$OUT/.s0"
    "$HEY" -z "$DUR" -c "$C" "${URLS[$k]}" > "$OUT/n$n.$k.txt"
    cpu_snap > "$OUT/.s1"
    cpus=$(printf "%s," "${REPLICA_CPUS[@]:0:$n}"); cpus=${cpus%,}
    cpu="$(busy_pct "$OUT/.s0" "$OUT/.s1" "$cpus") / nginx $(busy_pct "$OUT/.s0" "$OUT/.s1" "$LB_CPUS")"
    rps=$(awk '/Requests\/sec/{printf "%.0f",$2}' "$OUT/n$n.$k.txt")
    [ "$n" = "$(echo $COUNTS | cut -d' ' -f1)" ] && BASE[$k]=$rps
    p() { awk -v q="$1%%" '$1==q{printf "%.1f",$3*1000}' "$OUT/n$n.$k.txt"; }
    codes=$(awk '/Status code distribution/{f=1;next} f&&/\[/{printf "%s%s ",$1,$2}' "$OUT/n$n.$k.txt")
    grow=$(awk -v a="$rps" -v b="${BASE[$k]}" 'BEGIN{printf "×%.2f", a/b}')
    echo "| $n | $k | $rps | $grow | $(p 50) | $(p 95) | $(p 99) | $cpu | $codes |" | tee -a "$OUT/summary.md"
  done
done
