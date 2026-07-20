"""
Allegro Finance — rozbicie przelewów bankowych na kupujących.

Użycie:
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11

Wymagania:
  pip install requests
  brew install poppler   (dla pdftotext)
"""
import os
import requests
import time
import sys
import re
import subprocess
import calendar
from datetime import date, timedelta
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["ALLEGRO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_CLIENT_SECRET"]
LIMIT         = 100

# ── PARSOWANIE PDF ────────────────────────────────────────────────────────────
# Uwaga (decyzja projektowa): treść wyciągu bankowego (PDF) jest przetwarzana
# wyłącznie lokalnie (pdftotext + regex) i nigdy nie jest wysyłana do żadnego
# zewnętrznego LLM/API. Wyciąg zawiera dane wrażliwe (IBAN, kontrahenci spoza
# Allegro). Jeśli w przyszłości dojdzie tu LLM (np. do bardziej odpornego
# parsowania czy walidacji rozbieżności), ma dostawać tylko już
# zagregowane liczby (kwoty/sumy per operator) — nigdy surowego tekstu
# wyciągu ani danych osobowych kupujących (imię/nazwisko z Allegro API).

UUID_RE  = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE
)
# kwota w formacie polskim: 1 031,75 lub 235,83 (nie łapie dat DD.MM.YYYY)
KWOTA_RE = re.compile(r'\b(\d{1,3}(?:[ \xa0]\d{3})*),(\d{2})\b')
# linia zaczynająca się od daty DD.MM.YYYY = nagłówek wpisu w mBank
DATA_LINIA_RE = re.compile(r'^\s*\d{4}-\d{2}-\d{2}')  # mBank: YYYY-MM-DD


def _kwota_z_liczb(matches):
    """Przetwarza wyniki KWOTA_RE.findall na float."""
    wynik = []
    for tys, gr in matches:
        val = round(float(f"{tys.replace(' ','').replace(chr(160),'')}.{gr}"), 2)
        if val > 0:
            wynik.append(val)
    return wynik


def parsuj_pdf_mbank(sciezka):
    """
    Wyciąga kwoty przelewów Allegro Finance z PDF mBank (przez pdftotext).
    Zwraca Counter[kwota_float].

    Strategia: dla każdej linii z UUID+Allegro skanuje wstecz do linii daty
    (DD.MM.YYYY...) i bierze PIERWSZĄ liczbę z tej linii — to kwota przelewu.
    W mBank layout kwota przelewu jest zawsze przed saldem bieżącym.
    """
    proc = subprocess.run(
        ["pdftotext", "-layout", str(sciezka), "-"],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.exit(
            "Błąd pdftotext. Zainstaluj poppler:\n"
            "  brew install poppler"
        )

    linie = proc.stdout.splitlines()
    kwoty = Counter()
    przetworzone = set()  # indeksy linii dat już policzonych (każda transakcja ma 2 UUID)

    for i, linia in enumerate(linie):
        if not UUID_RE.search(linia):
            continue
        # kontekst ±8 linii — sprawdź czy to przelew Allegro Finance
        okno_tekst = "\n".join(linie[max(0, i - 8): i + 3])
        if "allegro" not in okno_tekst.lower():
            continue

        # skanuj WSTECZ do linii daty (nagłówek wpisu mBank: YYYY-MM-DD ...)
        for j in range(i, max(0, i - 10), -1):
            if DATA_LINIA_RE.match(linie[j]):
                if j not in przetworzone:
                    liczby = _kwota_z_liczb(KWOTA_RE.findall(linie[j]))
                    if liczby:
                        kwoty[liczby[0]] += 1
                    przetworzone.add(j)
                break

    if not kwoty:
        sys.exit(
            "Nie znaleziono przelewów Allegro Finance w pliku PDF.\n"
            "Upewnij się że to wyciąg mBank z przelewami Allegro Finance."
        )

    print(f"[PDF] Znaleziono {sum(kwoty.values())} przelewów Allegro Finance: "
          f"{sorted(kwoty.elements())}")
    return kwoty


def ustal_parametry():
    """Parsuje argumenty i zwraca (DATE_OD, DATE_DO, MIESIAC_OD, KWOTY_WYCIAG)."""
    args = sys.argv[1:]
    pdf_plik  = next((a for a in args if a.lower().endswith(".pdf")), None)
    miesiac_s = next((a for a in args if re.match(r'^\d{4}-\d{2}$', a)), None)

    if not pdf_plik or not miesiac_s:
        print(__doc__)
        sys.exit("Podaj plik PDF i miesiąc, np.:  python3 allegro_rozliczenie.py wyciag.pdf 2025-11")

    kwoty = parsuj_pdf_mbank(pdf_plik)
    rok   = int(miesiac_s[:4])
    mies  = int(miesiac_s[5:7])

    ostatni_dzien = calendar.monthrange(rok, mies)[1]
    miesiac_od    = date(rok, mies, 1)
    prev          = (miesiac_od - timedelta(days=1)).replace(day=1)

    date_od      = f"{prev.isoformat()}T00:00:00Z"
    date_do      = f"{rok}-{mies:02d}-{ostatni_dzien:02d}T23:59:59Z"
    miesiac_od_s = f"{miesiac_od.isoformat()}T00:00:00Z"

    return date_od, date_do, miesiac_od_s, kwoty


DATE_OD, DATE_DO, MIESIAC_OD, KWOTY_WYCIAG = ustal_parametry()

BASE_URL = "https://allegro.pl"
API_URL  = "https://api.allegro.pl"
HEADERS  = {"Accept": "application/vnd.allegro.public.v1+json"}

# ── KROK 1: pobierz device code ──────────────────────────────────────────────
print("Pobieram kod autoryzacji...")
r = requests.post(
    f"{BASE_URL}/auth/oauth/device",
    auth=(CLIENT_ID, CLIENT_SECRET),
    data={"client_id": CLIENT_ID},
)
r.raise_for_status()
device = r.json()

print(f"\n>>> Wejdź na: {device['verification_uri_complete']}")
print(">>> Zatwierdź dostęp w przeglądarce, a potem wróć tutaj i naciśnij Enter.")
input()

# ── KROK 2: odbierz token ─────────────────────────────────────────────────────
print("Pobieram token...")
interval = device.get("interval", 5)
while True:
    r = requests.post(
        f"{BASE_URL}/auth/oauth/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
        },
    )
    data = r.json()
    if "access_token" in data:
        token = data["access_token"]
        print("Token OK.\n")
        break
    if data.get("error") == "authorization_pending":
        time.sleep(interval)
    else:
        print("Błąd:", data)
        exit(1)

auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}

def pobierz_wszystkie(url, params):
    """Pobiera wszystkie strony wyników (paginacja)."""
    wyniki = []
    offset = 0
    while True:
        params["offset"] = offset
        r = requests.get(url, headers=auth_headers, params=params)
        r.raise_for_status()
        dane = r.json()
        # payment-operations zwraca 'paymentOperations', billing zwraca 'billingEntries'
        klucz = next((k for k in dane if isinstance(dane[k], list)), None)
        if not klucz:
            break
        batch = dane[klucz]
        wyniki.extend(batch)
        total = dane.get("totalCount", dane.get("count", len(batch)))
        offset += len(batch)
        if offset >= total or len(batch) == 0:
            break
        print(f"  ...pobrano {offset}/{total}")
    return wyniki

# ── KROK 3: wpłaty od kupujących (INCOME) ────────────────────────────────────
print("=" * 60)
print(f"WPŁATY OD KUPUJĄCYCH  {DATE_OD[:10]} – {DATE_DO[:10]}")
print("=" * 60)

ops = pobierz_wszystkie(
    f"{API_URL}/payments/payment-operations",
    {
        "group": "INCOME",
        "limit": LIMIT,
        "occurredAt.gte": DATE_OD,
        "occurredAt.lte": DATE_DO,
        "currency": "PLN",
    },
)

suma_wplat = 0.0
for op in ops:
    p = op.get("participant", {})
    nazwa = p.get("companyName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
    kwota = float(op["value"]["amount"])
    suma_wplat += kwota
    data_op  = op["occurredAt"][:10]
    pid      = op.get("payment", {}).get("id", "—")
    operator = op.get("wallet", {}).get("paymentOperator", "—")
    print(f"{data_op} | {kwota:>8.2f} PLN | {operator:<8} | {pid} | {nazwa}")

print(f"\nŁącznie wpłat: {len(ops)}  |  Suma: {suma_wplat:.2f} PLN")

# ── KROK 4: grupowanie wpłat według wypłat — osobno per operator ─────────────
print("\n" + "=" * 60)
print("ROZBICIE PRZELEWÓW BANKOWYCH NA KUPUJĄCYCH (per operator)")
print("=" * 60)

# pobierz wszystkie operacje z szerokiego zakresu dat (bez filtra group)
wszystkie_operacje = pobierz_wszystkie(
    f"{API_URL}/payments/payment-operations",
    {
        "limit": LIMIT,
        "occurredAt.gte": DATE_OD,
        "occurredAt.lte": DATE_DO,
        "currency": "PLN",
    },
)

# posortuj od najstarszej do najnowszej
wszystkie_operacje.sort(key=lambda x: x["occurredAt"])

NAZWY_OPERATOROW = {
    "AF":       "Allegro Finance (bezpośredni)",
    "AF_PAYU":  "Allegro Finance — PayU",
    "AF_P24":   "Allegro Finance — Przelewy24 (PayPro)",
    "PAYPRO":   "Allegro Finance — Przelewy24 (PayPro)",
}

def operator_z_op(op):
    return op.get("wallet", {}).get("paymentOperator", "UNKNOWN")

# zbierz unikalne operatory
operatory = sorted(set(operator_z_op(o) for o in wszystkie_operacje))

for operator in operatory:
    ops_op = [o for o in wszystkie_operacje if operator_z_op(o) == operator]
    wplaty    = [o for o in ops_op if o.get("group") == "INCOME"]
    zwroty_op = [o for o in ops_op if o.get("group") == "REFUND"]

    # Wypłaty bankowe z miesiąca — filtrowane po kwotach z wyciągu PDF
    wyplaty_all = sorted(
        [o for o in ops_op if o.get("type") == "PAYOUT" and o["occurredAt"] >= MIESIAC_OD],
        key=lambda x: x["occurredAt"]
    )
    uzyte_kwoty = Counter()
    wyplaty = []
    for o in wyplaty_all:
        kwota_abs = round(abs(float(o["value"]["amount"])), 2)
        if KWOTY_WYCIAG[kwota_abs] > uzyte_kwoty[kwota_abs]:
            uzyte_kwoty[kwota_abs] += 1
            wyplaty.append(o)

    if not wyplaty:
        continue

    nazwa_op = NAZWY_OPERATOROW.get(operator, operator)
    print(f"\n{'═'*60}")
    print(f"OPERATOR: {nazwa_op}  ({len(wyplaty)} przelewów bankowych)")
    print(f"{'═'*60}")

    # znajdź ostatnią wypłatę z PRZED miesiącem jako punkt startowy
    wyplaty_przed = [o for o in ops_op if o.get("type") == "PAYOUT"
                     and o["occurredAt"] < MIESIAC_OD]
    prev_time = wyplaty_przed[-1]["occurredAt"] if wyplaty_przed else DATE_OD

    for wyplata in wyplaty:
        czas_wyplaty  = wyplata["occurredAt"]
        kwota_wyplaty = float(wyplata["value"]["amount"])

        kupujacy = [
            o for o in wplaty
            if prev_time < o["occurredAt"] <= czas_wyplaty
        ]
        zwroty_okna = [
            o for o in zwroty_op
            if prev_time < o["occurredAt"] <= czas_wyplaty
        ]

        data_wyplaty      = czas_wyplaty[:10]
        kwota_wyplaty_abs = round(abs(kwota_wyplaty), 2)
        suma_kupujacych   = sum(float(o["value"]["amount"]) for o in kupujacy)
        suma_zwrotow_abs  = sum(abs(float(o["value"]["amount"])) for o in zwroty_okna)
        oplaty_impl       = round(suma_kupujacych - kwota_wyplaty_abs - suma_zwrotow_abs, 2)

        print(f"\n  PRZELEW: {data_wyplaty} | {kwota_wyplaty_abs:.2f} PLN")
        if kupujacy:
            for o in kupujacy:
                p = o.get("participant", {})
                nazwa = (p.get("companyName") or
                         f"{p.get('firstName','')} {p.get('lastName','')}".strip())
                kwota = float(o["value"]["amount"])
                pid   = o.get("payment", {}).get("id", "—")
                print(f"    {o['occurredAt'][:10]} | {kwota:>8.2f} PLN | {nazwa} | {pid}")
        if zwroty_okna:
            print(f"    --- zwroty ---")
            for o in zwroty_okna:
                p = o.get("participant", {})
                nazwa = (p.get("companyName") or
                         f"{p.get('firstName','')} {p.get('lastName','')}".strip())
                kwota = float(o["value"]["amount"])
                pid   = o.get("payment", {}).get("id", "—")
                print(f"    {o['occurredAt'][:10]} | {kwota:>8.2f} PLN | ZWROT | {pid} | {nazwa}")
        if kupujacy or zwroty_okna:
            print(f"    Σ kupujących: {suma_kupujacych:.2f} - opłaty: {oplaty_impl:.2f} - zwroty: {suma_zwrotow_abs:.2f} = {kwota_wyplaty_abs:.2f} PLN")
        else:
            print(f"    (brak wpłat w tym oknie)")

        prev_time = czas_wyplaty

# ── KROK 6: opłaty Allegro (prowizje, wystawienie itp.) ──────────────────────
print("\n" + "=" * 60)
print(f"OPŁATY ALLEGRO (prowizje, wystawienie...)  {DATE_OD[:10]} – {DATE_DO[:10]}")
print("=" * 60)

billing = pobierz_wszystkie(
    f"{API_URL}/billing/billing-entries",
    {
        "limit": LIMIT,
        "occurredAt.gte": DATE_OD,
        "occurredAt.lte": DATE_DO,
    },
)

suma_oplat = 0.0
pokaz = [b for b in billing if b["type"]["name"] == "Pobranie opłat z wpływów"]
for b in pokaz:
    kwota   = float(b["value"]["amount"])
    suma_oplat += kwota
    data_op = b["occurredAt"][:10]
    print(f"{data_op} | {kwota:>8.2f} PLN | Pobranie opłat z wpływów")

print(f"\nŁącznie pozycji: {len(pokaz)}  |  Suma: {suma_oplat:.2f} PLN")

# ── KROK 7: zwroty ────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"ZWROTY  {DATE_OD[:10]} – {DATE_DO[:10]}")
print("=" * 60)

zwroty = pobierz_wszystkie(
    f"{API_URL}/payments/payment-operations",
    {
        "group": "REFUND",
        "limit": LIMIT,
        "occurredAt.gte": DATE_OD,
        "occurredAt.lte": DATE_DO,
        "currency": "PLN",
    },
)

suma_zwrotow = 0.0
for op in zwroty:
    p = op.get("participant", {})
    nazwa   = p.get("companyName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
    kwota   = float(op["value"]["amount"])
    suma_zwrotow += kwota
    data_op = op["occurredAt"][:10]
    pid     = op.get("payment", {}).get("id", "—")
    typ     = op.get("type", "—")
    print(f"{data_op} | {kwota:>8.2f} PLN | {typ:<20} | {pid} | {nazwa}")

if not zwroty:
    print("Brak zwrotów w tym okresie.")

print(f"\nŁącznie zwrotów: {len(zwroty)}  |  Suma: {suma_zwrotow:.2f} PLN")