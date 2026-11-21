#!/usr/bin/env bash
# Local test stack with production-sized limits. It never touches the developer stack (postgres-dev, keycloak-dev,
# helti-app): the developer database is only read with pg_dump to clone the catalog and realm.
#
#   ./local/perf-stack.sh clone   # pg_dump dietdb + keycloakdb from the running postgres-dev (read only)
#   ./local/perf-stack.sh up      # start the stack, restore the clone on first start, wait for the backend
#   ./local/perf-stack.sh stats <seconds> <file.csv>   # sample docker stats of the stack
#   ./local/perf-stack.sh down    # remove the stack's containers, network and volume (diecik-perf only)
set -euo pipefail

CALLER_DIR=$PWD
cd "$(dirname "$0")"
COMPOSE=(docker compose -f docker-compose.yml)
DUMPS="${PERF_DUMP_DIR:-../results/local/db}"
SOURCE_CONTAINER="${PERF_SOURCE_CONTAINER:-postgres-dev}"

wait_for() {
  local what=$1 url=$2 tries=${3:-90}
  for _ in $(seq "$tries"); do
    if curl -fsS -o /dev/null "$url"; then echo "$what is up"; return 0; fi
    sleep 2
  done
  echo "$what did not come up: $url" >&2
  return 1
}

case "${1:-}" in
  clone)
    mkdir -p "$DUMPS"
    for db in dietdb keycloakdb; do
      docker exec "$SOURCE_CONTAINER" pg_dump -U postgres -Fc "$db" > "$DUMPS/$db.dump"
      echo "dumped $db: $(du -h "$DUMPS/$db.dump" | cut -f1)"
    done
    ;;
  up)
    "${COMPOSE[@]}" up -d postgres
    "${COMPOSE[@]}" exec -T postgres sh -c 'until pg_isready -U postgres >/dev/null; do sleep 1; done'
    restored=$("${COMPOSE[@]}" exec -T postgres psql -U postgres -d dietdb -Atc \
      "select count(*) from information_schema.tables where table_name = 'databasechangelog'")
    if [ "$restored" = "0" ]; then
      for db in dietdb keycloakdb; do
        [ -f "$DUMPS/$db.dump" ] || { echo "Missing $DUMPS/$db.dump, run: $0 clone" >&2; exit 1; }
        "${COMPOSE[@]}" exec -T postgres pg_restore -U postgres -d "$db" --no-owner --exit-on-error < "$DUMPS/$db.dump"
        echo "restored $db"
      done
    fi
    mkdir -p ../results/local/jvm
    "${COMPOSE[@]}" up -d keycloak backend
    wait_for keycloak http://localhost:18080/realms/health-realm
    wait_for backend http://localhost:18081/public/health 120
    ;;
  stats)
    seconds=${2:?seconds}; file=${3:?file.csv}
    case "$file" in /*) ;; *) file="$CALLER_DIR/$file" ;; esac
    mkdir -p "$(dirname "$file")"
    echo "time,container,cpu_percent,mem_usage" > "$file"
    end=$((SECONDS + seconds))
    while [ $SECONDS -lt $end ]; do
      docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' \
        $("${COMPOSE[@]}" ps -q) | sed "s/^/$(date +%H:%M:%S),/" >> "$file"
      sleep 1
    done
    ;;
  down)
    "${COMPOSE[@]}" down -v
    ;;
  *)
    sed -n '2,9p' "$0"
    exit 2
    ;;
esac
