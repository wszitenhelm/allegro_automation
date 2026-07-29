# Allegro Finance — automatyzacja rozliczeń miesięcznych

## Cel biznesowy

Firma e-commerce prowadzi dwa sklepy na Allegro: **pigmejka-pl** i **decor4-pl**
(każdy to osobne konto Allegro / osobna aplikacja `CLIENT_ID`). Co miesiąc
księgowa musi rozbić **zbiorcze przelewy bankowe** od Allegro (na koncie mBank,
w PLN) na poszczególne wpłaty kupujących, żeby móc to zaksięgować. Ręcznie jest
to żmudne, bo jeden przelew bankowy = suma wielu pojedynczych zamówień minus
prowizje i zwroty.

Ten projekt to automatyzacja tego rozbicia: wgrywasz wyciąg PDF z mBank +
wybierasz miesiąc, system loguje się do Allegro API (OAuth) dla każdego
sklepu i produkuje tabelę/CSV: który przelew bankowy odpowiada jakim
zamówieniom, z ilu wpłat się składał, ile poszło na prowizję Allegro, ile na
zwroty.

**Kolejny planowany krok (jeszcze niezrobiony):** podpięcie wyniku do
Subiekt Nexo, żeby księgowanie też było zautomatyzowane. Na razie system
tylko generuje gotowe zestawienie do ręcznego/pół-automatycznego księgowania.

## Repo i środowisko

- GitHub: `github.com/wszitenhelm/allegro_automation` (branch `main`), właściciel
  konta: wszitenhelm.
- Lokalnie: `/Users/wikusia/Desktop/allegro_automation`.
- Wirtualne środowisko Python: `~/allegro_venv` (NIE `.venv` w folderze
  projektu — to jest osobny, już istniejący venv używany do wszystkiego).
  Aktywacja: `source ~/allegro_venv/bin/activate`.
- `requirements.txt`: requests, python-dotenv, anthropic, streamlit, pandas,
  watchdog.
- Sekrety w `.env` (gitignored, NIGDY nie commitować):
  `ALLEGRO_PIGMEJKA_CLIENT_ID/SECRET`, `ALLEGRO_DECOR_CLIENT_ID/SECRET`,
  opcjonalnie `ANTHROPIC_API_KEY`, `APP_PASSWORD`.
- `.env.example` i `.streamlit/secrets.toml.example` dokumentują wymagane
  zmienne bez prawdziwych wartości.

## Struktura plików

- `allegro_listopad.py` — **pierwszy, historyczny, jednorazowy skrypt**
  (tylko pigmejka-pl, sztywny zakres dat na listopad). Zostawiony jako
  działający punkt odniesienia, nie jest rozwijany.
- `allegro_rozliczenie.py` — **punkt wejścia CLI** dla ogólnego,
  wieloskepowego, wielowalutowego rozliczenia. Tylko: parsowanie argumentów,
  orkiestracja (pętla po sklepach), eksport CSV, wywołanie podsumowania LLM.
  Cała logika biznesowa jest w modułach poniżej (ważne dla przyszłego
  frontendu — te moduły da się zaimportować bez odpalania całego CLI).
- `config.py` — wczytywanie sklepów z `.env` (`wczytaj_sklepy()`), stałe
  (`TOLERANCJA_DNI=1`), definicja kolumn wyniku (`KOLUMNY_WYNIKU`,
  `NAZWY_KOLUMN_WYNIKU` — jedno miejsce prawdy używane i przez CSV, i przez
  frontend), `zakres_dat(rok, mies)`.
- `pdf_parser.py` — parsowanie wyciągu mBank przez `pdftotext -layout` +
  regex. Zwraca listę `{"data": "YYYY-MM-DD", "kwota": float, "uzyta": False}`
  dla każdego przelewu Allegro Finance znalezionego w PDF (szuka linii z UUID
  + słowem "allegro" w kontekście, cofa się do linii z datą). Rzuca
  `RuntimeError` (nie `sys.exit`) — celowo, żeby dało się to wywołać z
  frontendu bez zabijania procesu.
- `allegro_api.py` — cienki klient Allegro API:
  - `zainicjuj_device_flow` / `czekaj_na_token` / `autoryzuj` — OAuth
    device-code flow (rozbite na 2 funkcje + funkcję złożoną, żeby CLI i
    frontend mogły to różnie obsłużyć — CLI robi `input()`, frontend pokazuje
    link do skopiowania).
  - `pobierz_wszystkie` — paginacja.
  - `pobierz_dane_faktury` — sprawdza czy kupujący zażądał faktury (przez
    `/order/checkout-forms?payment.id=...`), zwraca dane firmy/NIP. Podnosi
    `BrakUprawnienDoZamowien` przy 403 (brak scope "Order management" w
    Allegro Developer Portal).
- `rozliczenie.py` — **serce systemu**, `rozlicz_sklep()`. Patrz sekcja
  "Jak działa dopasowanie" niżej.
- `nbp.py` — kursy NBP (`kurs_nbp(waluta, dzien)`), do walidacji dopasowań
  w obcej walucie. Publiczne, darmowe API `api.nbp.pl`, tabela A (kursy
  średnie), z fallbackiem na wcześniejszy dzień roboczy (weekendy/święta).
- `llm_summary.py` — opcjonalne 2-3 zdaniowe podsumowanie tekstowe
  (Anthropic API, model `claude-haiku-4-5-20251001`). Dostaje WYŁĄCZNIE
  zagregowane liczby (per sklep/operator: liczba przelewów, sumy) — nigdy
  treść wyciągu ani dane osobowe kupujących. Brak klucza = krok pomijany,
  reszta programu działa normalnie.
- `app.py` — frontend Streamlit dla osoby nietechnicznej (patrz sekcja
  "Frontend" niżej).
- `logo.png` / `logo.avif` — logo firmy (szaro-pomarańczowe), użyte w
  nagłówku appki.
- `.streamlit/config.toml` — motyw kolorystyczny (szaro-pomarańczowy,
  dopasowany do logo).

## Jak działa dopasowanie (najważniejsza, najbardziej dopracowana część)

### Krok 1: parsowanie wyciągu
`pdf_parser.py` wyciąga z PDF listę `(data, kwota)` dla każdego przelewu
Allegro Finance widocznego na wyciągu mBank. To jest **jedyne źródło prawdy**
o tym, co faktycznie wpłynęło na konto bankowe.

### Krok 2: pobranie operacji z Allegro API
Dla każdego sklepu, jedno zapytanie do `/payments/payment-operations`
(bez filtra `currency` — to było źródłem bugów, patrz niżej), potem
filtrowane lokalnie po `group` (INCOME=wpłaty kupujących, REFUND=zwroty) i
`type` (PAYOUT=wypłata bankowa).

### Krok 3: dopasowanie w DWÓCH przebiegach
1. **PLN** — dopasowanie po **dokładnej kwocie + dacie** (z tolerancją ±1
   dzień, bo `occurredAt` z API jest w UTC a wyciąg/panel Allegro pokazują
   czas lokalny Europe/Warsaw — blisko północy te daty mogą się różnić o
   dzień). To jest jednoznaczne i pewne.
2. **Waluty obce (EUR/CZK/HUF)** — niektóre operatory (np. "PayU - Allegro
   Finance" dla sprzedaży na allegro.cz/sk/hu) prowadzą osobny portfel w
   obcej walucie, wypłacany na to samo (złotówkowe) konto po przewalutowaniu.
   **Allegro NIE udostępnia w API ani kursu, ani przeliczonej kwoty PLN**
   (sprawdzone wyczerpująco w pełnej specyfikacji OpenAPI) — to jest twardy
   fakt, nie luka w wyszukiwaniu. Dopasowanie odbywa się więc **tylko po
   dacie**, wyłącznie wśród wpisów z wyciągu, które NIE zostały już
   wykorzystane w przebiegu 1 (PLN ma pierwszeństwo, bo jest jednoznaczne —
   to mocno zawęża pulę dla dopasowania po samej dacie). Dodatkowo
   zweryfikowane kursem średnim NBP z danego dnia: oczekiwana kwota PLN =
   kwota_oryginalna × kurs_NBP, musi się zgadzać z wpisem z wyciągu z
   tolerancją **±10%** (bank/Allegro mogą użyć nieco innego kursu niż
   średni NBP).

Każdy wpis z wyciągu ma flagę `"uzyta"` — współdzielona (ten sam obiekt w
pamięci) między wszystkimi sklepami i operatorami, żeby jeden wpis z wyciągu
nie został dopasowany dwa razy.

### Krok 4: liczenie "Pobranie opłat Allegro"
**Nie przez osobne zapytanie do API** (próbowaliśmy — `billing-entries`
rozlicza prowizję z opóźnieniem, które nie pokrywa się z oknem między
przelewami, więc dopasowanie per-przelew wychodziło losowo błędne). Zamiast
tego liczone jako **reszta z równania**:

```
Suma Zamówień − Kwota Przelewu − Zwroty = Pobranie opłat Allegro
```

Dla wierszy w walucie obcej te trzy liczby **zostają w oryginalnej walucie**
(nie są przeliczane na PLN) — tak zdecydowała użytkowniczka, bo i tak nie
znamy dokładnego kursu użytego przez Allegro. Kolumna „Kwota Przelewu" dla
takich wierszy pokazuje OBIE wartości: `185.30 zł (44.17 EUR)` — PLN z
wyciągu (do porównania z bankiem) + oryginalna kwota (do matematyki).

### Dane faktury
Dla każdego kupującego sprawdzane jest (jeśli jest dostęp — patrz
`BrakUprawnienDoZamowien` wyżej) czy zażądał faktury; jeśli tak, do nazwy
dopisywane są dane firmy + NIP (albo dane osoby). To dodatkowe zapytanie
API per wpłatę (kosztowne czasowo przy dużej liczbie transakcji, ale
użytkowniczka zaakceptowała ten koszt).

### Data w wyniku
Zawsze **data z wyciągu** (`wyciag_wpis["data"]`), nie `occurredAt` z API —
to jest data którą księgowa widzi na wyciągu bankowym i po której będzie
szukać tego przelewu.

### Kolumny wyniku (CSV i frontend, definicja w `config.py`)
Sklep | Data | Operator | Kwota Przelewu | Waluta | Liczba kupujących |
Suma Zamówień | Pobranie opłat Allegro | Zwroty

Operator w PEŁNEJ nazwie (np. "Allegro Finance", "Allegro Finance — PayU"),
nie skrótem (AF, AF_PAYU). Wynik jest sortowany chronologicznie po dacie
z wyciągu.

## Rzeczy, które NIE działają / zostały świadomie odrzucone

- **Status OK/ROZBIEŻNOŚĆ i "sieroty"** (kwoty z wyciągu bez dopasowania) —
  była wcześniej cała warstwa walidacji z kolorowanym statusem, usunięta na
  życzenie użytkowniczki. Dziś system po prostu pokazuje surowe liczby, bez
  automatycznego werdyktu.
- **Kolumna "Inne waluty (poza sumą)"** — pierwsze podejście do walut obcych
  (wykryj i pomiń z ostrzeżeniem) zostało odrzucone i zastąpione pełną
  integracją (przebieg 2 opisany wyżej).
- **`billing-entries` do liczenia prowizji** — porzucone, patrz Krok 4 wyżej.

## Frontend (Streamlit, `app.py`)

- Nagłówek: logo + hasło "mniej czasu na księgowanie = więcej czasu z
  rodziną", motyw szaro-pomarańczowy, `layout="wide"`.
- Upload wyciągu PDF, wybór roku/miesiąca, multiselect sklepów (można
  rozliczyć tylko wybrane — reszta po prostu nie pojawi się w wyniku, bez
  fałszywych ostrzeżeń).
- OAuth: dla każdego sklepu pokazuje link **jako tekst do skopiowania**
  (`st.code`, ma przycisk kopiowania) — bo różne sklepy bywają zalogowane w
  różnych przeglądarkach (np. pigmejka w Safari, decor4 w Chrome), więc
  przycisk otwierający w bieżącej przeglądarce nie zawsze wystarcza.
  Ukryta wbudowana ikonka Streamlita "aplikacja się wykonuje" (CSS na
  `data-testid=stStatusWidget`).
- Tabela wyników: klikalne wiersze — po kliknięciu pokazuje się lista
  kupujących i zwrotów dla tego okna (w oryginalnej walucie).
- Przycisk pobrania CSV.
- Opcjonalne podsumowanie LLM na dole.
- **Uwaga:** appka lokalnie uruchamiana jest na PRAWDZIWEJ maszynie
  użytkowniczki (nie w sandboxie asystenta) — `streamlit run app.py` na
  `localhost:8501`, więc testy "uruchom to" i realne testy przez
  użytkowniczkę w przeglądarce to ten sam proces.
- Deploy docelowy: Streamlit Community Cloud (jeszcze nie zrobiony/potwierdzony
  jako aktywny — sprawdzić przy następnej sesji). `packages.txt` zawiera
  `poppler-utils` (dla `pdftotext` na serwerze).

## Historia sesji / jak pracujemy razem

- Użytkowniczka testuje dużo przez rzeczywiste dane (prawdziwe wyciągi PDF:
  `wyciag_listopad.pdf`, `wyciag_pazdziernik.pdf` — oba w repo, ale
  gitignored jako `*.pdf`, bo to wrażliwe dane bankowe).
  Realne konto: pigmejka-pl ma dane w listopadzie 2025 (217 wpłat PLN + kilka
  operacji EUR/CZK/HUF przez operator "Allegro Finance — PayU" — sprzedaż na
  allegro.cz/sk/hu).
- Asystent (ja) testuje zmiany przez **testy syntetyczne z mockami**
  (`unittest.mock` na `requests.get/post`, `subprocess.run`) w
  `/private/tmp/.../scratchpad/` — nigdy nie commitowane, budowane od nowa
  przy każdej sesji debugowania. To ważne bo nie mam dostępu do prawdziwego
  OAuth Allegro (wymaga loginu użytkowniczki w przeglądarce).
- **Bardzo ważna zasada: NIGDY nie dodawać `Co-Authored-By: Claude` do
  commitów** — użytkowniczka to explicite zabroniła.
- Workflow commitów: robię zmianę → testuję syntetycznie → pytam czy
  wdrożyć lokalnie / pushować → dopiero po potwierdzeniu przez
  użytkowniczkę (czasem testuje sama w swojej przeglądarce z prawdziwym
  Allegro OAuth) → commit + push.
- Wiele bugów w tej sesji wynikało z **niepełnej wiedzy o rzeczywistym
  kształcie danych z Allegro API** (np. że occurredAt jest w UTC a wyciąg w
  czasie lokalnym; że prowizja rozlicza się z opóźnieniem; że sprzedaż
  zagraniczna ma zupełnie inny model danych z osobnymi operacjami
  CONTRIBUTION/DEDUCTION_CHARGE/PAYOUT per waluta) — każdy taki przypadek
  był rozwiązywany przez dodanie tymczasowej diagnostyki (`print` surowego
  JSON), poproszenie użytkowniczki o uruchomienie na prawdziwych danych, i
  dopiero na tej podstawie projektowanie poprawki. To sprawdzony wzorzec
  pracy w tym projekcie — warto go powtarzać zamiast zgadywać kształt API.

## Otwarte tematy na przyszłość

1. Podpięcie wyniku do Subiekt Nexo (księgowanie) — jeszcze nie zaczęte.
2. Deploy na Streamlit Community Cloud — przygotowane (`packages.txt`,
   `.streamlit/secrets.toml.example`) ale trzeba sprawdzić czy faktycznie
   wdrożone i działa.
3. Warto na bieżąco potwierdzać z użytkowniczką czy sortowanie/format
   nowych kolumn jej odpowiada — dużo iteracji w tej sesji polegało na
   drobnych korektach UX tabeli wyników.
