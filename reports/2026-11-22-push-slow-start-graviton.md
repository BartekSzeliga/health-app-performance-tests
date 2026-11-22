# Push, slow start i Graviton na AWS dev (2026-11-22)

Sprawdzenie optymalizacji z raportu `2026-11-21-aws-dev-pojemnosc.md`:
- **push:** zdarzenia na żywo (`GET /api/events`) zamiast odpytywania czatu;
- **stopniowe wpuszczanie ruchu na nowy task** (`slow_start` 90 s) i wygaszanie połączeń 60 s;
- **backend 0,5 vCPU / 2 GB** zamiast 1 GB;
- **Graviton (ARM64)** zamiast x86.

## Warunki

- **Środowisko:** dev odbudowany w rozmiarach docelowych:
  - backend 2× 0,5 vCPU / 2 GB, Keycloak 2× 0,5 vCPU / 1 GB, liczba tasków stała;
  - RDS `db.t4g.small`, ALB bez sticky sessions, WAF wyłączony.
- **Kod:** `dev` z commitem `34133520` i niezacommitowanymi zmianami pushu w backendzie. Obrazy x86 i arm64 z tego
  samego kodu.
- **Dane:** jak 2026-11-21: 20 dietetyków i 600 klientów z planami, 100 kont indywidualnych, katalog bez danych
  użytkowników.
- **Ruch:** `DietitianUnitsSimulation` z `-Dperf.chatPolling=false`, czyli bez odpytywania czatu, bo robi to push.
  Równolegle `LiveEventsCheck`: 320 otwartych strumieni (300 rozmów), dietetycy wysyłają 300 wiadomości na minutę.
  Narzędzie mierzy czas od wysłania do zdarzenia na strumieniu klienta.

## Push end-to-end

| Strumienie | Wiadomości | Dostarczone | Zgubione | p50 | p95 | p99 | Maks. |
|---|---|---|---|---|---|---|---|
| 120 | 360 | 360 | 0 | 63 ms | 134 ms | 356 ms | 434 ms |
| 320 | 1 200 | 1 200 | 0 | 63 ms | 135 ms | 224 ms | 408 ms |
| 320, przy 200–400 jednostkach ruchu | 2 400 | 2 400 | 0 | 63 ms | 187 ms | 351 ms | 560 ms |
| 320, z padnięciem taska backendu | 3 300 | 3 300 | 0 | 57 ms | 260 ms | 494 ms | 3,96 s |

- **Czas:** jest liczony od początku wysłania wiadomości z laptopa, więc obejmuje też drogę Polska–Frankfurt.
  Strumienie rozkładają się na oba taski, więc większość zdarzeń przechodzi między taskami przez Postgres.
- **Ponowne łączenie:** strumienie kończą się ok. 5 s przed wygaśnięciem tokenu (co ok. 5 minut), a klient łączy się
  od razu ponownie. W tym czasie nie zginęło żadne zdarzenie.
- **Padnięcie taska:** zerwało 77 strumieni. Klienci połączyli się ponownie, a najdłuższe opóźnienie wyniosło 4 s.

**Uwaga do narzędzia:** pierwsza wersja `LiveEventsCheck` łączyła wszystkie strumienie jednym połączeniem HTTP/2, a ALB
przyjmuje do 128 strumieni na połączenie. Przy 320 strumieniach połowa była odrzucana. Narzędzie używa teraz HTTP/1.1
(osobne połączenie na strumień, jak karta przeglądarki albo telefon). Ten nieważny przebieg nie jest liczony.
Przeglądarka i aplikacja otwierają jeden strumień na kartę lub urządzenie, więc tego limitu nie dotyczy.

## Pojemność z pushem (x86, backend 0,5 vCPU / 2 GB)

| Jednostki | Żądań/s | Błędy | p95 ALB | Backend CPU: średnio / najbardziej obciążony task | Pamięć | RDS CPU |
|---|---|---|---|---|---|---|
| 50 | 9 | 0 | 87 ms | 18% / 29% | 37% | 5% |
| 100 | 14 | 0 | 113 ms | 26% / 46% | 37% | 6% |
| 200 | 20 | 0 | 98–101 ms | 22–24% / 31–35% | 38% | 7% |
| 300 | 28 | 0 | 124 ms | 34% / 41% | 37% | 9% |
| 400 | 36 | 1 (429) | 171–172 ms | 35–41% / 53–58% | 37–40% | 8% |

**Dla porównania 2026-11-21 (odpytywanie czatu, 1 GB):** 300 jednostek dawało 53% / 65% CPU i 65 żądań/s. Z pushem 400
jednostek to 41% / 58% CPU i 36 żądań/s. Odpytywanie czatu stanowiło prawie połowę żądań. Przyrost CPU wynosi teraz
ok. 0,085 punktu procentowego na jednostkę zamiast ok. 0,14, czyli ok. 1,6× mniej na dietetyka. 70% średniego CPU
wypada przy ok. 700 jednostkach (ekstrapolacja) zamiast ok. 430.

## Padnięcie taska ze stopniowym wpuszczaniem ruchu (x86, 200 jednostek)

| | 2026-11-21: bez `slow_start`, 1 GB, odpytywanie | 2026-11-22: `slow_start` 90 s, 2 GB, push |
|---|---|---|
| Najgorsza minuta p95 | 31 s | 0,52 s |
| Żądania powyżej 1,2 s | 15,9% | 0,53% |
| Błędy | 55 (0,21%): 500 z puli R2DBC, 429, timeouty | 0 |
| Zastępczy task zdrowy po | ok. 3 min | 3 min |

To łączny efekt trzech zmian. Nowy, zimny task dostaje ruch stopniowo przez 90 s i nie dusi się pulą połączeń,
2 GB daje większy heap, a mniej żądań (push) zmniejsza obciążenie.

## Generator (x86, 0,5 vCPU / 2 GB, rozgrzana JVM)

| Generacji naraz | Generacji/min | p50 | p95 |
|---|---|---|---|
| 2 | 184 | 0,56 s | 1,4 s |
| 4 | 253 | 0,86 s | 2,0 s |
| 8 | 285 | 1,5 s | 3,6 s |
| 16 | 285 | 4,3 s | 6,4 s |

Maksimum ok. 285 na minutę, jak 2026-11-21 (ok. 280 przy 1 GB).

## Graviton (ARM64) wobec x86 przy tym samym rozmiarze (0,5 vCPU)

Ten sam kod i ta sama sekwencja: rozgrzewka, 200 i 400 jednostek z ruchem pushu w tle, dwa przebiegi generatora.

| | x86 | ARM64 (Graviton) |
|---|---|---|
| Generator, maks. 7-dniowych/min | ok. 285 | ok. 560 (~2×) |
| Generator, p50 przy 2 naraz | 0,56 s | 0,37 s |
| 400 jednostek: CPU średnio / najbardziej obciążony task | 41% / 58% | 35% / 45% |
| 400 jednostek: p95 ALB | 0,17 s | 0,17 s |
| Push przy 200–400 jednostkach: dostarczone, p95 | 2400/2400, 187 ms | 2397/2400, 145 ms |
| Start backendu (`Started … in`) | ok. 73 s | 32–46 s |
| Zastępczy task w ALB po padnięciu (200 jednostek) | ok. 3 min | 28 s od startu |
| Padnięcie taska: błędy, najgorsza minuta p95 | 0, 0,52 s | 0, 0,11 s |
| Cena za task 0,5 vCPU / 2 GB (Frankfurt) | 24,5 USD/mies. | 19,6 USD/mies. (−20%) |

- **Dlaczego ARM liczy szybciej:** na Fargate x86 jeden vCPU to jeden wątek hyperthreadingu, czyli pół fizycznego
  rdzenia. Na Gravitonie jeden vCPU to cały rdzeń. Generator obciąża jeden wątek do pełna, więc różnica jest największa
  właśnie tam.
- **3 brakujące zdarzenia na ARM** padły w ok. 1-sekundowej przerwie, gdy strumień łączy się ponownie po wygaśnięciu
  tokenu. Zdarzeń z tej przerwy serwer nie powtarza z założenia. Panel i aplikacja przeładowują czat po każdym
  połączeniu („ready”), więc nic nie tracą. Narzędzie testowe tego nie robi i liczy je jako brakujące.
- **Generator na ARM: 2 odpowiedzi 422** `DIET_TARGET_UNMET` dla skrajnych profili. To odpowiedź biznesowa, nie
  awaria. Przy 2× większej liczbie generacji trafiały się częściej.
- **Uczciwość porównania:** test padnięcia taska na ARM szedł bez ruchu pushu w tle, więc z mniejszym obciążeniem niż
  na x86. Pewne są czasy startu i rejestracji w ALB. Wynik p95 jest tu tylko orientacyjny.

## Wnioski

1. **Push działa** przez ALB i między taskami: przy 320 strumieniach 0 zgubionych zdarzeń, p95 135–260 ms, także przy
   padnięciu taska. Odpytywanie czatu stanowiło prawie połowę żądań, a push zmniejsza CPU na dietetyka ok. 1,6×.
2. **`slow_start` z 2 GB i pushem usuwa problem zimnego startu:** padnięcie taska przy 200 dietetykach daje 0 błędów
   zamiast 55, a najgorsza minuta p95 to 0,5 s zamiast 31 s.
3. **Graviton** przy tej samej cenie minus 20% daje ok. 2× wydajność generatora, 15–20% mniej CPU dla zwykłego ruchu
   i ok. 2× szybszy start, więc i szybsze zastąpienie taska.
4. **Pojemność 2× 0,5 vCPU / 2 GB na ARM z pushem** (ekstrapolacja z 400 jednostek przy 35% CPU): 70% średniego CPU
   wypada przy ponad 800 jednostkach. Bezpiecznie, z zapasem na padnięcie taska: ok. 400 dietetyków według tego modelu
   ruchu. Wczoraj na x86 z odpytywaniem było to ok. 100. Przed przekroczeniem ok. 600 trzeba obserwować RDS.

## Koszt po zmianach (netto, Frankfurt, z publicznym IPv4 tasków)

| Wariant | Taski backend + Keycloak | Cała infrastruktura |
|---|---|---|
| x86: backend 2× 0,5 vCPU / 2 GB, Keycloak 2× 0,5 vCPU / 1 GB | 105 USD (425 zł) | ok. 177 USD (717 zł) |
| ARM64: to samo | 87 USD (353 zł) | ok. 159 USD (645 zł) |

## Odtworzenie

- Push end-to-end: `LiveEventsCheck <rozmowy> <minuty> <wiadomości/min>` (`-Dperf.env=dev`).
- Pojemność z pushem: `DietitianUnitsSimulation -Dperf.chatPolling=false -Dperf.units=200,400`, równolegle `LiveEventsCheck 300 8 300`.
- ARM w dev: `task_cpu_architecture = "ARM64"` i obrazy arm64 (`perf-live-arm64`).
