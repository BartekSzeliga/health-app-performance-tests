# 2026-11-21: generator lokalnie, limity prod (W-02, W-03, eksperyment F1)

Status: **pomiar lokalny, wstępny**. Kod aplikacji nie był zmieniany. Eksperyment F1 to wyłącznie ustawienie JVM
w `local/docker-compose.yml` tego repo.

## Warunki

- **Stack:** `local/docker-compose.yml`, osobny od deweloperskiego. Backend: obraz `health-app-helti-app:latest`
  (zbudowany 2026-11-19 z gałęzi `dev`), limity `--cpus 0.5`, `--memory 1g` jak task w prod. Baza: klon lokalnej
  `dietdb` (39,5 tys. produktów, 1626 wersji przepisów, 425 przepisów w katalogu generatora).
- **JVM bazowo (jak w prod):** 1 efektywny CPU, heap max 256 MB, SerialGC.
- **Obciążenie:** `GeneratorCapacitySimulation` w modelie zamkniętym. Krok rozgrzewkowy, potem 1, 2, 4 i 8 generacji
  naraz, po 60 s. Generacja to podgląd tygodnia (`POST /api/diet/generate/preview`, 7 dni). Każde z 300 kont ma inny
  profil: 1500–3000 kcal, 3–6 posiłków, część kont z alergiami.
- **Sondy przez cały test (po 2 żądania/s):** `/public/health` (tylko event loop) i `GET /api/diet-day` (jeden
  odczyt z bazy).
- **Zastrzeżenie:** procesor Maca jest szybszy od vCPU Fargate, więc czasy bezwzględne są optymistyczne
  (szacunkowo 2–3× za nisko). Wiarygodne są trendy. Każdy wariant przeszedł jeden przebieg na laptopie, na którym
  działa też stack deweloperski.

## Wyniki: stan obecny (heap 25% = 256 MB)

| Generacji naraz | Generacji/s | Generator p50 / p95 / p99 (ms) | `/public/health` p50 / p95 | `diet-day` p50 / p95 | CPU backendu | GC (czas, pełne GC) |
|---|---|---|---|---|---|---|
| 1 | ~4,9 | 118 / 578 / 1205 | 12 / 192 | 78 / 392 | 50% (limit) | 3,2%, 0 |
| 2 | ~7,9 | 218 / 455 / 660 | 12 / 98 | 15 / 100 | 50% (limit) | 4,7%, 0 |
| 4 | ~7,8 | 490 / 814 / 1384 | 33 / 197 | 84 / 200 | 50% (limit) | 6,6%, 1 |
| 8 | ~8,1 | 919 / 1291 / 1807 | 107 / 213 | 107 / 249 | 50% (limit) | 9,7%, 5 (~230 ms każde) |

Wnioski:

1. **Jedna generacja w toku zużywa cały przydział CPU taska** (0,5 vCPU). Od 2 generacji naraz przepustowość stoi
   w miejscu (ok. 8/s lokalnie), a czas rośnie liniowo z liczbą jednoczesnych generacji (H1 i H3 potwierdzone).
2. **Hałaśliwy sąsiad:** przy 8 generacjach naraz mediana najlżejszego endpointu rośnie z 12 do 107 ms (ok. 9×),
   a odczytu z bazy z 15 do 107 ms. Generator spowalnia całe API, w tym klientów dietetyka.
3. **GC (H2 potwierdzone):** przy 256 MB heapu i 8 generacjach naraz GC zajmuje ok. 10% czasu. Do tego dochodzi
   5 pełnych odśmiecań na minutę po ok. 230 ms. Przy SerialGC każde zatrzymuje całą aplikację.
4. **Zimny start:** pierwsze generacje po starcie JVM trwają do 4,5–6,4 s, bo JIT nie jest jeszcze rozgrzany.
   Dotyczy to każdego deployu i każdego nowego taska z autoskalowania.
5. **Limiter działa per użytkownik:** w pierwszym przebiegu (100 kont) część żądań dostała 429, bo pojedyncze konta
   przekroczyły 1 generację na 15 s.
6. **Pamięć:** RSS backendu sięga ok. 660 MiB przy heapie 256 MB. Poza heapem zostaje więc ok. 400 MB (metaspace,
   wątki, bufory Netty).

## Eksperyment F1 (tylko konfiguracja testowa, kod bez zmian)

| Wariant | Wynik |
|---|---|
| `-XX:MaxRAMPercentage=70` (heap ~718 MB) | ❌ **Kontener zabity przez limit pamięci** (`OOMKilled=true`, kod 137) w kroku 4 generacji naraz, przy RSS 927 MiB z 1024. Na Fargate oznaczałoby to restart jedynego taska i kilka minut przestoju |
| `-XX:MaxRAMPercentage=50` (heap ~512 MB) | ✅ stabilnie. Przy 8 generacjach naraz: 0 pełnych GC (było 5), przepustowość +18% (576 zamiast 488 generacji/min), generator p50 798 ms (było 919), p95 1111 ms (było 1291) |

Wniosek dla planu napraw: zmiana heapu pomaga, ale umiarkowanie, a **ślepe 70% przy 1 GB położy produkcję**.
Bezpieczne opcje to ok. 50% przy 1 GB albo task 2 GB z 60–65%. Główny problem (cały CPU zajęty przez jedną generację)
zostaje bez względu na heap.

## Co to znaczy dla 40 dietetyków (wstępnie)

Przepustowość generatora nie jest problemem. Nawet jeśli Fargate jest 3× wolniejszy, wychodzi ok. 2,5 generacji/s,
czyli ok. 9 tys. na godzinę. 40 dietetyków robiących po kilka–kilkanaście szkiców dziennie to kilkaset generacji na
dobę. Problemem jest to, co dzieje się w chwili, gdy kilku dietetyków generuje naraz: wszyscy użytkownicy dostają
wtedy wolniejsze odpowiedzi, a po deployu dochodzi zimny start. Pełna odpowiedź wymaga testu jednostki dietetyka
(W-14/W-15) na AWS dev. Generowanie szkiców trenera (`generate-draft-v2`), plany par i 14 dni mogą być cięższe od
podglądu tygodnia.

## Pozycje do planu napraw (wdrożenie tylko po zgodzie Bartosza)

| # | Problem (dowód) | Propozycja | Koszt |
|---|---|---|---|
| N1 | Heap 256 MB, pełne GC pod obciążeniem (tabela, wniosek 3) | `JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=50` w task definition (Terraform) | 0 zł |
| N2 | 70% heapu przy 1 GB = OOM kill (eksperyment F1) | nie ustawiać więcej niż ok. 50% przy 1 GB; przy większym heapie task 2 GB | 0 zł / +ok. 20 zł/mies. |
| N3 | Jedna generacja zajmuje cały CPU, generator spowalnia całe API (wnioski 1–2) | osobna, ograniczona pula wątków generatora z kolejką i szybkim 429/503 z `Retry-After` (F2); na dłużej generacja asynchroniczna (F4) | 0 zł (praca w kodzie) |
| N4 | Zimny start po deployu (wniosek 4) | rozgrzewka generatora przed wpuszczeniem ruchu (health check po rozgrzaniu) | 0 zł |

## Po wdrożeniu N1 + N3 (obraz `diecik-perf/backend:n3-20261121`, kod na `dev`, niezacommitowany)

Zgoda Bartosza na N1, N3 i N4 przyszła 2026-11-21; N4 odpuścił w trakcie. W kodzie:

- **N1:** heap 50% w `Dockerfile`.
- **N3:** osobna pula wątków generatora (`GeneratorScheduler`) oraz bramka `DietGeneratorGateFilter`: 2 generacje
  naraz, kolejka 16, maksymalnie 20 s czekania, potem 429 z `Retry-After: 10`. Limiter i filtr DPA działają przed
  bramką.

Scenariusz identyczny jak w pomiarze bazowym. Wykonane dwa przebiegi.

| Generacji naraz | Generacji/min przed → po | Generator p50 przed → po (ms) | `/public/health` p50, przyrost względem c=1 (przed → po) | `diet-day` p50, przyrost względem c=1 (przed → po) |
|---|---|---|---|---|
| 2 | 476 → 372–390 | 218 → 290–295 | 12 → 7–43 ms, ×1 → ×1 | 15 → 10–46 ms, ×0,2 → ×1 |
| 4 | 467 → 336–351 | 490 → 600–606 | 33 → 8–43 ms, **×2,8 → ×1** | 84 → 11–46 ms, ×1,1 → ×1 |
| 8 | 488 → 393–416 | 919 → 1093–1189 | 107 → 8–43 ms, **×9 → ×1** | 107 → 8–42 ms, **×1,4 → ×1** |

Bezwzględny poziom sond różnił się między dwoma przebiegami „po” (8 albo 43 ms) z powodu szumu laptopa. Wiarygodne
jest porównanie w obrębie jednego przebiegu: c=8 względem c=1. Generator też zwolnił wyraźnie także przy c=1, ale przy
c=1 bramka nic nie zmienia, więc to również szum hosta.

**Pamięć (szczytowy RSS przy limicie 1 GB):**

| Wariant | RSS | Udział w limicie |
|---|---|---|
| przed (heap 25%) | 660 MiB | 65% |
| heap 50% bez bramki | 911 MiB | 89% |
| N1 + N3 (bramka 2) | 598 MiB | 58% |
| eksperyment: bramka 4 | 913 MiB | 89% |

**N1 jest bezpieczne tylko razem z N3.** Wartość 2 zostaje: przy 4 generator był wolniejszy, a pamięć wracała do 89%.

**Przeciążenie: 24 generacje naraz przez 60 s, klienci ponawiają od razu i ignorują `Retry-After`:**

| | Przed | Po |
|---|---|---|
| udane generacje | 241, każda czeka ok. 6,5 s | 96; nadmiar od razu dostaje 429 (2604 z bramki, 1668 z limitera per użytkownik) |
| `/public/health` p50 / p95 | **937 / 1857 ms** | 66 / 168 ms |
| `diet-day` p50 / p95 | **968 / 1865 ms** | 160 / 262 ms |
| RSS | 481 MiB | 540 MiB |

Bez zmian przeciążenie generatora spowalnia całą aplikację, czyli każde kliknięcie klienta, do ok. 1–2 s. Po zmianach
reszta API zostaje szybka. Mało udanych generacji „po” wynika z testu: wirtualni klienci ponawiali od razu,
a ok. 70 odrzuceń na sekundę zjadało CPU. Zachowanie aplikacji, które odczekują `Retry-After`, jest teraz
w symulacji (`-Dperf.respectRetryAfter=true`, domyślnie). Lokalny przebieg z tym ustawieniem nie dał wiarygodnego
wyniku, bo laptop dwukrotnie zasnął w trakcie. **Do powtórzenia na AWS dev.**

**Koszt zmian:** przy 2–8 jednoczesnych generacjach generator ma o ok. 15–20% mniejszą przepustowość, a jego mediana
jest o ok. 20–30% wyższa. Zysk: generator przestał spowalniać resztę aplikacji, pamięć jest ograniczona, a nadmiar
dostaje szybkie 429 zamiast czekać ok. 6,5 s. Dla 40 dietetyków (kilkaset generacji dziennie) ta przepustowość jest
z dużym zapasem wystarczająca.

**Nie objęte bramką:** generator treningów (`training-snapshot/generate*`, `/api/training-generator/preview`,
`/api/training-plans/generate`). Nie był mierzony; do sprawdzenia w kolejnym kroku.

## Odtworzenie

```bash
./local/perf-stack.sh clone && ./local/perf-stack.sh up
mvn exec:java -Dexec.mainClass=diecik.perf.seed.SeedMain -Dexec.args="setup-realm"
mvn exec:java -Dexec.mainClass=diecik.perf.seed.SeedMain -Dexec.args="individuals 300"
mvn gatling:test -Dgatling.simulationClass=diecik.perf.simulations.GeneratorCapacitySimulation \
    -Dperf.steps=1,1,2,4,8 -Dperf.stepSeconds=60
python3 scripts/gatling_stats.py results/gatling/<folder>
# eksperyment F1 (nakładka, kod aplikacji bez zmian):
PERF_JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=50" \
  docker compose -f local/docker-compose.yml -f local/jvm-experiment.yml up -d backend
```

Surowe dane (raporty Gatling, `docker stats`, `jstat`) leżą w `results/` (poza gitem):

- `results/local/w02-baseline`: pierwszy przebieg (stałe wejście, 100 kont, sonda z błędem nagłówka);
- `results/local/w02-varied`;
- `results/local/w02-f1-heap70`;
- `results/local/w02-f1-heap50`.
