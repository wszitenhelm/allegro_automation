"""Konfiguracja: sklepy z .env, stałe, zakres dat."""
import os
import calendar
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

LIMIT          = 100
TOLERANCJA_DNI = 1      # dopuszczalne przesunięcie daty (opóźnienie księgowe banku)

BASE_URL = "https://allegro.pl"
API_URL  = "https://api.allegro.pl"
HEADERS  = {"Accept": "application/vnd.allegro.public.v1+json"}

# Kolumny wyniku rozliczenia (CSV i frontend) — wspólne, żeby oba miejsca
# eksportu pokazywały to samo.
KOLUMNY_WYNIKU = ["sklep", "data", "operator", "kwota_przelewu",
                  "l_kupujacych", "suma_zamowien", "oplaty", "zwroty"]
NAZWY_KOLUMN_WYNIKU = {
    "sklep": "Sklep",
    "data": "Data",
    "operator": "Operator",
    "kwota_przelewu": "Kwota Przelewu",
    "l_kupujacych": "Liczba kupujących",
    "suma_zamowien": "Suma Zamówień",
    "oplaty": "Pobranie opłat Allegro",
    "zwroty": "Zwroty",
}


def _sklep_z_env(prefix, nazwa):
    client_id = os.environ.get(f"ALLEGRO_{prefix}_CLIENT_ID")
    client_secret = os.environ.get(f"ALLEGRO_{prefix}_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    return {"nazwa": nazwa, "client_id": client_id, "client_secret": client_secret}


def wczytaj_sklepy():
    """Zwraca listę skonfigurowanych sklepów (te bez pary CLIENT_ID/SECRET w .env są pomijane)."""
    return [s for s in [
        _sklep_z_env("PIGMEJKA", "pigmejka-pl"),
        _sklep_z_env("DECOR", "decor4-pl"),
    ] if s is not None]


def zakres_dat(rok, mies):
    """Zwraca (date_od, date_do, miesiac_od) w formacie ISO 8601 dla danego roku/miesiąca."""
    ostatni_dzien = calendar.monthrange(rok, mies)[1]
    miesiac_od    = date(rok, mies, 1)
    prev          = (miesiac_od - timedelta(days=1)).replace(day=1)

    date_od      = f"{prev.isoformat()}T00:00:00Z"
    date_do      = f"{rok}-{mies:02d}-{ostatni_dzien:02d}T23:59:59Z"
    miesiac_od_s = f"{miesiac_od.isoformat()}T00:00:00Z"
    return date_od, date_do, miesiac_od_s
