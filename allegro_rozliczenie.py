"""
Allegro Finance - rozbicie przelewów bankowych na kupujących.
Obsługuje wiele sklepów (kont Allegro) rozliczanych z tego samego wyciągu.

Użycie:
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11

Wymagania:
  pip install -r requirements.txt
  brew install poppler   (dla pdftotext)

Sklepy do rozliczenia konfiguruje się w .env (patrz .env.example):
  ALLEGRO_PIGMEJKA_CLIENT_ID / ALLEGRO_PIGMEJKA_CLIENT_SECRET
  ALLEGRO_DECOR_CLIENT_ID / ALLEGRO_DECOR_CLIENT_SECRET
Skrypt loguje się (OAuth) osobno do każdego skonfigurowanego sklepu i
dopasowuje jego wypłaty do tej samej wspólnej puli przelewów z wyciągu —
jeden wpis z wyciągu może zostać przypisany tylko do jednego sklepu.
Sklep bez ustawionych zmiennych w .env jest po prostu pomijany.

Logika rozliczenia jest w osobnych modułach (config.py, pdf_parser.py,
allegro_api.py, rozliczenie.py) — ten plik to tylko punkt wejścia CLI
(argumenty, orkiestracja, eksport CSV).
"""
import csv
import re
import sys

from config import wczytaj_sklepy, zakres_dat, KOLUMNY_WYNIKU, NAZWY_KOLUMN_WYNIKU
from pdf_parser import parsuj_pdf_mbank
from allegro_api import autoryzuj
from rozliczenie import rozlicz_sklep


def ustal_parametry():
    """Parsuje argumenty CLI i zwraca (date_od, date_do, miesiac_od, wyciag_przelewy)."""
    args = sys.argv[1:]
    pdf_plik  = next((a for a in args if a.lower().endswith(".pdf")), None)
    miesiac_s = next((a for a in args if re.match(r'^\d{4}-\d{2}$', a)), None)

    if not pdf_plik or not miesiac_s:
        print(__doc__)
        sys.exit("Podaj plik PDF i miesiąc, np.:  python3 allegro_rozliczenie.py wyciag.pdf 2025-11")

    try:
        wyciag_przelewy = parsuj_pdf_mbank(pdf_plik)
    except RuntimeError as e:
        sys.exit(str(e))

    rok, mies = int(miesiac_s[:4]), int(miesiac_s[5:7])
    date_od, date_do, miesiac_od = zakres_dat(rok, mies)
    return date_od, date_do, miesiac_od, wyciag_przelewy


def main():
    sklepy = wczytaj_sklepy()
    if not sklepy:
        sys.exit(
            "Brak skonfigurowanych sklepów w .env. Ustaw co najmniej:\n"
            "  ALLEGRO_PIGMEJKA_CLIENT_ID / ALLEGRO_PIGMEJKA_CLIENT_SECRET\n"
            "(opcjonalnie też ALLEGRO_DECOR_CLIENT_ID / ALLEGRO_DECOR_CLIENT_SECRET)"
        )

    date_od, date_do, miesiac_od, wyciag_przelewy = ustal_parametry()

    wiersze_csv = []

    for sklep in sklepy:
        auth_headers = autoryzuj(sklep["nazwa"], sklep["client_id"], sklep["client_secret"])
        wiersze, _stats, _operacje = rozlicz_sklep(
            sklep["nazwa"], auth_headers, date_od, date_do, miesiac_od, wyciag_przelewy
        )
        wiersze_csv.extend(wiersze)

    # sortowanie chronologiczne wg daty z wyciągu — bez tego wiersze są w
    # kolejności w jakiej przetwarzane były sklepy/operatory, nie w kolejności
    # jak na wyciągu bankowym
    wiersze_csv.sort(key=lambda w: w["data"])

    # eksport CSV
    print("\n" + "=" * 60)
    print("EKSPORT ROZLICZENIA")
    print("=" * 60)

    plik_csv = f"rozliczenie_{miesiac_od[:7]}.csv"
    with open(plik_csv, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([NAZWY_KOLUMN_WYNIKU[k] for k in KOLUMNY_WYNIKU])
        writer = csv.DictWriter(f, fieldnames=KOLUMNY_WYNIKU, extrasaction="ignore")
        writer.writerows(wiersze_csv)
    print(f"Zapisano: {plik_csv}  ({len(wiersze_csv)} wierszy)")


if __name__ == "__main__":
    main()
