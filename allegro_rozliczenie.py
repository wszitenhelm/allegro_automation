"""
Allegro Finance - rozbicie przelewów bankowych na kupujących + walidacja.
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

Opcjonalnie (podsumowanie tekstowe): ustaw ANTHROPIC_API_KEY w .env.
Bez tego skrypt działa normalnie, tylko pomija ten krok.

Logika rozliczenia jest w osobnych modułach (config.py, pdf_parser.py,
allegro_api.py, rozliczenie.py, llm_summary.py) — ten plik to tylko punkt
wejścia CLI (argumenty, orkiestracja, eksport CSV).
"""
import csv
import re
import sys

from config import wczytaj_sklepy, zakres_dat
from pdf_parser import parsuj_pdf_mbank
from allegro_api import autoryzuj
from rozliczenie import rozlicz_sklep, operator_z_op
from llm_summary import generuj_podsumowanie_llm


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
    stats_wszystkie = {}              # (sklep, operator) -> stats
    operacje_wszystkich_sklepow = []  # do diagnostyki sierot niżej

    for sklep in sklepy:
        auth_headers = autoryzuj(sklep["nazwa"], sklep["client_id"], sklep["client_secret"])
        wiersze, stats, operacje_sklepu = rozlicz_sklep(
            sklep["nazwa"], auth_headers, date_od, date_do, miesiac_od, wyciag_przelewy
        )
        wiersze_csv.extend(wiersze)
        for operator, dane in stats.items():
            stats_wszystkie[(sklep["nazwa"], operator)] = dane
        operacje_wszystkich_sklepow.extend(
            [{**o, "_sklep": sklep["nazwa"]} for o in operacje_sklepu]
        )

    # kwoty z wyciągu bez odpowiadającej wypłaty w ŻADNYM ze sklepów
    sieroty = [w for w in wyciag_przelewy if not w["uzyta"]]
    if sieroty:
        print("\n" + "=" * 60)
        print("UWAGA: kwoty z wyciągu BEZ odpowiadającej wypłaty w żadnym sklepie")
        print("=" * 60)
        for w in sieroty:
            print(f"  {w['data']} | {w['kwota']:>8.2f} PLN  — sprawdź ręcznie")
            # diagnostyka: pokaż zbliżone PAYOUT-y z API (kwota w promieniu
            # 1 PLN, w dowolnym ze sklepów), żeby zobaczyć czy to np.
            # przesunięcie daty a nie brak wypłaty w ogóle.
            kandydaci = [
                o for o in operacje_wszystkich_sklepow
                if o.get("type") == "PAYOUT"
                and abs(round(abs(float(o["value"]["amount"])), 2) - w["kwota"]) < 1.0
            ]
            for k in kandydaci:
                kw_ = round(abs(float(k["value"]["amount"])), 2)
                print(f"      [diagnostyka] zbliżony PAYOUT w API: {k['occurredAt']} "
                      f"| {kw_:.2f} PLN | operator={operator_z_op(k)} | sklep={k['_sklep']}")
            wiersze_csv.append({
                "sklep": "NIEZNANY",
                "data": w["data"],
                "operator": "NIEZNANY",
                "kwota_przelewu": f"{w['kwota']:.2f}",
                "l_kupujacych": "",
                "oplaty": "",
                "zwroty": "",
                "status": "ROZBIEZNOSC (brak w API)",
            })

    # eksport CSV
    print("\n" + "=" * 60)
    print("EKSPORT ROZLICZENIA")
    print("=" * 60)

    plik_csv = f"rozliczenie_{miesiac_od[:7]}.csv"
    with open(plik_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["sklep", "data", "operator", "kwota_przelewu",
                           "l_kupujacych", "oplaty", "zwroty", "status"]
        )
        writer.writeheader()
        writer.writerows(wiersze_csv)
    print(f"Zapisano: {plik_csv}  ({len(wiersze_csv)} wierszy)")

    # opcjonalne podsumowanie LLM
    stats_dla_llm = [
        {"sklep": sklep, "operator": operator, **dane}
        for (sklep, operator), dane in stats_wszystkie.items()
    ]
    if sieroty:
        stats_dla_llm.append({
            "sklep": "NIEZNANY", "operator": "NIEZNANY (brak w API)",
            "ok": 0, "rozbieznosci": len(sieroty), "suma": 0.0,
            "suma_rozbieznosci": round(sum(w["kwota"] for w in sieroty), 2),
        })

    podsumowanie = generuj_podsumowanie_llm(stats_dla_llm)
    if podsumowanie:
        print("\n--- Podsumowanie ---")
        print(podsumowanie)


if __name__ == "__main__":
    main()
