# Pojemność na AWS dev: 2 taski backendu i 2 taski Keycloaka (2026-11-21)

Pytanie Bartosza: ilu dietetyków obsłuży bezpiecznie konfiguracja 2× backend i 2× Keycloak po 0,5 vCPU / 1 GB, kiedy
przejść na 1 vCPU i ile uciągną 2 taski po 1 vCPU. Ten raport zastępuje szacunek z `PLAN.md` §10.4 pomiarem.

## Warunki

- **Środowisko:** dev odbudowany na czas testu w rozmiarach prod: RDS `db.t4g.small` Single-AZ, ALB bez sticky
  sessions, WAF wyłączony (jak w prod). Taski x86, liczba stała (min = max = 2), więc autoskalowanie nie wpływa na wynik.
- **Kod:** obrazy z gałęzi `dev`, commit `34133520` (N1 heap 50%, N3 bramka generatora, N5 przypomnienia).
- **Dane:** 20 dietetyków z planem PRO i zaakceptowaną umową powierzenia oraz 600 powiązanych klientów. Każdy klient
  ma opublikowany plan 7-dniowy i wpisy wody. Do tego 100 kont indywidualnych. Katalog: 39 492 produkty i 425 przepisów
  z lokalnego klonu, bez kont i danych użytkowników. Maile wyłączone.
- **Generator ruchu:** Gatling na laptopie (Polska → Frankfurt), z współdzielonymi połączeniami.
- **Różnice względem prod:** pula DB Keycloaka ma wartości domyślne Keycloaka (prod: maks. 10); w dev brak zmiennych
  maili, sklepów mobilnych i restore credentials. Na wynik nie wpływają.

## Model ruchu: jednostka dietetyka

Jedna jednostka to dietetyk i 60 klientów (42 aktywnych) w najbardziej obciążonej godzinie dnia. Symulacja
`DietitianUnitsSimulation`, model otwarty (żądania przychodzą w stałym tempie niezależnie od szybkości serwera):

| Kto | Akcja | Na jednostkę i sekundę |
|---|---|---|
| Klient | otwarcie aplikacji: 8 równoległych GET ekranu startowego | 0,0070 |
| Klient | podgląd planu i listy zakupów | 0,00175 |
| Klient | wpis wody | 0,00875 |
| Klient | otwarty czat: odpytanie listy i wiadomości | 0,021 |
| Dietetyk | otwarty czat w panelu (polling co 5 s, 2 GET) | 0,05 |
| Dietetyk | karta klienta: 5 GET | 0,00083 |
| Dietetyk | szkic 7-dniowy z generatora | 0,0011 |

Daje to ok. **0,22 żądania/s na jednostkę**. Najwięcej, ok. 45%, robi polling czatu w panelu.

## Wynik 1: 2× backend 0,5 vCPU / 1 GB, 2× Keycloak 0,5 vCPU / 1 GB

Po 2,5 min rozgrzewki kroki po 3 minuty. CPU w procentach przydziału taska (0,5 vCPU = 100%), najgorsza minuta kroku,
bez pierwszej minuty po zmianie kroku.

| Jednostki | Żądań/s | Błędy | p95 ALB | Backend CPU: średnio / najbardziej obciążony task | Keycloak CPU | RDS CPU | Połączenia RDS |
|---|---|---|---|---|---|---|---|
| 25 | 5 | 0 | 35 ms | 6% / 10% | 1% | 7% | 19 |
| 50 | 10 | 0 | 46 ms | 12% / 21% | 7% | 7% | 20 |
| 100 | 21 | 0 | 66 ms | 25% / 44% | 8% | 7% | 21 |
| 150 | 32 | 0 | 51 ms | 27% / 37% | 15% | 9% | 27 |
| 200 | 44 | 0 | 101 ms | 39% / 60% | 6% | 9% | 27 |
| 300 | 65 | 0 | 105 ms | 53% / 65% | 4% | 12% | 29 |

- **Pamięć backendu:** 82–84% limitu 1 GB przez cały czas, bez wzrostu z obciążeniem. To stały ślad JVM przy heapie
  512 MB (N1), a nie przeciek. Zapas jest jednak mały.
- **Keycloak:** pamięć 61–62% limitu, CPU poniżej 15%. Przy tej skali prawie nie pracuje.
- **RDS:** CPU do 12%, poniżej progu bazowego `t4g` (20%), więc kredyty CPU się nie wyczerpują.
- **Ekstrapolacja:** CPU backendu rośnie liniowo, o ok. 0,14 punktu procentowego na jednostkę, od podstawy ok. 11%.
  Wynika z tego 70% średniego CPU przy ok. 430 jednostkach i nasycenie przy ok. 550–600.

### Padnięcie jednego taska backendu

Stały ruch, w 3. minucie `stop-task` jednego z 2 tasków. Zastępczy task startuje ok. 73 s i jest zdrowy w ALB po ok.
3 minutach od zatrzymania.

| Jednostki | Sam jeden task (ok. 2,5 min) | Po dołączeniu zimnego taska | Błędy |
|---|---|---|---|
| 60 | 100% CPU, p95 0,08–0,13 s | 1 minuta z p95 2,0 s | 0 |
| 200 | 100% CPU, p95 0,09–0,17 s | ok. 3 minuty z p95 19–31 s | 55 z 25 843 (0,21%): 500 z braku połączenia z puli R2DBC w 5 s, 429 z bramki, 2 timeouty |

**Wniosek:** jeden task uniesie cały ruch aż do 200 jednostek. Problemem jest dopiero nowy, zimny task. ALB od razu daje mu
połowę ruchu, a JVM bez rozgrzanego JIT na 0,5 vCPU przetwarza wolno. Trzyma przy tym połączenia z puli (maks. 12),
a kolejne żądania nie dostają połączenia w 5 s. Tak samo jest przy każdym deployu w godzinach szczytu. To problem N4
(rozgrzewka), który odłożyliśmy. Teraz jest zmierzony. Tańsza poprawka niż rozgrzewka w kodzie to `slow_start` na target
groupie ALB (60–120 s). To zmiana tylko w Terraformie.

### Padnięcie jednego taska Keycloaka

W trakcie pętli 400 logowań przez formularz (kod autoryzacyjny z PKCE, ALB bez sticky sessions) zatrzymany został jeden
z 2 tasków Keycloaka. **Wynik: 400 z 400 logowań i 60 z 60 odświeżeń tokenu bez błędu.** Obejmuje to wyrejestrowanie
z ALB i start zastępczego taska. Taski tworzą klaster przez bazę (`jdbc-ping`, port 7800). Port 57800 do wykrywania
awarii (FD_SOCK2) nie jest otwarty w security group, ale przy łagodnym zatrzymaniu (SIGTERM) to nie przeszkadza.
Twardego crasha (SIGKILL, OOM) nie testowałem.

### Sam generator (W-02 na Fargate)

100 kont indywidualnych, podgląd tygodniowy (7 dni), model zamknięty, 90 s na krok. Sondy lekkich endpointów biegną
równolegle.

| Generacji naraz | Generacji/min | p50 | p95 | Sonda `/public/health` p50 / p95 | Sonda `diet-day` p50 / p95 |
|---|---|---|---|---|---|
| 2 | 223 | 0,45 s | 1,1 s | 31 / 83 ms | 39 / 117 ms |
| 4 | 283 | 0,79 s | 1,9 s | 32 / 76 ms | 41 / 126 ms |
| 8 | 271 | 1,8 s | 3,5 s | 32 / 75 ms | 41 / 124 ms |
| 16 | 280 | 3,3 s | 7,2 s | 47 / 80 ms | 71 / 127 ms |

- **Przepustowość:** maksymalnie ok. 280 generacji 7-dniowych na minutę (2 taski), czyli ok. 2,7× mniej niż laptop przy
  tym samym limicie CPU.
- **Reszta API:** bramka (N3) trzyma resztę API szybką także na Fargate: sondy prawie się nie zmieniają przy 16
  generacjach naraz.
- **Odrzucenia:** przy 16 naraz nie było jeszcze 429, bo kolejka ma 16 miejsc na task.

## Wynik 2: 2× backend 1 vCPU / 2 GB, 2× Keycloak 1 vCPU / 2 GB

Zmiana tylko w tfvars, potem rozgrzewka 2,5 min i kroki po 3 minuty. CPU w procentach przydziału (1 vCPU = 100%).

| Jednostki | Żądań/s | Błędy | p95 ALB | Backend CPU: średnio / najbardziej obciążony task | Keycloak CPU | RDS CPU | Połączenia RDS |
|---|---|---|---|---|---|---|---|
| 50 | 11 | 0 | 45 ms | 18% / 33% | 1% | 6% | 24 |
| 100 | 21 | 0 | 42 ms | 20% / 33% | 7% | 7% | 25 |
| 200 | 44 | 0 | 56 ms | 30% / 41% | 5% | 9% | 28 |
| 300 | 64 | 0 | 52 ms | 31% / 41% | 10% | 11% | 28 |
| 400 | 85 | 0 | 79 ms | 41% / 49% | 3% | 13% | 29 |
| 600 | 129 | 0 | 132 ms | 70% / 85% | 5% | 16% | 29 |

- **Pamięć backendu:** 31–35% limitu 2 GB (heap 1 GB). Zapas jest duży.
- **Keycloak:** pamięć 26–27% limitu, CPU poniżej 10%. Przy tej skali 1 vCPU dla Keycloaka niczego nie zmienia.
- **RDS:** 16% CPU i 29 połączeń przy 600 jednostkach. Kolejnym ograniczeniem po aplikacji będzie baza: powyżej ok.
  20% CPU `t4g.small` zaczyna zużywać kredyty.

### Padnięcie jednego taska backendu przy 1 vCPU

300 jednostek, w 3. minucie `stop-task`. Zastępczy task jest zdrowy w ALB po **84 s** (na 0,5 vCPU po ok. 3 minutach).

| Jednostki | Sam jeden task | Po dołączeniu zimnego taska | Błędy |
|---|---|---|---|
| 300 | 100% CPU, p95 0,18 s | 1 minuta z p95 7,5 s | 3 z 38 788 (0,01%), wszystkie 429 z bramki generatora, 0 × 500 |

### Sam generator przy 1 vCPU

| Generacji naraz | Generacji/min | p50 | p95 | Sondy p50 / p95 |
|---|---|---|---|---|
| 2 | 231 | 0,44 s | 1,0 s | 30–38 / 88–104 ms |
| 4 | 480 | 0,45 s | 0,9 s | 30–36 / 79–84 ms |
| 8 | 578 | 0,80 s | 1,5 s | 64–69 / 80–87 ms |
| 16 | 575 | 1,6 s | 3,5 s | 64–70 / 80–89 ms |

Maksymalnie ok. 575 generacji 7-dniowych na minutę, czyli 2,05× więcej niż przy 0,5 vCPU. Limit bramki (2 na task)
zostaje dobry, zgodnie z §10.2 planu.

## Odpowiedzi

Liczby dotyczą modelu ruchu z tabeli wyżej (0,22 żądania/s na dietetyka z 60 klientami w najbardziej obciążonej godzinie).
Gdy prawdziwi użytkownicy robią więcej żądań, liczby dietetyków maleją proporcjonalnie (patrz „Kalibracja”).

| | 2× 0,5 vCPU / 1 GB | 2× 1 vCPU / 2 GB |
|---|---|---|
| Zmierzone bez błędów | 300 dietetyków (53% CPU) | 600 dietetyków (70% CPU) |
| Normalna praca do 70% CPU | ok. 430 (ekstrapolacja) | ok. 600 (zmierzone) |
| Padnięcie taska lub deploy w szczycie, dziś | 60 bez błędów; przy 200 ok. 3 min z p95 do 30 s i 0,2% błędów | 300: 1 min z p95 7,5 s, 0 × 500 |
| **W miarę bezpiecznie** | **ok. 100 dietetyków** (między zmierzonymi 60 a 200; z `slow_start` na ALB ok. 200) | **ok. 300 dietetyków** |
| Generator, maks. 7-dniowych/min | ok. 280 | ok. 575 |

1. **Ilu dietetyków przy 2× 0,5 vCPU:** w miarę bezpiecznie ok. 100, czyli 2,5× więcej niż cel na 12 miesięcy (40). Przy
   40 dietetykach backend ma ok. 10% CPU, Keycloak ok. 5%, baza ok. 7%. Ograniczeniem nie jest moc w normalnej pracy
   (300 bez błędów), tylko chwila po padnięciu taska albo po deployu: nowy, zimny task na 0,5 vCPU przez kilka minut
   zwalnia odpowiedzi.
2. **Kiedy przejść na 1 vCPU:** backend przy ok. 100 dietetykach albo wcześniej, gdy w normalnym ruchu w najbardziej
   obciążonej godzinie średnie CPU backendu przekroczy 40–50%, p95 przekroczy 300 ms albo deploy w dzień daje skoki
   czasu odpowiedzi. **Keycloak nie potrzebuje 1 vCPU:** przy 600 dietetykach miał poniżej 10% CPU, więc wystarczy mu
   0,5 vCPU / 1 GB.
3. **Ile uciągną 2× 1 vCPU:** zmierzone 600 dietetyków przy 70% CPU bez błędów, a bezpiecznie z zapasem na padnięcie
   taska ok. 300. Powyżej ok. 600 trzeba obserwować RDS `t4g.small`: przy 600 miał 16% CPU, a próg bazowy kredytów to 20%.

## Koszt (Fargate x86 Frankfurt, netto, z publicznym IPv4 tasków)

| Wariant | Taski backend + Keycloak | Cała infrastruktura (T0 + zmiana) | Na dietetyka przy 40 / 100 |
|---|---|---|---|
| 2× 0,5 + 2× 0,5 vCPU | 97 USD (396 zł) | ok. 169 USD (687 zł) | 17 zł / 7 zł |
| backend 2× 1 vCPU, Keycloak 2× 0,5 vCPU | 139 USD (564 zł) | ok. 211 USD (856 zł) | 21 zł / 9 zł |
| 2× 1 + 2× 1 vCPU | 180 USD (733 zł) | ok. 252 USD (1024 zł) | 26 zł / 10 zł |

Przy 80 zł netto od dietetyka standardowego infrastruktura w pierwszym wariancie zjada 21% przychodu przy 40
dietetykach i 9% przy 100.

## Znaleziska

1. **Zimny start pod obciążeniem (N4):** nowy task dostaje od razu połowę ruchu. Przy 0,5 vCPU i 200 dietetykach daje to
   ok. 3 min z p95 do 30 s i błędy 500 z puli połączeń. Dzieje się tak przy każdej awarii i przy deployu w godzinach
   szczytu. Najtańsza poprawka to `slow_start` (np. 90 s) na target groupie backendu w ALB, bez zmian w kodzie
   aplikacji. Pomaga też 1 vCPU (start 84 s zamiast ok. 3 min).
2. **Pamięć backendu na 1 GB: stałe 82–84% limitu** przy heapie 50%. To mało zapasu przed zabiciem kontenera.
   Tania poprawka to 0,5 vCPU / 2 GB (+3,7 USD na task miesięcznie), bo Fargate pozwala na takie połączenie.
3. **Keycloak w klastrze działa** (logowania przez oba taski, łagodne zatrzymanie bez błędów). Security group nie
   otwiera portu 57800 do wykrywania awarii (FD_SOCK2). Do sprawdzenia przy twardym crashu taska, a najprościej
   dopisać regułę w Terraformie (prod-edge i dev-edge).
4. **Start Keycloaka na 0,5 vCPU trwa 92–138 s.** Przy 2 taskach to nie przeszkadza, przy 1 tasku oznacza
   ok. 2 minuty bez logowania i odświeżania tokenów.
5. **Odbudowa dev:** Terraform nie zakłada bazy `keycloakdb` na RDS, a przewodnik
   `docs/dev-rebuild-from-scratch-step-by-step.md` tego nie opisuje. Keycloak restartuje się wtedy w kółko.
   Dodatkowo sekrety `device-integrations` i `booking-google` mają `prevent_destroy`, więc `terraform destroy` dev ich
   nie usunie.
6. **Ponowne ręczne przyznanie planu** (`POST /api/admin/subscriptions/manual-grants`) odpowiada 500 zamiast 409.

## Kalibracja

**Czego brakowało w modelu** (sprawdzone w kodzie po pomiarze):
- aplikacja odpytuje listę rozmów (plakietka czatu) co 15 s przez cały czas, gdy jest otwarta;
- panel odpytuje powiadomienia i licznik nieprzeczytanych co 30 s, gdy karta jest widoczna.

Przy założeniu, że aplikacja jest otwarta ok. 12 minut dziennie, a panel przez cały dzień pracy, to ok. 0,15
żądania/s na jednostkę więcej, razem ok. 0,37 zamiast 0,22. Te żądania są lekkie, więc realnie liczby dietetyków
z tabeli „Odpowiedzi” trzeba podzielić przez ok. 1,5–1,7. Bezpiecznie przy 2× 0,5 vCPU wychodzi wtedy ok. 60–70, a
przy 2× 1 vCPU ok. 180–200. Zastąpienie odpytywania pushem (plan w `tasks/todo.md`) usuwa te żądania i większość
pollingu czatu.

Model ruchu to założenie. Gdy pojawią się pierwsi dietetycy, warto w CloudWatch policzyć żądania ALB do backendu
w najbardziej obciążonej godzinie i podzielić je przez liczbę aktywnych dietetyków. Jeśli wyjdzie np. 0,44 zamiast
0,22 żądania/s, wszystkie liczby dietetyków z tabeli trzeba podzielić przez 2. CPU na żądanie jest zmierzone (przyrost CPU między
krokami): ok. 5–6 ms CPU na żądanie z tego mixu, łącznie z generatorem.

## Odtworzenie

- Seed: `SeedMain dietitians 20 30 10` oraz `SeedMain individuals 100` (`-Dperf.env=dev`, hasło admina Keycloaka
  i dostęp do bazy przez tunel SSM; szczegóły w `DietitianUnitSeeder` i `SeedDatabase`).
- Kroki: `DietitianUnitsSimulation` z `-Dperf.units=25,50,100,150,200,300 -Dperf.stepSeconds=180`.
- Generator: `GeneratorCapacitySimulation` z `-Dperf.steps=2,4,8,16 -Dperf.shareConnections=true`.
- Keycloak: `KeycloakClusterCheck 400 60 https://dev.diecik.com/`.
- Metryki: `scripts/aws_metrics.py <log Gatlinga>`.
