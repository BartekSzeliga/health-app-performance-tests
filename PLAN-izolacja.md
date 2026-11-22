# Plan: izolacja danych (2026-11-22)

Cel: sprawdzić, czy użytkownik, specjalista albo firma może zobaczyć lub zmienić cudze dane. Wydzielone z `PLAN.md`
(sekcja 8) po zamknięciu etapu wydajności. Zasady jak dotąd: **testy mierzą stan obecny**, poprawki trafiają do
planu napraw i wchodzą do kodu dopiero po zgodzie Bartosza, przez `dev`.

## Środowisko

- **Lokalnie, bez AWS i bez prod.** Backend z bieżącego `dev` jako osobny proces na porcie 8086, z własną, pustą bazą
  `dietdb_remediation_isolation_<data>` na lokalnym Postgresie (Liquibase zakłada schemat). Lokalny Keycloak, realm
  `health-realm`, klient QA z password grant. Stack Bartosza na 8081 i jego baza `dietdb` zostają nietknięte.
- **Konta:** wyłącznie `@perf.diecik.invalid`, zakładane i usuwane przez API admina Keycloaka. Hasła tylko w
  prywatnym pliku poza repo.
- **Dane:** tylko przez API aplikacji, tak jak w aplikacji. Jedyny wyjątek: doręczenie tokenu zaproszenia klienta
  (`SeedDatabase`), bo poczta jest wyłączona.
- Po każdej sesji: usunięte konta, baza, proces backendu.

## Aktorzy („świat” testu)

| Aktor | Rola |
|---|---|
| Specjalista A, specjalista B | niezależne gabinety, każdy z klientem połączonym kontem i klientem tylko z e-mailem |
| Klient A1, klient B1 | konta klientów A i B |
| Firma A, firma B | organizacje z adminem, członkiem zespołu, klientem i aktywną aplikacją firmy |
| Użytkownik X, użytkownik Y | konta indywidualne z własnymi danymi |
| Para | dwie osoby w parze u specjalisty A |

Każdy właściciel tworzy przez API zasoby z każdej dziedziny: dane zdrowotne (pomiary, waga, skład ciała, zgody),
dni diety, posiłki, własne produkty i przepisy, listy zakupów, ulubione, trening, czat, ankiety, notatki i zadania,
rezerwacje i oferty, plany i ich publikacje, dokumenty i PDF, ustawienia firmy.

## Etapy

- [x] **0. Inwentarz tras** z kodu: `scripts/route_inventory.py` (649 mapowań, 413 z ID w ścieżce, w tym 133
  administracyjnych).
- [x] **1. Macierz autoryzacji (I-3, IDOR)** (2026-11-22: 180/293 tras, 2 znaleziska; raport w `reports/2026-11-22-izolacja-danych.md`). Dla każdej trasy z ID:
  - kontrola pozytywna: właściciel dostaje 2xx (inaczej wynik jest nierozstrzygnięty, nie „bezpieczny”);
  - atak: ten sam adres jako użytkownik X, specjalista B, firma B i klient A1 tam, gdzie nie powinien mieć dostępu;
  - wynik: 401/403/404 to OK, 2xx to znalezisko, 400/422/5xx to „nierozstrzygnięte” i do ręcznej weryfikacji;
  - przy zapisach (PUT/POST/DELETE) po ataku właściciel sprawdza, czy jego dane się nie zmieniły;
  - trasy `/api/admin/**` jako zwykły użytkownik i specjalista: zawsze 403;
  - ID także w query, body i nagłówku `X-Organization-Id` (np. `clientId`, organizacja innej firmy).

  Priorytet: dane zdrowotne, czat, PDF i eksporty, zdjęcia i pliki (czy adresy są publiczne lub do zgadnięcia),
  faktury, organizacje i aplikacja firmy, pary.
- [x] **2. Cache i nagłówki (I-4)** (zmiana konta: przegląd kodu, bez testu w przeglądarce): każda odpowiedź z danymi użytkownika ma `Cache-Control: no-store` albo
  `private`. `/api` nie idzie przez CloudFront (sprawdzone 2026-11-22: `api.diecik.com` wskazuje bezpośrednio na ALB).
  Zmiana konta w tej samej przeglądarce i aplikacji nie pokazuje danych poprzedniego konta (web: Playwright,
  mobile: przegląd czyszczenia cache przy wylogowaniu).
- [x] **3. Wyścigi (I-2)** (2026-11-22, `races.py`: generacje z alergiami, `previewId`, czat, zamówienie, plan; bez rezerwacji i par): JUnit z wirtualnymi wątkami i barierą startową. Zakres:
  - generacje z różnymi alergiami naraz: każdy dostaje plan ze swoimi alergiami;
  - `previewId`: commit dwa razy i commit cudzego;
  - para: równoległe edycje i usunięcia;
  - lost update planu klienta;
  - kolejność i przypisanie wiadomości czatu;
  - podwójna rezerwacja terminu i podwójne kliknięcie w płatności;
  - język i region nie przechodzą między równoległymi żądaniami.
- [x] **4. Kanarki pod obciążeniem (I-1)** (bez zapytań spójności w bazie): unikalny znacznik w danych każdego konta, sprawdzany w każdej odpowiedzi
  podczas ruchu Gatlinga; po teście zapytania spójności w bazie (właściciel dni, posiłków, list zgadza się z twórcą).
- [x] **5. Tożsamość przy dwóch instancjach (I-5)** (2026-11-22, `instances.py`, 2 procesy na jednej bazie): dwa procesy backendu na jednej bazie. Tokeny i sesje działają
  niezależnie od instancji, limiter liczy po `sub`.
- [x] **6. Raport** [`reports/2026-11-22-izolacja-danych.md`](reports/2026-11-22-izolacja-danych.md); `reports/<data>-izolacja-danych.md`: znaleziska z wagą, odtworzeniem i propozycją poprawki;
  pokrycie tras (ile rozstrzygniętych, ile nie i dlaczego).

Znalezisko krytyczne (cudze dane zdrowotne, czat, cudza firma) zgłaszam od razu, bez czekania na koniec etapu.
