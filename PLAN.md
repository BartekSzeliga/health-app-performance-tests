# Diecik: plan testów wydajności i izolacji danych

Data: 2026-11-21. Status: **plan do akceptacji**. Nic nie jest jeszcze zbudowane ani uruchomione w AWS.
Wszystkie liczby w sekcji 1 pochodzą z dzisiejszych odczytów (kod, Terraform, CloudWatch, Cost Explorer, Pricing API).

## 0. W skrócie

1. **Testujemy na odbudowanym środowisku dev, nie na produkcji** (decyzja z 2026-11-21). Dev jest dziś całkowicie
   zburzony: stan Terraform jest pusty od 2026-08-27. „Podniesienie” oznacza więc pełną odbudowę z `envs/dev`
   i `envs/dev-edge` według `docs/dev-rebuild-from-scratch-step-by-step.md`. Do pomiaru bazowego ustawiamy rozmiary
   prod (backend 0,5 vCPU / 1 GB, Keycloak 0,25 vCPU / 1 GB, RDS db.t4g.small zamiast t3.micro), a potem skalujemy
   przez tfvars. Używamy syntetycznych użytkowników i katalogu skopiowanego z prod. W czasie testów kosztuje to
   ok. 0,38 USD/h razem z maszyną generującą ruch, cała kampania ok. 15–40 USD.
2. **Narzędzie:** do obciążenia Gatling (Java DSL, Maven), do testów izolacji danych JUnit 5 z wirtualnymi
   wątkami. Oba w tym jednym projekcie Maven. JMeter odpada, k6 to rozsądna alternatywa.
3. **Blokada:** niczego nie zdejmujemy i nie dodajemy obejść w prod. WAF na produkcji jest wyłączony
   (`enable_waf = false`). Limiter w aplikacji liczy per użytkownik (JWT `sub`), więc potrzebujemy wielu kont
   testowych, a nie białej listy IP. Jeśli w `dev` włączymy WAF, dodajemy regułę allow tylko dla Elastic IP
   generatora ruchu i tylko w `dev`.
4. **Generator:** już analiza wskazuje konkretną przyczynę, a nie samo „jest wolny”. W kontenerze prod JVM widzi
   1 CPU, ma 256 MB heapu i SerialGC. Generator liczy na `Schedulers.parallel()`, który przy 1 CPU ma 1 wątek, więc
   generacje czekają jedna za drugą. Pauzy GC i limit CPU spowalniają przy tym resztę API. To hipotezy H1–H6,
   które potwierdzamy pierwszym testem lokalnie (ok. godziny pracy).
5. **Izolacja danych** ma trzy warstwy:
   - „kanarki”: unikalne znaczniki w danych każdego użytkownika, sprawdzane w każdej odpowiedzi pod obciążeniem;
   - scenariusze wyścigów;
   - macierz autoryzacji A→B (IDOR) dla ok. 360 tras z ID w ścieżce.

   Najczęstsza przyczyna sytuacji „widzę cudze dane” to brak sprawdzenia właściciela albo cache, rzadziej wyścig.
   Dlatego testujemy wszystkie trzy warstwy.
6. **Koszt miesięczny po skalowaniu (netto):**
   - ok. 105 USD: samo strojenie i ARM;
   - ok. 208 USD: mały wzrost;
   - ok. 348 USD: HA;
   - ok. 787 USD: osobny worker generatora i duża baza.

   Szczegóły są w sekcji 10.
7. **Pojemność liczymy w dietetykach.** Jednostka testowa to 1 dietetyk i 60 klientów (sekcja 7.1). Wynik testu
   brzmi: „wariant X mieści N dietetyków”. Do tego dochodzi próg, przy którym trzeba skalować, i koszt infrastruktury
   na dietetyka przy abonamencie 100 zł albo 600 zł z własną aplikacją (sekcja 10.3). Na początku infrastruktura to
   koszt stały ok. 426 zł/mies. Pokrywa go 6 dietetyków na 100 zł albo jeden z własną aplikacją.
8. **Cel na 12 miesięcy: 40 dietetyków** (decyzja z 2026-11-21), czyli ok. 2400 klientów. Kryterium sukcesu:
   tani wariant (T1, ewentualnie z większym taskiem) mieści **co najmniej 57 jednostek**. Przy progu 70% wystarczy
   to na 40 dietetyków bez skalowania, a infrastruktura zostaje na poziomie ok. 13% przychodu z planów standard.

## 1. Co już wiemy (stan prod na 2026-11-21)

| Element | Stan |
|---|---|
| Backend | ECS Fargate x86: 1 task (autoscaling 1–2 po CPU 80%/30%), 0,5 vCPU / 1 GB, pula R2DBC 12 |
| JVM w tym kontenerze | 1 efektywny CPU, **heap max 256 MB** (domyślne 25% RAM), **SerialGC**. W task definition nie ma `JAVA_TOOL_OPTIONS`. Sprawdzone na obrazie backendu z limitami prod (`--cpus 0.5 -m 1g`) |
| Wątki generatora | `DietDayGeneratorService`, `PairDietDayGeneratorService` i `ExistingDietDayFitPreview` liczą na `Schedulers.parallel()` (rozmiar = liczba CPU, czyli 1) |
| Keycloak 26.7.2 | 1 task 0,25 vCPU / 1 GB (autoscaling 1–2), pula DB 10, port klastra 7800 otwarty |
| RDS | PostgreSQL db.t4g.small, Single-AZ, 20 GB gp3, Performance Insights wyłączone |
| ALB | idle timeout 60 s, bez sticky sessions |
| WAF | zdefiniowany (3000 żądań / 5 min / IP dla całości i API, 1000 dla auth), **wyłączony** |
| Limiter aplikacji | `ApiRateLimitFilter`: token bucket per użytkownik (JWT `sub`) dla kilkudziesięciu tras. Liczniki trzyma w pamięci taska. Generowanie diety: 6 naraz, potem 1 na 15 s |
| Monitoring | Container Insights wyłączone, brak actuatora i metryk aplikacji. Mamy tylko metryki ALB oraz CPU/RAM ECS co minutę |
| Ruch (14 dni) | 1,32 mln żądań. Szczytowa godzina ok. 6,3 tys. (≈1,8 req/s), szczytowa minuta z ostatnich 23 h: 365 (≈6 req/s). p99 API (maks. godzinowe) 1,2 s |
| CPU backendu | średnio 0,6%. 100% tylko przy starcie JVM po deployu (2026-11-20 11:22 i 19:38); start trwa ok. 2 min przy 0,5 vCPU |
| RAM backendu | średnio 56%, maks. 71% |
| RDS | CPU średnio 4%, maks. 57%; maks. 27 połączeń |
| Koszt za październik 2026 | **139,59 USD** (113,49 netto + 26,10 VAT). ECS 33,6, RDS 30,9, ALB 20,3, publiczne IPv4 14,9, Route 53 8,0 |
| Środowisko dev | istnieje tylko jako kod Terraform (`envs/dev`, `envs/dev-edge`), w AWS nie działa |

Wniosek: dziś ruch jest mały, a lekkie API ma duży zapas. Ryzyko leży w trzech miejscach:

- generator dzieli jeden proces, 1 CPU i 256 MB heapu z resztą API;
- Keycloak ma 0,25 vCPU, co może nie wytrzymać fali logowań;
- nie ma HA: działa 1 task, a baza stoi w 1 AZ.

## 2. Cele i proponowane SLO (do akceptacji)

| Obszar | Próg |
|---|---|
| Odczyty (dzień diety, posiłki, produkty, postęp) | p95 < 300 ms, p99 < 800 ms |
| Zapisy (posiłek, waga, woda) | p95 < 500 ms |
| Generator: 7 dni, 1 osoba | p95 < 8 s |
| Generator: para, 14 dni, szkic trenera | p95 < 20 s; nic powyżej 55 s (ALB ucina po 60 s) |
| Logowanie (pełny flow Keycloak) | p95 < 1,5 s |
| Błędy (5xx i timeouty) przy obciążeniu docelowym | < 0,5% |
| Wpływ generatora na lekkie endpointy | p95 lekkich endpointów rośnie najwyżej 2× |
| Cudze dane w odpowiedzi | **0**, warunek twardy: każdy przypadek to błąd krytyczny i przerwanie kampanii |

Wynik podajemy jako pojemność w jednostkach dietetyka (sekcja 7.1): „wariant X mieści N dietetyków z 60 klientami
każdy w SLO, a pierwsze pęka Y”.

## 3. Gdzie testujemy

| Środowisko | Do czego | Uwagi |
|---|---|---|
| **L**: lokalnie, `docker compose` + `docker-compose.benchmark.yml` (0,5 CPU, 1 GB, jak prod) | szybkie pętle: hipotezy generatora i JVM, porównania A/B poprawek, wyścigi, IDOR | bez kosztów; nie mierzy sieci, ALB ani RDS |
| **D**: dev w AWS (eu-central-1), odbudowany z Terraform | właściwe pomiary, Keycloak, skalowanie, awarie, porównanie A/B poprawek z gałęzi `dev` | na pomiar bazowy rozmiary prod, potem T2/T3 przez tfvars; między sesjami `dev-edge` wyłączony; stan po testach według decyzji 7 |
| **Prod** | nie obciążamy | tylko pasywnie: porównanie metryk; ewentualnie jeden nocny smoke (kilka req/s, konta testowe) po Twojej akceptacji |

Dlaczego nie prod:

- prawdziwe dane zdrowotne (RODO art. 9);
- maile z Keycloaka;
- Przelewy24 i joby płatności cyklicznych;
- powiadomienia push;
- integracje Garmin i Polar;
- płatne wywołania OpenAI;
- zaśmiecone statystyki;
- ryzyko przestoju dla realnych użytkowników;
- sprzątanie danych testowych.

Dlaczego dev, a nie osobne środowisko:

- istnieją już Terraform, przewodnik odbudowy, skrypty on/off, domeny `api.dev.diecik.com` i `auth.dev.diecik.com`
  oraz pipeline `deploy-dev-backend.yml`;
- poprawki F1 i F2 idą przez gałąź `dev`, więc trafiają od razu do środowiska, które mierzymy;
- koszt w AWS jest taki sam jak dla nowego środowiska.

Warunki, bez których wyniki z dev nie będą porównywalne z prod:

- **Parzystość z prod.** `dev-edge` ma 49 zmiennych kontenerów mniej niż `prod-edge`, m.in. pule DB Keycloaka, mail,
  restore credentials, sklepy mobilne i SPI Keycloaka. Zmiana kodu Terraform wymaga zgody Bartosza. Najpierw
  sprawdzamy, czy dev wstanie na samych tfvars. Jeśli różnice wpływają na pomiar (np. pula Keycloaka), przedstawiamy
  listę zmian do akceptacji.
- **Rozmiary jak w prod na pomiar bazowy:**
  - RDS db.t4g.small zamiast domyślnego db.t3.micro (t3.micro ma 1 GB RAM i kredyty CPU, które pod stałym
    obciążeniem się wyczerpują);
  - backend 512 CPU / 1024 MB, Keycloak 256 / 1024;
  - te same pule połączeń i progi autoskalowania.
- **Ten sam obraz co prod.** Pomiar bazowy robimy na obrazach backendu i Keycloaka z prod (ten sam digest), a nie na
  HEAD gałęzi `dev`. Obraz z `dev` z poprawkami służy dopiero do porównania A/B.
- **Zamrożone pushe na `dev` w czasie sesji pomiarowej.** Push na `dev` uruchamia `deploy-dev-backend.yml`, który
  zrobiłby deploy w środku testu.
- **Lokalne tfvars dla dev.** Dziś są tylko pliki przykładowe. Trzeba utworzyć `terraform.tfvars` dla `envs/dev`
  i `envs/dev-edge` (sekrety dev, poza gitem), a po odbudowie zaktualizować zmienne GitHub (`DEV_ECS_CLUSTER_NAME`
  itd.).

W `dev` wyłączamy albo zastępujemy stubem (WireMock z realistycznym opóźnieniem):

- maile (`APP_MAIL_ENABLED=false`, Keycloak bez SMTP);
- płatności (P24 sandbox lub wyłączone, sklepy mobilne wyłączone);
- OpenAI i Bedrock;
- Garmin, Polar i Google Meet;
- OpenFoodFacts i FoodDataCentral.

## 4. Blokady i jak je obsłużyć

Co może blokować ruch testowy:

1. **AWS WAF.** W prod jest wyłączony. W `dev` testujemy dwa warianty: wyłączony (jak prod) i włączony. Ten drugi
   sprawdza, czy WAF nie blokuje legalnego ruchu, np. całej siłowni za jednym NAT-em (sekcja 11, punkt 8).
2. **`ApiRateLimitFilter`, per użytkownik.** Zostaje włączony, bo jest częścią systemu. Każdy wirtualny użytkownik
   ma własne konto. Test W-10 sprawdza osobno 429 z `Retry-After` i zachowanie przy 2 taskach.
3. **Publiczne limitery per IP** (demo generatora, ankieta, prośba o prezentację). Zostają; sprawdzamy ich poprawność.
4. **Keycloak.** W szablonie realmu nie widać brute-force protection, więc testy nie zablokują kont. To jednocześnie
   uwaga bezpieczeństwa do sprawdzenia w realmie prod.
5. **Alarmy CloudWatch** (skok ruchu, CPU). W `dev` zostają włączone, bo testujemy też alerty.

| Opcja | Plusy | Minusy | Werdykt |
|---|---|---|---|
| A. Wielu użytkowników testowych, limity bez zmian | realistyczne; limity też są testowane | trzeba wygenerować konta | ✅ domyślnie |
| B. IP generatora ruchu na allowliście WAF (IP set + reguła allow przed regułami limitów) | proste, w Terraform | dotyczy tylko WAF; w prod otwierałoby furtkę | ✅ tylko `dev`, gdy WAF włączony |
| C. Wyłączenie limitów flagą | pomiar surowej mocy generatora | mierzymy inny system niż prod | tylko lokalnie, do testu surowej przepustowości |
| D. Sekretny nagłówek omijający limity | brak | furtka bezpieczeństwa | ❌ |
| E. Generator ruchu w tym samym regionie (EC2 c7g.xlarge z Elastic IP) | domowe łącze nie ogranicza wyników, stałe IP | 0,165 USD/h | ✅ do pomiarów; laptop tylko do smoke |

## 5. Narzędzie

| | Gatling (Java) | k6 (JS) | JMeter | Locust (Python) |
|---|---|---|---|---|
| Język | Java 25 / Maven, jak backend | JavaScript | XML z GUI | Python |
| Kod w git i review | ✅ | ✅ | ❌ (XML) | ✅ |
| Wydajność generatora ruchu | async (Netty), tysiące req/s z jednej maszyny | bardzo dobra | wątek na użytkownika, ciężki | słabsza |
| Model otwarty (stały napływ) | ✅ | ✅ | ograniczony | ograniczony |
| Raporty, progi pass/fail | HTML i assertions, kod wyjścia dla CI | progi i Grafana | wtyczki | prosty |
| Werdykt | ✅ **rekomendacja** | dobra alternatywa | ❌ | ❌ |

Dlaczego Gatling:

- ten katalog jest już modułem Java w IntelliJ;
- znasz stos Java i Maven;
- jedna maszyna c7g.xlarge da z dużym zapasem setki req/s, których potrzebujemy;
- model otwarty nie ukrywa kolejki generatora (coordinated omission).

Testy izolacji w JUnit 5 (`java.net.http` + wirtualne wątki), bo potrzebne są precyzyjne asercje na pojedynczy
przypadek i powtarzalność. Narzędzia obciążeniowe słabo raportują, które żądanie zwróciło czyje dane.

Przy zakładaniu projektu sprawdzamy wersję Gatlinga obsługującą JDK 25. Jeśli takiej nie będzie, uruchamiamy
Gatlinga na JDK 21 przez Maven toolchain.

Proponowana struktura:

```text
health-app-performance-tests/
  pom.xml                      # Gatling + JUnit 5, profile: local / dev (bez profilu prod)
  README.md                    # jak uruchomić, jak czytać wyniki
  src/test/java/diecik/perf/
    support/                   # TokenProvider (z odświeżaniem), UserPool, ApiClient, Canary, ProdGuard
    simulations/               # Baseline, GeneratorCapacity, NoisyNeighbour, MixedLoad, Breakpoint, Spike, Soak, LoginStorm
    isolation/                 # CanaryIsolationIT, ConcurrentGenerationIT, PairRaceIT, TrainerRaceIT, AuthorizationMatrixIT, CacheHeadersIT
  src/test/resources/env/      # local.conf, dev.conf
  seed/                        # konta w Keycloak, profile i historia przez API, kopia katalogu
  results/                     # w .gitignore
```

`ProdGuard`: narzędzie odmawia startu na `api.diecik.com` i `auth.diecik.com`, chyba że dostanie jawną flagę, i wtedy
pozwala tylko na tryb smoke.

## 6. Użytkownicy, tokeny, dane

- **Konta:** 500, a potem 2000 syntetycznych użytkowników. Tworzy je Keycloak Admin API w realmie `dev`
  z `emailVerified=true` i domeną `@perf.diecik.invalid`, więc żaden mail nie wyjdzie. Profil (cele, typ diety,
  alergie, liczba posiłków) ustawiamy przez API. Rozkład chcemy mieć jak w prod: z prod bierzemy wyłącznie agregaty,
  za Twoją zgodą.
- **Role:**
  - zwykli użytkownicy i premium;
  - pary;
  - dietetycy z 60 klientami każdy (jednostka testowa z sekcji 7.1), do tego przypadki brzegowe z 5 i 150 klientami;
  - dwie organizacje (A i B) z aplikacji firmowej;
  - admin.
- **Historia:** 60–90 dni dni diety, wag, pomiarów, wody i czatu, żeby tabele miały rozmiar zbliżony do prod.
  Puste tabele dają fałszywie szybkie zapytania. Liczności kluczowych tabel w prod sprawdzamy przez istniejący
  port-forward (tylko `count(*)`, za zgodą).
- **Katalog:** przepisy, produkty, posiłki generyczne, ćwiczenia i tłumaczenia kopiujemy z prod. To własna treść,
  bez danych osobowych. Tabel użytkowników nie kopiujemy.
- **Tokeny:** w realmie `dev` włączamy Direct Access Grants dla tych samych klientów (web, mobile, aplikacja
  firmowa). Dzięki temu `azp` jest taki jak w prod (backend go czyta). W prod nigdy. Tokeny wygasają, więc skrypt
  odświeża je refresh tokenem. Osobny scenariusz W-08 przechodzi pełne logowanie (authorization code + PKCE),
  żeby zmierzyć Keycloaka.

## 7. Scenariusze obciążeniowe

**Model ruchu.** Prod nie ma jeszcze dietetyków z klientami, więc jego ruch nie opisze scenariusza dietetyka.
Logów z prod nie zbieramy (decyzja z 2026-11-21). Model budujemy z założeń sekcji 7.1 i z nagrań prawdziwych sesji
(HAR). ALB access logs włączamy tylko na dev, na czas testów, do analizy czasów per endpoint.

**Skale:** 1× to obecny szczyt (≈6 req/s), dalej 5×, 10× i 50×, a potem szukanie punktu załamania. Progi
biznesowe ustalamy w decyzji 3.

| ID | Test | Co mierzy | Gdzie |
|---|---|---|---|
| W-01 | Smoke i baseline (1–2 użytkowników, 5 min) | poprawność skryptów, czasy bazowe | L, D |
| W-02 | Krzywa pojemności generatora: współbieżność 1/2/4/8; 7 i 14 dni; standard i keto; para; szkic trenera; generator treningu | czas, kolejka, CPU, GC, heap, generacje/min | L (A/B), D |
| W-03 | Hałaśliwy sąsiad: stały lekki ruch + rosnąca liczba generacji | wpływ generatora na p95 lekkich endpointów | L, D |
| W-04 | Load: realistyczna mieszanka 1×/5×/10×, po 20–30 min | SLO przy docelowym ruchu | D |
| W-05 | Stress / breakpoint: model otwarty rośnie aż do złamania SLO | co pęka pierwsze: CPU, heap, pula DB, Keycloak | D |
| W-06 | Spike („push o 7:00”): 0→20× w 30 s, start aplikacji mobilnej z równoległymi żądaniami, logowania | zachowanie bez zapasu, czas do przeskalowania | D |
| W-07 | Soak 4–8 h przy 2–3× | wycieki pamięci, pula połączeń, rozmiar logów | D |
| W-08 | Keycloak: fala logowań i odświeżania tokenów | logowania/s na 0,25 vCPU; spójność sesji przy 2 węzłach | D |
| W-09 | Skalowanie i awarie pod obciążeniem: scale-out 1→2, scale-in, rolling deploy, zabicie taska | błędy w trakcie, czas reakcji autoskalera | D |
| W-10 | Limity: 429 + `Retry-After`, zachowanie przy 2 taskach, demo per IP | poprawność blokad | L, D |
| W-11 | Przerwane żądania: klient zrywa po 5 s, ALB po 60 s, „podwójny klik” | czy porzucone generacje liczą się dalej | L, D |
| W-12 | Baza: `pg_stat_statements` i Performance Insights w trakcie W-04 i W-05 | najcięższe zapytania, brakujące indeksy, wyczerpanie puli | D |
| W-13 | Zmiana katalogu generatora pod obciążeniem (nowa rewizja) | skok pamięci przy przeładowaniu snapshotu w 256 MB heapie | L, D |
| W-14 | Jednostki dietetyka: N = 1, 5, 10, 20, 40, 60, 100…, każdy krok 15–20 min w profilu godziny szczytu | N_max każdego wariantu (T0–T3) i to, co pęka pierwsze | D |
| W-15 | „Poniedziałek 9:00”: N dietetyków generuje szkice planów w tym samym kwadransie | kolejka generatora, timeouty, wpływ na klientów | L, D |
| W-16 | Publikacja i push: dietetyk publikuje plany 60 klientom, a ci otwierają aplikację w ciągu 2 min | skok startów aplikacji przy wielu dietetykach naraz | D |

### 7.1 Jednostka dietetyka (W-14 do W-16)

Jedna jednostka to **1 dietetyk i 60 klientów** z realistycznymi danymi:

- każdy klient ma 60–90 dni dziennika, aktywny plan i historię czatu;
- dietetyk ma oferty, ankiety i kalendarz wizyt.

Liczba klientów na dietetyka jest parametrem, więc ten sam test policzy też np. siłownię z 300 członkami.
Częstości poniżej to założenia startowe. Kalibrujemy je:

- nagraniem prawdziwych sesji: panel w przeglądarce i aplikacja mobilna przez proxy, eksport HAR, import do
  Gatling Recordera;
- za Twoją zgodą także agregatami z prod (np. średnia liczba wpisów posiłków na aktywnego klienta dziennie, wywołania
  generatora na specjalistę dziennie).

| Kto | Czynność | Założona częstość |
|---|---|---|
| Dietetyk (panel web, dni robocze 8–18) | przegląd karty klienta (dziennik tygodnia, postęp, pomiary) | 20 / dzień |
| | szkic planu z generatora: dieta / trening | 3–6 / dzień i 1–3 / dzień |
| | korekty (rewizje) i publikacja | 10 / dzień |
| | czat, ankiety, kalendarz, PDF | 30, 2, 5 i 3 / dzień |
| Klient (aplikacja mobilna, ok. 70% aktywnych dziennie) | otwarcie aplikacji (seria równoległych żądań startowych) | 4 / dzień |
| | wpis posiłku / woda | 4 i 5 / dzień |
| | waga / pomiary | 1 / tydzień i 1 / 2 tygodnie |
| | czat, podgląd planu, lista zakupów | 1–2, 1 i 1 / dzień |
| | synchronizacja Apple Health / Health Connect (tylko aplikacja Diecik) | 2 / dzień |
| | zdjęcie posiłku (AI) | 1–3 / dzień |

**Rozkład w ciągu doby i tygodnia:**

- klienci mają szczyty w godzinach 7–9, 12–14 i 18–21;
- dietetycy generują najwięcej w poniedziałek w godzinach 9–11;
- w niedzielę wieczorem przypada najwięcej publikacji.

**Zdarzenia skorelowane:** publikacja planów wysyła push, a wtedy wielu klientów jednego dietetyka otwiera aplikację
naraz (W-16).

**Pierwsza hipoteza, do sprawdzenia, a nie wynik.** Zwykłe API ma dziś duży zapas: średnie CPU wynosi 0,6%.
Pierwszym ograniczeniem będzie więc generator przy jednoczesnej pracy wielu dietetyków (W-15), a nie liczba
klientów. Jeśli to się potwierdzi, pierwszą rekomendacją z planu napraw będą F1 i F2, a nie skalowanie.

**Reguła skalowania** (wynik W-14 wpisujemy do tabeli w sekcji 10.3):

1. Próg skalowania to 70% N_max obecnego wariantu. Zapas jest potrzebny na skoki ruchu i na czas wdrożenia zmiany.
   Drugim sygnałem jest przekroczenie SLO w monitoringu prod.
2. Zanim przeskalujesz, sprawdź w tabeli z sekcji 10.3, jaki udział w przychodzie przy tym N miałby kolejny wariant.
   Jeśli udział jest wysoki, raport wskaże poprawki z planu napraw, które podnoszą N_max taniego wariantu.
3. Generator wydzielamy osobno (T4) dopiero wtedy, gdy to on jest wąskim gardłem, a API ma jeszcze zapas.

Każdy test ma hipotezę, kryterium pass/fail i wynik w `results/<data>/<test>/`: raport Gatlinga, zrzut metryk
CloudWatch i nagranie JFR.

### Hipotezy dla generatora (do potwierdzenia w W-02, W-03 i W-11)

- **H1:** `Schedulers.parallel()` ma 1 wątek, więc równoległe generacje ustawiają się w kolejce. Czas N-tego
  użytkownika to mniej więcej N × czas jednej generacji.
- **H2:** 256 MB heapu i SerialGC (dużo alokacji `BigDecimal`) dają pauzy stop-the-world, które zatrzymują całe API.
- **H3:** limit 0,5 vCPU (throttling CFS): generacja zjada przydział CPU i wątki event-loop Netty też zwalniają.
- **H4:** timery Reactora (`timeout`, `delay`) działają na tym samym `parallel()`, więc długa generacja opóźnia
  timeouty innych żądań.
- **H5:** po zerwaniu połączenia `Mono.fromCallable` liczy dalej i marnuje CPU.
- **H6:** przeładowanie snapshotu katalogu daje skok pamięci (zob. W-13).

### Plan poprawek (zasada Bartosza z 2026-11-21: bez zmian w kodzie bez jego zgody)

Testy mierzą stan obecny. Poprawki opisujemy w planie napraw razem z dowodami z testów i wyceną. Wdrażamy je tylko po
zgodzie, przez `dev`. Wyjątkiem są warianty czysto konfiguracyjne w lokalnym stacku testowym, np. flaga JVM F1
ustawiona w `local/docker-compose.yml` tego repo. Pozwalają pokazać efekt bez dotykania kodu aplikacji i w raporcie
są oznaczone jako eksperyment.

- **F1:** `JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=70` plus porównanie SerialGC i G1. Bez kosztu.
- **F2:** osobna, ograniczona pula wątków generatora z kolejką. Przy pełnej kolejce szybka odpowiedź 429/503
  z `Retry-After`. Event loop i `parallel()` zostają wolne. Bez kosztu.
- **F3:** większy task: 1 vCPU / 2 GB (+12–20 USD/mies. na task).
- **F4:** generacja asynchroniczna: zadanie, potem polling albo push. Większa zmiana architektury, potrzebna dla T4.
- **N5:** podwójne przypomnienia przy 2 taskach (sekcja 11, punkt 2). W `SubscriptionReminderJob` i
  `SpecialistPackageJob` najpierw warunkowo stawiamy znacznik (`UPDATE … WHERE reminder_sent_at IS NULL`), a mail
  wysyła tylko instancja, która go postawiła. Ten wzorzec ma już `StudentLicenseService`. Gdy mail się nie uda,
  zdejmujemy znacznik, żeby następny przebieg spróbował ponownie. Bez migracji i bez kosztu.
  **Wdrożone 2026-11-21 na `dev` (niezacommitowane).** Test integracyjny puszcza dwa przebiegi joba naraz: na starym
  kodzie mail wychodzi 2 razy, na nowym raz.
- **N6:** priorytet generacji dietetyków w bramce (`DietGeneratorGate`). Zamiast jednej kolejki FIFO są trzy: trasy
  `/api/trainer/…`, potem klienci indywidualni, potem publiczne demo. Każda ma własny limit kolejki i czasu
  czekania, żeby skok ruchu indywidualnego nie dawał dietetykom 429.
  - Warunek: 4 z 5 tras generatora dietetyka nie mają limitu na użytkownika: `diet-snapshot/generate`,
    `diet-snapshot/fit-day` oraz w parach `generate-day` i `fit-day`. Limit ma tylko `generate-draft-v2`. Bez limitu
    jedno konto mogłoby zapchać kolejkę priorytetową.
  - Trwającej generacji nie da się przerwać, więc dietetyk czeka najwyżej na koniec jednej z 2 trwających generacji.
  - Bez kosztu. Sprawdzenie: przeciążenie od klientów indywidualnych plus jednostka dietetyka (W-14), porównanie p95
    i liczby 429 u dietetyka.

## 8. Izolacja danych (współbieżność i autoryzacja)

**I-1. Kanarki pod obciążeniem.** Każdy użytkownik testowy ma unikalny znacznik w swoich danych, np.
`perf-u0042-<losowe>` w nazwie własnego posiłku, notatce i profilu. Zna go tylko on. W Gatlingu każda odpowiedź jest
sprawdzana na dwa sposoby:

- czy nie zawiera znacznika innego użytkownika;
- czy ID właściciela w odpowiedzi to ID tego użytkownika.

Trafienie to natychmiastowy fail i zapis pełnego żądania i odpowiedzi. Po teście uruchamiamy zapytania spójności
w bazie: właściciel dni diety, posiłków i list zakupów zgadza się z twórcą, a obiekty powiązane nie mają cudzego
`user_id`.

**I-2. Wyścigi.** JUnit z wirtualnymi wątkami i barierą startową, żeby wszystko ruszyło w tej samej milisekundzie:

- 200 użytkowników naraz generuje tydzień. Każdy musi dostać plan dla swoich celów i **swoich alergii**: cudzy plan
  z alergenem to ryzyko zdrowotne.
- Preview → commit: ten sam `previewId` dwa razy równolegle (idempotencja) i commit cudzego `previewId`.
- Para: obie osoby jednocześnie generują i edytują, usuwanie posiłku partnera w trakcie generacji. Ten obszar miał
  błąd 2026-11-20.
- Trener: dwie karty albo dwóch specjalistów edytuje plan tego samego klienta (lost update), publikacja rewizji.
- Lista zakupów (advisory lock) i czat (kolejność, przypisanie do rozmowy).
- Booking: podwójna rezerwacja tego samego terminu.
- Checkout: podwójne kliknięcie.
- Równoległe żądania w różnych językach i regionach: język i katalog nie przechodzą między żądaniami.

**I-3. Macierz autoryzacji (IDOR).** Inwentarz tras budujemy z kodu backendu: 637 mapowań, w tym 360 z ID w ścieżce.
Dla każdej trasy z ID użytkownik B tworzy zasób, a potem próbują go odczytać lub zmienić (GET, PUT, DELETE):

- użytkownik A;
- trener innej firmy;
- organizacja B;
- zwykły użytkownik na trasach trenera.

Oczekujemy 403 lub 404, nigdy 2xx z danymi B.

Priorytet:

- dane zdrowotne;
- czat;
- PDF i eksporty;
- zdjęcia: czy URL-e S3 są publiczne lub do zgadnięcia;
- faktury;
- organizacje (multi-tenant aplikacji firmowej);
- pary.

Do tego ręczne przypadki z ID w query i body (np. `clientId`).

**I-4. Cache:**

- każda odpowiedź z danymi użytkownika ma `Cache-Control: no-store` albo `private`;
- nic spod `/api` nie przechodzi przez CloudFront;
- przełączenie konta na tym samym urządzeniu nie pokazuje danych poprzedniego konta. W web dotyczy to cache zapytań
  i service workera, jeśli jest; w mobile pamięci podręcznej i AsyncStorage.

**I-5. Tożsamość przy wielu instancjach:**

- 2 taski backendu i 2 węzły Keycloaka;
- tokeny i sesje działają niezależnie od tego, który węzeł obsłuży żądanie;
- limiter liczy po `sub`.

## 9. Obserwowalność (włączamy w `dev`; w prod według decyzji)

- Container Insights dla ECS, RDS Performance Insights (7 dni gratis), `pg_stat_statements`.
- JFR na tasku backendu (`-XX:StartFlightRecording`, zrzut na koniec testu) i logi GC. Pokażą gorące miejsca
  generatora, alokacje i pauzy.
- Opcjonalnie Micrometer i actuator (zmiana w backendzie przez `dev`). Na start wystarczy JFR.
- Dashboard CloudWatch dla `dev`; znaczniki faz testu w logach, wszystko w UTC.

## 10. Koszty

Ceny on-demand eu-central-1 z AWS Pricing API (2026-11-21):

- Fargate x86: 0,04656 USD za vCPU-h i 0,00511 USD za GB-h;
- Fargate ARM: 0,03725 USD za vCPU-h i 0,00409 USD za GB-h;
- RDS t4g.small: 0,037 USD/h;
- RDS t4g.medium: 0,074 USD/h;
- RDS m7g.large Multi-AZ: 0,401 USD/h;
- ALB: 0,027 USD/h plus 0,008 USD za LCU-h;
- publiczne IPv4: 0,005 USD/h;
- WAF: 5 USD za ACL i 1 USD za regułę miesięcznie.

Kwoty brutto zawierają 23% VAT, tak jak na fakturze. Model dla T0 daje 112 USD netto, a faktura za październik 113,
więc model jest skalibrowany.

### 10.1 Kampania testowa

| Pozycja | Koszt |
|---|---|
| `dev` w rozmiarze prod, pod obciążeniem | ok. 0,21 USD/h |
| Generator ruchu EC2 c7g.xlarge | 0,165 USD/h |
| `dev` w rozmiarze T3 (testy HA) | ok. 0,52 USD/h |
| Typowa kampania: 4–5 sesji po 6–8 h + soak 8 h | **ok. 15–40 USD netto** |
| Zabezpieczenie | AWS Budget z alertem przy 50 USD dla tagu `Environment=dev` + wyłączenie `dev-edge` po każdej sesji |
| Po testach: `dev-edge` wyłączony, core zostaje (RDS t4g.small, ECR, S3/CloudFront) | ok. 32–35 USD/mies. |
| Po testach: cały dev zostaje jako staging w rozmiarze prod | ok. 110 USD/mies. (mniej po zmniejszeniu bazy i tasków) |
| Po testach: pełny destroy (jak dziś) | 0 USD |

### 10.2 Miesięcznie po skalowaniu

| Wariant | Zawartość | Netto USD/mies. | Brutto |
|---|---|---|---|
| **T0** obecnie | backend 1× 0,5 vCPU / 1 GB, Keycloak 1× 0,25 vCPU / 1 GB, RDS t4g.small 1 AZ | ~112 (faktura: 113) | ~137 (faktura: 140) |
| **T1** strojenie | jak T0 + F1/F2 + obrazy ARM (Graviton, −20% na Fargate) | ~105 | ~129 |
| **T2** mały wzrost | backend 2× 1 vCPU / 2 GB ARM (autoscaling do 4), Keycloak 1× 0,5 vCPU, RDS t4g.medium 1 AZ, Container Insights | ~208 | ~256 |
| **T3** HA | backend średnio 3× 1 vCPU / 2 GB, Keycloak 2× 0,5 vCPU, RDS t4g.medium Multi-AZ, WAF stale włączony | ~348 | ~428 |
| **T4** wzrost + worker generatora | API 2× 1 vCPU / 2 GB, worker generatora średnio 3× 2 vCPU / 4 GB (kolejka zadań), Keycloak 2× 1 vCPU / 2 GB, RDS m7g.large Multi-AZ 100 GB | ~787 (z Fargate Spot dla workerów ~655) | ~967 (~806) |

**Bramka generatora przy skalowaniu (`app.diet-generator.gate.max-concurrent` = 2):**
- **Więcej tasków:** nic do zmiany. Każdy task ma własną bramkę, więc 3 taski to 6 generacji naraz.
- **Większy task, do 2 vCPU (T1–T4):** 2 zostaje. Jedna generacja liczy na jednym wątku, więc 2 naraz zajmą najwyżej
  2 CPU.
- **Powyżej 2 vCPU:** trzeba podnieść ręcznie, bo przy 2 reszta CPU stoi.
  - Zmienna `APP_DIET_GENERATOR_GATE_MAX_CONCURRENT` nie wymaga przebudowy obrazu.
  - W `envs/prod-edge/main.tf` zmienne środowiskowe są stałą listą, więc trzeba dopisać zmienną Terraform obok
    `backend_task_cpu` i `backend_task_memory`.
  - Pamięć musi rosnąć razem z limitem.
- **Samo się dopasowuje:** heap (50% pamięci taska) i liczba wątków generatora (`max(2, liczba CPU)`).
- **Do zmierzenia przy przejściu na 2 vCPU i ≥ 2 GB:** JVM sam przełącza się z SerialGC na G1, więc zmienia się
  profil pamięci.

Rozbicie netto (USD/mies.):

| | backend | Keycloak | RDS | ALB | IPv4 | WAF | CloudWatch | reszta (R53, Secrets, S3) |
|---|---|---|---|---|---|---|---|---|
| T0 | 21 | 12 | 30 | 20 | 15 | 0 | 2 | 12 |
| T1 | 17 | 10 | 30 | 20 | 15 | 0 | 2 | 12 |
| T2 | 66 | 17 | 57 | 28 | 18 | 0 | 10 | 12 |
| T3 | 99 | 33 | 114 | 37 | 26 | 11 | 15 | 12 |
| T4 | 265 | 66 | 320 | 49 | 33 | 15 | 25 | 13 |

Do własnych przeliczeń:

- task 1 vCPU / 2 GB: 33 USD/mies. na ARM, 41 na x86;
- worker 2 vCPU / 4 GB: 66 USD na ARM;
- każdy dodatkowy task: +3,65 USD za publiczny IPv4;
- RDS t4g.small → t4g.medium: +27 USD;
- Single-AZ → Multi-AZ: koszt instancji ×2.

Po ustabilizowaniu konfiguracji można obniżyć koszt (wartości przybliżone):

- Compute Savings Plan na 1 rok: ok. −20% na Fargate;
- RDS Reserved na 1 rok: ok. −30%.

VAT 23% (26 USD/mies.) może przejść na odwrotne obciążenie, jeśli konto AWS ma podany NIP firmy. Do potwierdzenia
z księgową.

**Pojemność każdego wariantu** wpiszemy po W-14 (tabela w sekcji 10.3). Przed testem byłoby to zgadywanie.

### 10.3 Ekonomika: czy infrastruktura nie zjada zysku

Założenia potwierdzone przez Bartosza 2026-11-21:

- ceny 100 zł i 600 zł są brutto (VAT 23%);
- prowizja P24 wynosi 1,3%;
- klienci dietetyka nie płacą, więc ich przychód liczymy jako 0.

Dodatkowo:

- kurs NBP z 2026-11-20: 4,0613 zł/USD;
- koszty AWS liczymy netto, czyli przy założeniu, że VAT z faktury odliczasz (jeśli nie, +23%).

Przychód netto na dietetyka, po odjęciu VAT i prowizji:

| Plan | Przychód netto |
|---|---|
| standard (100 zł) | 80,00 zł/mies. |
| własna aplikacja (600 zł) | 480,00 zł/mies. |

**Próg rentowności samej infrastruktury:**

| Wariant | AWS netto | Dietetycy standard, żeby pokryć | albo z własną aplikacją | N_max z W-14 | Próg skalowania (70% N_max) | Infrastruktura na dietetyka przy progu |
|---|---|---|---|---|---|---|
| T0 obecnie | 455 zł | 6 | 1 | _po teście_ | _po teście_ | _po teście_ |
| T1 strojenie | 426 zł | 6 | 1 | _po teście_ | _po teście_ | _po teście_ |
| T2 mały wzrost | 845 zł | 11 | 2 | _po teście_ | _po teście_ | _po teście_ |
| T3 HA | 1413 zł | 18 | 3 | _po teście_ | _po teście_ | _po teście_ |
| T4 worker generatora | 3196 zł | 40 | 7 | _po teście_ | _po teście_ | _po teście_ |

**Początek, wariant T1 (426 zł/mies.), koszt infrastruktury na dietetyka:**

| Liczba dietetyków | Na dietetyka | Udział w przychodzie (standard) | Udział w przychodzie (własna aplikacja) |
|---|---|---|---|
| 1 | 426 zł | 533% | 89% |
| 3 | 142 zł | 178% | 30% |
| 6 | 71 zł | 89% | 15% |
| 10 | 43 zł | 53% | 9% |
| 20 | 21 zł | 27% | 4% |
| 40 | 11 zł | 13% | 2% |
| 60 | 7 zł | 9% | 1% |

**Cel: 40 dietetyków.** Przychód netto wynosi 3200 zł/mies. przy samych planach standard albo 6400 zł przy mieszance
80/20. Udział infrastruktury w przychodzie:

| Wariant | Same plany standard | Mieszanka 80/20 |
|---|---|---|
| T1 | 13% | 7% |
| T2 | 26% | 13% |
| T3 | 44% | 22% |

Celem testów jest więc to, żeby 40 dietetyków zmieściło się w T1.

Przy samych planach standard infrastruktura spada poniżej 15% przychodu od ok. 36 dietetyków. Przy mieszance 80%
standard i 20% z własną aplikacją (średnio 160 zł) dzieje się to od ok. 18 dietetyków. Budżetu infrastruktury
(dopuszczalnego udziału w przychodzie) nie ustalamy teraz. Raport pokaże udział dla każdej liczby dietetyków,
a decyzja należy do Bartosza.

**Jak nie przepalić pieniędzy na początku:**

1. Nie skalujemy na zapas. Zostajemy na T1, dopóki W-14 i monitoring nie pokażą progu.
2. F1, F2 i obrazy ARM kosztują 0 zł albo dają −30 zł/mies. i podnoszą N_max taniego wariantu. To najtańsza
   „pojemność”, jaką da się kupić.
3. Dev po testach (decyzja 7): staging w rozmiarze prod to 447 zł/mies., czyli przychód od ok. 5,6 dietetyka. Lepiej
   go zburzyć albo zostawić sam core (ok. 130–140 zł).
4. VAT na fakturze AWS to ok. 106 zł/mies. Z NIP-em firmy w ustawieniach podatkowych AWS może przejść na odwrotne
   obciążenie. Do sprawdzenia z księgową.
5. Savings Plan i Reserved Instances (−20–30%) kupujemy dopiero przy konfiguracji stabilnej od ok. 6 miesięcy.
6. AWS Budgets z alertami, np. przy 150 i 250 USD, żeby wzrost kosztu nie przeszedł niezauważony.
7. Koszty zmienne na klienta są dziś małe, ale je mierzymy:
   - AI do zdjęć: ok. 0,16 USD za 1000 analiz, czyli ok. 0,6–0,9 USD/mies. na jednostkę 60 klientów;
   - maile;
   - przechowywanie zdjęć w S3.

**Własna aplikacja (600 zł): koszty, których nie ma na fakturze AWS.** Backend jest wspólny, więc dodatkowa firma
w AWS to tylko dodatkowy ruch. Liniowo z liczbą firm rosną natomiast:

- buildy i aktualizacje EAS (plan Expo, do sprawdzenia);
- Twój czas na każde wydanie × liczba aplikacji × 2 sklepy;
- ryzyko przeglądów Apple (wytyczne 4.2.6 i 4.3).

Konta deweloperskie Apple i Google należą do klienta. Te pozycje warto wycenić w godzinach i doliczyć do marży
planu 600 zł.

## 11. O czym łatwo zapomnieć

Punkty z ★ wynikają z dzisiejszej analizy kodu i infrastruktury.

1. ★ **JVM bez ustawień pamięci** (sekcja 1). Generator dzieli jedyny wątek `parallel()` z timerami Reactora.
2. ★ **Joby `@Scheduled` (15) przy wielu instancjach.** ShedLocka nie ma. Sprawdzone w kodzie 2026-11-21:
   - **Bezpieczne (13 z 15):** joby rezerwują pracę w bazie, więc druga instancja jej nie dubluje.
     - Blokada wiersza z leasem lub tokenem: rekoncyliacja P24, płatności cykliczne, linki Meet, Polar, Garmin.
     - Warunkowy `UPDATE`: wygasanie subskrypcji, rezerwacji i nieopłaconych zamówień.
     - Advisory lock z `ON CONFLICT`: ranking przepisów.
     - Najpierw oznaczenie, potem mail: przypomnienie o licencji studenckiej.
     - Ponowny odczyt pod blokadą: odnowienia pakietów.
     - Idempotentne `DELETE`: retencje i sprzątanie.
   - **Podwójny mail (2):** `SubscriptionReminderJob` (co godzinę o :00) i przypomnienia w `SpecialistPackageJob`
     (o :15). Oba najpierw szukają rekordów bez znacznika, potem wysyłają mail, a znacznik stawiają dopiero na końcu.
     Cron odpala się na obu instancjach w tej samej sekundzie, więc przy 2 taskach mail prawie na pewno idzie
     2 razy. Pieniędzy to nie dotyczy.

   **To dotyczy już dziś:** deploy z `maximum_percent = 200` i autoscaling do 2 uruchamiają 2 instancje naraz.
   Poprawka to N5 w planie napraw (wdrożona na `dev`); ShedLock nie jest potrzebny.
3. ★ **Limity w pamięci taska:** przy N taskach i ALB bez sticky sessions realny limit rośnie N razy. Przy skalowaniu
   trzeba wspólnego licznika (Postgres albo Redis) albo świadomej akceptacji.
4. ★ **Limiter publicznego demo generatora w prod liczy po adresie węzła ALB.** Ustawienia: `trusted-proxy-hops` = 0,
   `SERVER_FORWARD_HEADERS_STRATEGY=NONE`. Wszyscy anonimowi dzielą więc 2–3 kubełki, a limit 5 / 15 min działa
   praktycznie globalnie. Do tego `ApiRateLimitFilter` bierze pierwszy wpis `X-Forwarded-For` (da się go podrobić),
   a pozostałe limitery biorą ostatni. Trzeba to ujednolicić; potwierdzi to W-10.
5. ★ **ALB ucina po 60 s, a obliczenie liczy dalej.** Porzucone generacje zjadają CPU, użytkownik klika ponownie,
   robi się lawina (W-11).
6. ★ **Autoskalowanie jest za wolne na skoki.** Alarm CPU działa w skali minut, start JVM trwa ok. 2 min przy 100%
   CPU, grace period to 180 s, więc nowy task jest gotowy po 3–5 min. Minimum 1 task to brak HA, a Single-AZ RDS
   to przestój przy awarii strefy.
7. ★ **Keycloak 0,25 vCPU.** Hashowanie haseł jest celowo kosztowne. Fala logowań (kampania, push, nowa wersja
   aplikacji) to osobne wąskie gardło. Brak brute-force protection w szablonie realmu trzeba sprawdzić w prod.
8. **Siłownia za jednym NAT-em.** Członkowie na wspólnym Wi-Fi mają jedno IP. Włączony WAF z limitem
   3000 / 5 min / IP może zablokować całą siłownię. Trzeba to przetestować i dobrać limit.
9. **Zewnętrzne API.**
   - OpenAI i Bedrock są płatne za wywołanie.
   - OpenFoodFacts to cudzy serwis i nie wolno go obciążać.
   - P24, Google Meet, Garmin, Polar i SMTP w `dev` dostają stuby.

   Osobny test: zewnętrzne API wolne albo niedostępne (timeouty, brak lawiny).
10. **Dane w rozmiarze prod.** Puste tabele dają fałszywie dobre wyniki.
11. **Coordinated omission.** Stała liczba użytkowników ukrywa kolejkę generatora, więc dla generatora stosujemy
    stały napływ żądań na minutę.
12. **Miejsce generatora ruchu.** Mac na Wi-Fi mierzy własne łącze. Pomiary robimy z EC2 we Frankfurcie, a jeden
    przebieg z Polski pokaże czas po stronie klienta.
13. **Wzorce ruchu mobilnego:**
    - równoległe żądania przy starcie aplikacji;
    - fala zimnych startów po aktualizacji OTA;
    - synchronizacja Apple Health i Health Connect w tle o podobnych porach;
    - paczki webhooków Garmin.
14. **Snapshot katalogu generatora** (`RecipeGeneratorCatalogService`). Po edycji przepisu w adminie każdy task
    przeładowuje cały katalog w trakcie ruchu (W-13).
15. **Rosnące mapy w pamięci**, np. liczniki w `MealPickerService` dla każdej kombinacji filtrów. Soak test (W-07)
    pokaże, czy heap rośnie.
16. **Regresja w CI.** Krótki test generatora z limitami prod przy PR-ach, które go dotykają. Before/after na
    identycznym katalogu i celach, zgodnie z lekcją z listopada.
17. **RODO.** Nie kopiujemy danych użytkowników prod do `dev`, tylko katalog i agregaty.
18. **Testujemy też alarmy.** Przy W-05 i W-06 sprawdzamy, czy przychodzą maile z SNS.
19. **Frontend** (CloudFront + S3) nie wymaga testu obciążenia, bo stoi za CDN. Czas pierwszego ładowania SPA to
    osobny temat (Lighthouse), poza zakresem.

## 12. Etapy

- [ ] **Etap 0. Decyzje** (sekcja 13) i ewentualna zgoda na ALB access logs (7 dni) oraz agregaty z prod.
- [x] **Etap 1. Szkielet projektu** (2026-11-21): Maven, Gatling 3.16.1, JUnit 6.1.3, konfiguracje `local` i `dev`,
  `ProdGuard` z testami, README.
- [ ] **Etap 2. Lokalnie (L):**
  - [x] stack z limitami prod (`local/`), klon katalogu i realmu, seed kont, tokeny;
  - [x] W-02 i W-03 oraz eksperyment F1: raport `reports/2026-11-21-lokalnie-generator.md`. H1–H3 potwierdzone;
    70% heapu przy 1 GB kończy się OOM kill;
  - [ ] W-10 (limiter był widoczny przypadkiem), W-11, W-13, H4–H6 z JFR;
  - [ ] seed jednostek dietetyka (plan PRO, 60 klientów, powiązania) i scenariusz W-14/W-15 lokalnie.
- [ ] **Etap 3. Izolacja lokalnie:**
  - [ ] I-1 do I-5;
  - [ ] inwentarz tras i macierz IDOR (najpierw dane zdrowotne, czat, organizacje, pary).

  Znaleziony błąd izolacji naprawiamy (przez `dev`) przed dalszymi testami.
- [ ] **Etap 4. Dev w AWS (T0):**
  - [ ] tfvars dev w rozmiarach prod i odbudowa według przewodnika. Jeśli potrzebne są zmiany kodu Terraform
    (parzystość z prod), przedstawiamy listę do akceptacji;
  - [ ] obrazy backendu i Keycloaka z prod, katalog, seed, EC2 do generowania ruchu, monitoring;
  - [ ] W-01 do W-13, z zamrożonymi pushami na `dev`;
  - [ ] wyłączenie `dev-edge` między sesjami.
- [ ] **Etap 5. Skalowanie i plan napraw:**
  - [ ] konfiguracje T2 i T3 oraz W-09 (przez tfvars, bez zmian w kodzie);
  - [ ] plan napraw z dowodami i wyceną (F1–F4, błędy izolacji, joby, limitery); wdrożenie tylko po zgodzie, przez
    `dev`, potem powtórka W-02, W-03 i W-05;
  - [ ] stan dev po testach według decyzji 7.
- [ ] **Etap 6. Raport:**
  - [ ] pojemność każdej konfiguracji w jednostkach dietetyka (N_max), próg skalowania i koszt infrastruktury na
    dietetyka (uzupełniona tabela z sekcji 10.3);
  - [ ] lista błędów;
  - [ ] rekomendacja;
  - [ ] smoke w CI;
  - [ ] sprzątanie.

Szacunek: ok. 6–9 dni pracy plus Twoje akceptacje na etapach 0, 3 (poprawki), 4 i 5 (AWS i deploy na `dev`).

## 13. Decyzje do podjęcia

1. **Środowisko:** ✅ zdecydowane 2026-11-21: dev odbudowany w rozmiarach prod, potem skalowany.
2. **Narzędzie:** Gatling (rekomendacja przyjęta bez zastrzeżeń 2026-11-21).
3. **Cele biznesowe:** ✅ 40 dietetyków w 12 miesięcy.
4. **Zgody** (✅ 2026-11-21):
   - odbudowa dev w AWS na czas testów; zmiany kodu Terraform (np. wyrównanie `dev-edge` do `prod-edge`) tylko po
     osobnej zgodzie;
   - logi tylko na dev na czas testów (prod nie ma jeszcze dietetyków, więc jego logi nic nie wniosą);
   - odczyt z prod jest dozwolony, ale potrzebna jest tylko kopia katalogu (przepisy, produkty), bez agregatów
     użytkowników.
5. **Poprawki F1 i F2:** tylko w planie napraw; wdrożenie po osobnej zgodzie (patrz punkt 11).
6. **Git:** ✅ podpięte 2026-11-21. `origin` to `github.com/BartekSzeliga/health-app-performance-tests`, gałąź
   `main`, bez commitów (commitujesz Ty).
7. **Dev po testach:** ✅ wraca do obecnego stanu, czyli pełny destroy `dev-edge` i `dev` (0 USD). Kod Terraform
   (wyrównanie do prod) zostaje w repo, niezacommitowany.
8. **Ceny:** ✅ brutto, prowizja P24 1,3%, klienci dietetyka nie płacą.
9. **Budżet infrastruktury:** nie ustalamy teraz. Raport pokaże udział w przychodzie dla każdej liczby dietetyków.
10. **Typowy klient planu 600 zł:** nieznany. Jednostka domyślna to 60 klientów; jeden przebieg kontrolny robimy dla
    300 członków.
11. **Zmiany w kodzie** aplikacji i infrastruktury: ✅ tylko po zgodzie Bartosza. Testy mierzą stan obecny, a poprawki
    trafiają do planu napraw.

## 14. Ryzyka

- `dev` z danymi syntetycznymi nie odwzoruje prod 1:1. Kalibrujemy go metrykami z prod (sekcja 1).
- Zapomniane środowisko `dev` generuje koszt: ok. 5–9 USD na dobę. Chroni przed tym budżet z alertem i `destroy`
  po każdej sesji.
- Wykrycie IDOR lub wycieku danych zatrzymuje testy wydajności do czasu poprawki.
