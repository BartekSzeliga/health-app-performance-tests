# Izolacja danych (2026-11-22)

**Stan:** dwie rundy testów. Znaleziska 1 i 2 są poprawione na `dev` (bez commita), a poprawki potwierdził ten sam test,
który je wykrył (sekcja „Poprawki”).

Plan: [PLAN-izolacja.md](../PLAN-izolacja.md). Testy mierzą stan obecny; poprawki wymagają zgody.

## Warunki

- **Środowisko:** lokalnie, backend z `dev` (`d50898e7`) na porcie 8086, osobna pusta baza
  `dietdb_remediation_isolation_20261122`, lokalny Keycloak; bez AWS i bez prod.
- **Świat testu** (`scripts/isolation/world.py`): 15 kont `@perf.diecik.invalid`, dwie lustrzane strony A i B.
  - Każda strona ma:
    - niezależnego specjalistę z 2 klientami połączonymi kontem i kartą tylko z e-mailem;
    - firmę z adminem, członkiem bez dostępu klinicznego, klientem i aktywną aplikacją firmy;
    - użytkownika indywidualnego;
    - parę.
  - Dane we wszystkich głównych dziedzinach, z unikalnym znacznikiem („kanarkiem”) w polach tekstowych.
- **Narzędzia:**
  - `scripts/route_inventory.py`: 649 tras z kodu;
  - `scripts/isolation/idor.py`: macierz;
  - `scripts/isolation/writes.py`: zapisy z poprawną treścią i ID w treści lub nagłówku.

## Znaleziska

### 1. Przejęcie cudzego dnia diety przez `PUT /api/diet-day` (waga: wysoka)

**Co się dzieje.** Endpoint przyjmuje `id` dnia w treści żądania i nie sprawdza właściciela.

- Gdy wywołujący nie ma swojego dnia na tę datę, serwer wykonuje
  `UPDATE diet_days SET user_id = <wywołujący>, … WHERE id = <id z treści>`.
- Dzień ofiary przechodzi na konto atakującego z treścią atakującego, a ofiara traci dzień razem z posiłkami.
- Każde zalogowane konto może to zrobić: w teście zrobili to specjalista B, admin i członek firmy B.

**Odtworzenie** (lokalnie, konta syntetyczne):
1. Ofiara ma dzień `02f9ed9a…` z 1 posiłkiem.
2. `PUT /api/diet-day {"id": "02f9ed9a…", "name": "Przejęte", "dayDate": "…", "meals": []}` jako specjalista B
   zwraca 200 z tym samym `id`.
3. `GET /api/diet-day?date=…` jako ofiara zwraca pustą odpowiedź.
4. Ten sam GET jako specjalista B zwraca dzień `02f9ed9a…` o nazwie „Przejęte”.

**Warunek ataku:** znajomość UUID dnia ofiary. UUID jest losowy, więc nie da się go zgadnąć, ale nie jest też tajemnicą
z założenia. Odczytu cudzych posiłków tą drogą nie stwierdzono: odpowiedź zawiera treść wysłaną przez atakującego.

**Przyczyna:**
- `CreateUpdateDietDayUseCase.handleUpdateDayDiet` buduje dzień z `request.id()`;
- `DietDayDatabaseAdapter.saveDietDay` zapisuje go przez `repository.save(...)` bez warunku `user_id`.

**Propozycja poprawki:**
- ignorować `id` z treści i ustalać dzień po `(userId, dayDate)`: istniejący dzień użytkownika albo nowy;
- dodatkowo zapis `UPDATE … WHERE id = ? AND user_id = ?`;
- test integracyjny „cudzy `id` w treści nie zmienia cudzego dnia”.

### 2. Odpowiedź 500 ujawnia treść wyjątku, w tym zapytanie SQL (waga: średnia)

Gdy atakujący ma własny dzień na tę datę, ten sam `PUT` zwraca 500 z treścią
`execute; SQL [UPDATE diet_days SET user_id = $1, … WHERE diet_days.id = $7]; duplicate key value violates unique …`.

**Przyczyna:** wspólny handler `GlobalExceptionHandler.handleGenericException` (`@ExceptionHandler(Exception.class)`)
odsyła klientowi `ex.getMessage()` przy każdym nieobsłużonym błędzie.

**Propozycja poprawki:** przy 500 stały komunikat („Wystąpił błąd serwera”) i identyfikator zdarzenia; szczegóły tylko
w logu.

### 3. Drobne (waga: niska, bez ujawnienia danych)

- `POST /api/favourite-meals` przyjmuje `mealId` cudzego posiłku i zapisuje go w ulubionych wywołującego. Treść
  posiłku pochodzi z żądania, a nie z cudzego posiłku, więc nic nie wycieka.
- `GET/DELETE /api/recipes/{id}/bookmark` i `DELETE /api/recipes/community/authors/{id}/block` zwracają 2xx dla
  dowolnego ID. To działa na zakładkach i blokadach wywołującego i nie ujawnia cudzych danych.

## Co jest chronione

| Test | Zakres | Wynik |
|---|---|---|
| Macierz odczytu (GET) | 232 odczyty z kontrolą właściciela na trasach z ID; atak kontami drugiej strony i niepowiązanymi kontami tej samej strony, bez nagłówka, z nagłówkiem firmy ofiary i z własnym | tylko 401/403/404, 0 cudzych kanarków |
| Macierz zapisów (ID z kontroli GET) | trasy zapisu z tymi samymi ID | 56 tras jednoznacznie chronionych; reszta zwracała 400 na odgadniętą treść, więc rozstrzygnięta osobno niżej |
| Zapisy z poprawną treścią (`writes.py`) | 22 przypadki, w tym ID w treści i w nagłówku; poniżej pełna lista | odrzucone (403/404), dane ofiary bez zmian; wyjątek: znalezisko 1 |
| Obcy `X-Organization-Id` | każda trasa listy przyjmująca nagłówek firmy, jako specjalista B, admin firmy B i użytkownik Y z nagłówkiem firmy A | brak danych firmy A |
| Członek firmy bez dostępu klinicznego | lista klientów, karta, czat, pomiary klienta firmy | lista pusta, reszta 403 `ORGANIZATION_ACCESS_DENIED` |
| Trasy `/api/admin/**` | 133 trasy jako specjalista, admin firmy i zwykły użytkownik (399 wywołań) | zawsze 403 |
| Kanarki pod obciążeniem (I-1) | 60 s, 32 wątki: 26 618 odczytów i 4675 zapisów | 0 cudzych kanarków; błędy to wyłącznie 429 z limitu zapisów czatu |
| Język przy równoległych żądaniach (I-2) | 2000 żądań naprzemiennie `pl`/`en` | 0 odpowiedzi w złym języku |
| Nagłówki cache (I-4) | 232 odpowiedzi z danymi | wszystkie `no-cache, no-store, max-age=0, must-revalidate`; `api.diecik.com` bez CloudFront |
| Zmiana konta w aplikacjach (I-4) | przegląd kodu | mobile i aplikacja firmowa czyszczą pamięć zapytań w `AuthContext`; web przeładowuje stronę przy wylogowaniu i nie zapisuje zapytań w przeglądarce |
| Limiter przy 2 instancjach (I-5) | przegląd kodu | klucz to `sub` z tokenu, liczniki w pamięci każdej instancji (szczegóły niżej) |

**Przypadki z poprawną treścią** (wszystkie odrzucone, dane ofiary bez zmian):
- **Dane kliniczne:** pomiar i notatka w gabinecie u cudzego klienta, wysłanie ankiety, szkic i publikacja cudzego planu.
- **Czat:** pisanie i oznaczanie przeczytania w cudzej rozmowie, rozmowa innej firmy.
- **Dane klienta:** odpowiedzi w cudzej ankiecie, zmiana cudzego produktu.
- **Firma:** funkcje aplikacji i funkcje klienta w innej firmie.
- **ID w treści:**
  - para z cudzych kart;
  - lista zakupów z cudzego planu i z planu cudzej pary;
  - cudzy szablon ankiety wysłany własnemu klientowi;
  - cudzy prywatny produkt w przepisie, także w przepisie specjalisty;
  - cudzy ulubiony posiłek w dniu diety;
  - `PUT /api/user-details` z cudzym `id` (zmienia dane wywołującego);
  - wpis wody z cudzym ID (zmienia wpis wywołującego).

**Limiter (I-5):** liczniki są w pamięci każdego taska, więc przy 2 taskach limit na użytkownika jest faktycznie do
2× wyższy. To nie dotyczy izolacji danych, tylko kosztu ochrony generatora przy skalowaniu. Gałąź anonimowa
`ApiRateLimitFilter` bierze pierwszy adres z `X-Forwarded-For` (podawany przez klienta), ale jest martwa: trasy
z regułami wymagają tokenu, a Spring Security odrzuca żądanie bez niego wcześniej. Publiczne limitery (demo diety,
ankiety, prezentacja) biorą adres dopisany przez ALB, czyli poprawnie.

## Pokrycie i ograniczenia

- **Pokrycie tras z ID:** 180 z 293 (bez tras publicznych i admina) ma kontrolę właściciela i atak; ostatni przebieg
  to 15 184 żądania w 16 s.
- **Bez kontroli zostały 24 odczyty i 89 zapisów.** Głównie brakuje danych w świecie testu:
  - płatności i faktury, zamówienia pakietów, zamówienia firmy;
  - dostarczone plany (PDF), rezerwacje (`booking-requests`), wizyty kontrolne (`follow-ups`);
  - przepisy społeczności i szkice przepisów, integracje urządzeń, katalog przepisów (pusta baza).

  **To nie jest wynik „bezpieczne”, tylko „nie sprawdzone”.**
- **Nie robione w tej rundzie** (wymagają katalogu produktów albo dodatkowych danych):
  - wyścigi generatora (I-2: 200 generacji z alergiami, `previewId`);
  - podwójna rezerwacja i podwójna płatność;
  - równoległe edycje pary;
  - dwie instancje uruchomione naprawdę (I-5 sprawdzone przeglądem kodu).
- **Środowisko:** lokalne, jedna instancja, poczta do lokalnego odbiornika.

## Odtworzenie

```bash
# backend z dev na 8086 i pusta baza QA (runner z repo web), odbiornik poczty, świat, testy
python3 scripts/isolation/smtp_sink.py &
python3 scripts/route_inventory.py
python3 scripts/isolation/world.py build
python3 scripts/isolation/idor.py            # --no-writes: tylko odczyt
python3 scripts/isolation/writes.py
python3 scripts/isolation/canaries.py --seconds 60
python3 scripts/isolation/world.py cleanup   # usuwa konta i klientów Keycloak
```

Wyniki (JSON) trafiają do `results/isolation/` (poza gitem).

## Poprawki (zgoda Bartosza 2026-11-22, `health-app` na `dev`, bez commita)

**Znalezisko 1** (dzień diety):
- `CreateUpdateDietDayUseCase` ustala zapisywany wiersz po stronie serwera:
  1. dzień użytkownika na tę datę;
  2. jeśli go nie ma, własny dzień o podanym `id` (przeniesienie na nową datę);
  3. w przeciwnym razie nowy dzień.
- `DietDayDatabaseAdapter` nie aktualizuje wiersza innego użytkownika (404) i z nieznanego `id` robi nowy wiersz.
- Nowe `DietDayDatabase.findOwnedById`.
- Testy:
  - 3 jednostkowe (`CreateUpdateDietDayUseCaseTest`);
  - 3 integracyjne (`DietDayOwnershipIntegrationTest`): cudzy dzień bez zmian, nieznane `id` daje nowy dzień,
    właściciel dalej zapisuje po `id`.

**Znalezisko 2** (treść wyjątku w 500): `GlobalExceptionHandler.handleGenericException` zwraca stały komunikat
„Wystąpił nieoczekiwany błąd serwera. Spróbuj ponownie później.” (tłumaczenie w `api-errors.en.json`); szczegóły
zostają w logu. Test w `GlobalExceptionHandlerTest`.

**Skutek uboczny:** ręczne ponowne nadanie planu przez admina zwraca dziś 500 (od dawna, powinno być 409). Zamiast
„already has an active subscription” admin zobaczy teraz ogólny komunikat. Proponuję osobną drobną poprawkę: 409.

**Weryfikacja:**
- kroki CI: skrypty OK, testy jednostkowe 6193 bez błędów (3 pominięte jak dotąd);
- testy integracyjne: w pierwszym przebiegu 960/964. Padły 4 przypadki `Przelewy24RecurringFlowIntegrationTest`
  (500 przy wstawianiu anulowania). Ta klasa uruchomiona osobno przechodzi 70/70 na czystym `HEAD` i z poprawkami,
  a powtórzony pełny przebieg daje 964/964. To niestabilny test zależny od kolejności albo czasu, nie skutek poprawki.
- `writes.py` na poprawionym backendzie: każdy atakujący dostaje 200 z własnym nowym dniem (inne `id`), a dzień
  ofiary zostaje bez zmian, z posiłkiem.

## Druga runda (2026-11-22, poprawiony backend)

**Środowisko:**
- osobna baza `dietdb_remediation_isolation2_20261122` z katalogiem: 39 492 produkty, 425 przepisów, 1616 wersji;
- katalog skopiowany z lokalnej bazy przez tymczasowy kontener, bez produktów użytkowników i firm, 0 adresów
  e-mail.

**Świat rozszerzony o:**
- plan klienta wygenerowany z katalogu, opublikowany i wysłany;
- generację użytkownika indywidualnego;
- wizyty kontrolne;
- zamówienie pakietu specjalisty;
- szkice przepisów i zgłoszenie przepisu do społeczności;
- sesję treningową z seriami;
- produkt firmy.

| Test | Wynik |
|---|---|
| Macierz (`idor.py`) | 193 z 293 tras z ID z kontrolą (było 180), 19 462 żądania; 0 dostępów do cudzych danych. Jedyne nowe 2xx: produkt z globalnego katalogu |
| Zapisy z poprawną treścią (`writes.py`) | 93 przypadki chronione, 2 nierozstrzygnięte (opisane niżej) |
| Prywatne przepisy specjalisty i użytkownika, 5 tras | właściciel 200; inni 403 albo 404 |
| Ankiety: trasy specjalisty i klienta | obcy specjaliści, klienci i członek firmy: 404 |
| **R1:** 12 generacji naraz, każda z inną alergią i kalorycznością | 12/12 OK; 24 dni z celem właściwej osoby; 0 z 72 przepisów z własnym alergenem |
| **R2:** `previewId` | nie jest sprawdzany (przechodzi cudzy i losowy), ale zatwierdzenie zapisuje tylko dni wywołującego, a `id` dni z treści jest ignorowane (cudzy dzień bez zmian); podwójne zatwierdzenie daje jeden dzień na datę |
| **R3:** 30 wiadomości naraz od specjalisty i klienta | 30/30, ta sama kolejność u obu, bez duplikatów |
| **R4:** podwójne kliknięcie zamówienia pakietu | 201 i 409, jedno zamówienie |
| **R5:** dwa zapisy szkicu planu z tą samą wersją | 200 i 409, bez cichego nadpisania |
| Serie treningowe z cudzym `id` w treści | dane ofiary bez zmian (szczegóły niżej) |
| Kanarki pod obciążeniem (45 s, 278 tras) | 17 587 odczytów, 0 cudzych danych |
| Język przy 2000 równoległych żądaniach | 0 w złym języku |

**Serie treningowe (znalezisko o niskiej wadze, bez wpływu na cudze dane).** ID serii z treści trafia do `INSERT`.
Cudze ID kończy się kolizją klucza i odpowiedzią 500, której treść jest już ukryta dzięki poprawce 2. Lepiej: serwer
nadaje nowe ID albo przyjmuje tylko ID serii z tej samej sesji.

**Dwie instancje na jednej bazie (I-5, `instances.py`):**
- ten sam token działa na obu instancjach;
- ta sama wiadomość wysłana do obu naraz zapisuje się raz;
- zdarzenie push przechodzi między instancjami w 14 ms;
- odebranie członkowi dostępu do firmy na instancji A daje mu 403 na instancji B od razu (autoryzacja nie ma cache'a
  w pamięci);
- limiter jest liczony osobno na każdej instancji: z 16 równoległych żądań przechodzą 4 na jednej i 8 na dwóch.

Przy 2 taskach limity na użytkownika są więc faktycznie 2× wyższe. To nie dotyczy izolacji, tylko ochrony generatora.

**Nadal nierozstrzygnięte albo niepokryte:**
- **Rezerwacje:** wymagają opublikowanego publicznego profilu specjalisty (`published`, `activated_at`, robi to admin).
- **Płatności i faktury subskrypcji, zamówienia firmy:** operatorzy płatności są lokalnie wyłączeni.
- **Integracje urządzeń; zatwierdzenie przepisu do społeczności przez admina.**
- **Usuwanie prywatnego produktu z cudzym nagłówkiem firmy:** walidacja przed dostępem. Bez nagłówka wszyscy
  dostają 403.

## Odtworzenie drugiej rundy

Jak wyżej, a dodatkowo:
- katalog wczytany do bazy QA;
- druga instancja na porcie 8087;
- `scripts/isolation/races.py --users 12`;
- `scripts/isolation/instances.py`.
