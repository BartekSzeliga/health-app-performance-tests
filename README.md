# health-app-performance-tests

Load tests (Gatling) and data isolation tests (JUnit) for Diecik. Plan: [PLAN.md](PLAN.md). Results: [reports/](reports/).

Safety rules built into the toolkit:

- **Production is refused.** `ProdGuard` blocks `diecik.com`, `api.diecik.com`, `auth.diecik.com` and the prod ALB.
  Load and isolation runs are blocked even with `-Dperf.allowProdSmoke=true`.
- **Test accounts only.** Every account uses the reserved domain `@perf.diecik.invalid`, so no e-mail is ever
  delivered. The password grant is enabled only in the test realm (local clone or AWS dev).
- **Experiments do not change app code.** JVM variants go through the `local/jvm-experiment.yml` overlay:

  ```bash
  PERF_JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=50" \
    docker compose -f local/docker-compose.yml -f local/jvm-experiment.yml up -d backend
  ```

  Without the overlay, the backend uses the JVM settings of its image, as in ECS.
- **Testing a backend build.** Set `PERF_BACKEND_IMAGE=<image>` before `./local/perf-stack.sh up`. The default is
  the developer image `health-app-helti-app:latest`.

## Requirements

- JDK 25 and Maven 3.9.
- Docker, for the local stack.
- The local developer stack running (`postgres-dev`). It is only read, to clone the catalog and the realm.

## Local stack with production limits

```bash
./local/perf-stack.sh clone     # read-only pg_dump of dietdb + keycloakdb from postgres-dev into results/local/db
./local/perf-stack.sh up        # ports 15432 / 18080 / 18081, backend limited to 0.5 vCPU / 1 GB like prod
./local/perf-stack.sh stats 300 results/local/<run>/docker-stats.csv   # sample CPU / memory while a test runs
./local/perf-stack.sh down      # removes only the diecik-perf containers, network and volume
```

## Seeding

```bash
mvn exec:java -Dexec.mainClass=diecik.perf.seed.SeedMain -Dexec.args="setup-realm"
mvn exec:java -Dexec.mainClass=diecik.perf.seed.SeedMain -Dexec.args="individuals 300"
```

Seeded accounts and their passwords are written to `results/<env>/users.json`. The `results/` folder is ignored by git.

## Running load tests

```bash
mvn gatling:test -Dgatling.simulationClass=diecik.perf.simulations.GeneratorCapacitySimulation \
    -Dperf.steps=1,1,2,4,8 -Dperf.stepSeconds=60 [-Dperf.inputs=fixed] [-Dperf.days=7]
python3 scripts/gatling_stats.py results/gatling/<run-folder>   # per-request table (requests are named per step)
```

## Selecting the environment

The default environment is `local`. For AWS dev:

```bash
-Dperf.env=dev -Dperf.clientId=<web client id> \
PERF_ADMIN_USER=... PERF_ADMIN_PASSWORD=...
```

Any setting can be overridden with `-Dperf.<key>` or `PERF_<KEY>`. See `TestEnvironment`.

## Toolkit unit tests

```bash
mvn test        # ProdGuard and other toolkit tests, no stack needed
```
