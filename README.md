# allegro_automation

Automatyzacja rozliczeń Allegro Finance (AF / PayU / Przelewy24) — rozbicie
zbiorczych przelewów bankowych na kupujących, dla sklepów decor4-pl i pigmejka-pl.

## Skrypty

- `allegro_listopad.py` — pobiera wpłaty kupujących i przelewy bankowe z Allegro
  Payments API dla stałego zakresu dat i grupuje wpłaty per operator płatności.
- `allegro_rozliczenie.py` — jak wyżej, ale ogólne dla dowolnego miesiąca: parsuje
  wyciąg PDF z mBank (`pdftotext`), znajduje w nim przelewy Allegro Finance po
  **dacie i kwocie**, dopasowuje je do wypłat (PAYOUT) z Allegro API i waliduje
  każdy przelew: `suma wpłat − suma zwrotów − rzeczywista prowizja Allegro
  (billing API) = kwota przelewu` (z tolerancją 0,01 PLN). Kwoty z wyciągu bez
  odpowiadającej wypłaty w API są zgłaszane jako do ręcznego sprawdzenia.

  ```
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11
  ```

  Wynik: konsola (szczegóły per przelew) + plik `rozliczenie_YYYY-MM.csv`
  (jeden wiersz na przelew bankowy, kolumny: data, operator, kwota_przelewu,
  l_kupujacych, oplaty, zwroty, status). Jeśli w `.env` jest ustawiony
  `ANTHROPIC_API_KEY`, dodatkowo generowane jest 2-3 zdaniowe podsumowanie
  tekstowe (na podstawie wyłącznie zagregowanych liczb, patrz niżej) — bez
  klucza ten krok jest po prostu pomijany.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install poppler   # dla pdftotext (potrzebne tylko dla allegro_rozliczenie.py)
cp .env.example .env   # i uzupełnij ALLEGRO_CLIENT_ID / ALLEGRO_CLIENT_SECRET
```

Dane logowania do Allegro API znajdziesz w [Allegro Developer Portal](https://apps.developer.allegro.pl/).
Autoryzacja odbywa się przez OAuth device flow — po uruchomieniu skryptu
otwórz podany link w przeglądarce i zatwierdź dostęp.

## Bezpieczeństwo danych

- `.env` (sekrety), `*.pdf` (rzeczywiste wyciągi bankowe) i `rozliczenie_*.csv`
  (wygenerowane rozliczenia) są w `.gitignore` — nigdy nie trafiają do repozytorium.
- Wyciąg bankowy jest przetwarzany wyłącznie lokalnie. Do LLM (opcjonalne
  podsumowanie w `allegro_rozliczenie.py`) trafiają WYŁĄCZNIE zagregowane
  liczby per operator (ile przelewów, jakie sumy) — nigdy treść wyciągu,
  numer konta ani dane osobowe kupujących.
