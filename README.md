# allegro_automation

Automatyzacja rozliczeń Allegro Finance (AF / PayU / Przelewy24) — rozbicie
zbiorczych przelewów bankowych na kupujących, dla sklepów decor4-pl i pigmejka-pl.

## Skrypty

- `allegro_listopad.py` — pobiera wpłaty kupujących i przelewy bankowe z Allegro
  Payments API dla stałego zakresu dat i grupuje wpłaty per operator płatności.
- `allegro_rozliczenie.py` — jak wyżej, ale ogólne dla dowolnego miesiąca i dla
  **wielu sklepów jednocześnie** (pigmejka-pl, decor4-pl — każdy to osobne konto/
  `CLIENT_ID` w Allegro, oba wpływają na ten sam wyciąg mBank). Parsuje wyciąg
  PDF (`pdftotext`), znajduje w nim przelewy Allegro Finance po **dacie i
  kwocie**, loguje się (OAuth) osobno do każdego skonfigurowanego sklepu i
  dopasowuje jego wypłaty (PAYOUT) do tej samej wspólnej puli przelewów z
  wyciągu — jeden wpis z wyciągu może trafić tylko do jednego sklepu.

  Dla każdego przelewu liczy `Pobranie opłat Allegro` jako resztę z równania
  `Σ zamówień − kwota przelewu − zwroty = Pobranie opłat Allegro` (rzeczywista
  prowizja Allegro rozlicza się z opóźnieniem, które nie pokrywa się z oknem
  między przelewami, więc nie da się jej wiarygodnie dopasować per przelew z
  osobnego zapytania do API). Data każdego przelewu w wyniku to data **z
  wyciągu bankowego**, nie z Allegro API (mogą się różnić o dzień przez
  strefę czasową/opóźnienie księgowe banku). Dla kupujących, którzy zażądali
  faktury, do nazwy dopisywane są dane firmy/NIP (osobne zapytanie do
  `/order/checkout-forms` per wpłata — wymaga włączonego w Allegro Developer
  Portal dostępu do odczytu zamówień; bez tego uprawnienia ten krok jest po
  prostu pomijany).

  ```
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11
  ```

  Wynik: konsola (szczegóły per sklep/przelew) + plik `rozliczenie_YYYY-MM.csv`
  (jeden wiersz na przelew bankowy, kolumny: Sklep, Data, Operator, Kwota
  Przelewu, Waluta, Liczba kupujących, Suma Zamówień, Pobranie opłat Allegro,
  Zwroty). Jeśli w `.env` jest ustawiony `ANTHROPIC_API_KEY`, dodatkowo
  generowane jest 2-3 zdaniowe podsumowanie tekstowe (na podstawie wyłącznie
  zagregowanych liczb, patrz niżej) — bez klucza ten krok jest po prostu
  pomijany.

  **Sprzedaż zagraniczna (EUR/CZK/HUF):** niektóre operatory (np. PayU —
  Allegro Finance dla allegro.cz/sk/hu) prowadzą osobny portfel w obcej
  walucie, wypłacany na to samo (złotówkowe) konto bankowe po przewalutowaniu.
  Allegro nie udostępnia w API ani kursu, ani przeliczonej kwoty PLN, więc
  takie wypłaty są dopasowywane do wyciągu **po dacie** (nie po kwocie) wśród
  wpisów jeszcze niewykorzystanych przez dopasowania PLN (te mają
  pierwszeństwo, bo są jednoznaczne), a dopasowanie jest dodatkowo
  weryfikowane kursem średnim NBP z danego dnia (`nbp.py`, publiczne API) —
  oczekiwana kwota PLN musi się zgadzać z wpisem z wyciągu z tolerancją ±10%.
  W wyniku taki wiersz ma `Waluta` = EUR/CZK/HUF, kolumna `Kwota Przelewu`
  pokazuje obie wartości (np. `185.30 zł (44.17 EUR)`), a `Suma Zamówień` /
  `Pobranie opłat Allegro` / `Zwroty` zostają w oryginalnej walucie (bez
  przeliczania kursem).

  Logika jest rozbita na moduły, `allegro_rozliczenie.py` to tylko punkt
  wejścia CLI (argumenty, orkiestracja, eksport CSV):
  - `config.py` — sklepy z `.env`, stałe, zakres dat
  - `pdf_parser.py` — parsowanie wyciągu PDF (bez side-effectów sieciowych)
  - `allegro_api.py` — klient Allegro API (OAuth, pobieranie z paginacją)
  - `rozliczenie.py` — dopasowanie wypłat do wyciągu + walidacja, per sklep
  - `nbp.py` — kursy NBP, do weryfikacji dopasowania wypłat w obcej walucie
  - `llm_summary.py` — opcjonalne podsumowanie tekstowe (Anthropic API)

  Ten podział ma znaczenie przy podpinaniu frontendu: `pdf_parser.py` i
  `rozliczenie.py` da się wtedy zaimportować i wywołać wprost, bez
  uruchamiania całego skryptu CLI (który dziś czeka na `input()` i sam
  odpala OAuth przy imporcie).

- `app.py` — frontend (Streamlit) dla osoby nietechnicznej: wgraj wyciąg PDF,
  wybierz rok/miesiąc, kliknij "Rozlicz". Loguje się przez link (bez
  terminala) osobno do każdego skonfigurowanego sklepu, pokazuje tabelę
  wyników (kolorowany status OK/ROZBIEZNOSC) i przycisk pobrania CSV.
  Opcjonalnie chroniona hasłem (`APP_PASSWORD` w sekretach) — patrz niżej.

## Frontend (Streamlit)

Lokalnie:

```bash
pip install -r requirements.txt
brew install poppler
cp .env.example .env   # jak w sekcji Setup niżej
streamlit run app.py
```

### Wdrożenie na Streamlit Community Cloud

1. Repo jest już na GitHubie — nic więcej nie trzeba pushować.
2. Wejdź na [share.streamlit.io](https://share.streamlit.io) → "New app" →
   wybierz to repo, branch `main`, plik główny `app.py`.
3. W ustawieniach aplikacji → "Secrets" wklej zawartość
   `.streamlit/secrets.toml.example` uzupełnioną prawdziwymi danymi (Client
   ID/Secret per sklep, opcjonalnie `ANTHROPIC_API_KEY`, i `APP_PASSWORD` —
   proste hasło, żeby nikt obcy z linkiem nie odpalił rozliczenia na Waszym
   koncie Allegro).
4. `packages.txt` (poppler-utils) sprawia, że `pdftotext` jest dostępny na
   serwerze Streamlit — nic dodatkowo nie trzeba instalować.
5. Dostajesz publiczny link (`*.streamlit.app`). Każdy kolejny `git push` do
   `main` automatycznie redeployuje aplikację.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install poppler   # dla pdftotext (potrzebne tylko dla allegro_rozliczenie.py)
cp .env.example .env   # i uzupełnij dane co najmniej jednego sklepu (patrz niżej)
```

Dane logowania do Allegro API znajdziesz w [Allegro Developer Portal](https://apps.developer.allegro.pl/)
— osobno dla każdego sklepu (`ALLEGRO_PIGMEJKA_CLIENT_ID`/`_SECRET`,
`ALLEGRO_DECOR_CLIENT_ID`/`_SECRET` w `.env`). Sklep bez ustawionych zmiennych
jest po prostu pomijany, więc `allegro_rozliczenie.py` działa też z jednym
sklepem skonfigurowanym. Autoryzacja odbywa się przez OAuth device flow —
skrypt loguje się do każdego skonfigurowanego sklepu po kolei, za każdym razem
otwórz podany link w przeglądarce i zatwierdź dostęp.

## Bezpieczeństwo danych

- `.env` (sekrety), `*.pdf` (rzeczywiste wyciągi bankowe) i `rozliczenie_*.csv`
  (wygenerowane rozliczenia) są w `.gitignore` — nigdy nie trafiają do repozytorium.
- Wyciąg bankowy jest przetwarzany wyłącznie lokalnie. Do LLM (opcjonalne
  podsumowanie w `allegro_rozliczenie.py`) trafiają WYŁĄCZNIE zagregowane
  liczby per operator (ile przelewów, jakie sumy) — nigdy treść wyciągu,
  numer konta ani dane osobowe kupujących.
