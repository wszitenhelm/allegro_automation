# allegro_automation

Automatyzacja rozliczeń Allegro Finance (AF / PayU / Przelewy24) — rozbicie
zbiorczych przelewów bankowych na kupujących, dla sklepów decor4-pl i pigmejka-pl.

## Skrypty

- `allegro_listopad.py` — pobiera wpłaty kupujących i przelewy bankowe z Allegro
  Payments API dla stałego zakresu dat i grupuje wpłaty per operator płatności.
- `allegro_rozliczenie.py` — jak wyżej, ale ogólne dla dowolnego miesiąca: parsuje
  wyciąg PDF z mBank (`pdftotext`), znajduje w nim przelewy Allegro Finance po
  kwotach, i dopasowuje je do wypłat z Allegro API.

  ```
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11
  ```

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

- `.env` (sekrety) i `*.pdf` (rzeczywiste wyciągi bankowe) są w `.gitignore` —
  nigdy nie trafiają do repozytorium.
- Wyciąg bankowy jest przetwarzany wyłącznie lokalnie. Żadne dane wrażliwe
  (numer konta, dane kupujących) nie są wysyłane do zewnętrznych usług/LLM.
