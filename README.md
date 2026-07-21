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
  wyciągu — jeden wpis z wyciągu może trafić tylko do jednego sklepu. Dla
  każdego przelewu waliduje: `suma wpłat − suma zwrotów − rzeczywista prowizja
  Allegro (billing API) = kwota przelewu` (z tolerancją 0,00 PLN). Kwoty z
  wyciągu bez odpowiadającej wypłaty w ŻADNYM sklepie są zgłaszane jako do
  ręcznego sprawdzenia.

  ```
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11
  ```

  Wynik: konsola (szczegóły per sklep/przelew) + plik `rozliczenie_YYYY-MM.csv`
  (jeden wiersz na przelew bankowy, kolumny: sklep, data, operator,
  kwota_przelewu, l_kupujacych, oplaty, zwroty, status). Jeśli w `.env` jest
  ustawiony `ANTHROPIC_API_KEY`, dodatkowo generowane jest 2-3 zdaniowe
  podsumowanie tekstowe (na podstawie wyłącznie zagregowanych liczb, patrz
  niżej) — bez klucza ten krok jest po prostu pomijany.

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
