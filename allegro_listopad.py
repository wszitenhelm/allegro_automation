import os
import requests
import time
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["ALLEGRO_PIGMEJKA_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_PIGMEJKA_CLIENT_SECRET"]

# ── ZAKRES DAT ────────────────────────────────────────────────────────────────
DATE_OD        = "2025-10-01T00:00:00Z"   # szerszy zakres żeby złapać wpłaty z poprzedniego miesiąca
DATE_DO        = "2025-11-30T23:59:59Z"
MIESIAC_OD     = "2025-11-01T00:00:00Z"   # przelewy bankowe tylko z listopada
LIMIT          = 100

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
    wplaty  = [o for o in ops_op if o.get("group") == "INCOME"]
    wyplaty = [o for o in ops_op if o.get("type") == "PAYOUT"
               and o["occurredAt"] >= MIESIAC_OD]

    if not wyplaty:
        continue

    nazwa_op = NAZWY_OPERATOROW.get(operator, operator)
    print(f"\n{'═'*60}")
    print(f"OPERATOR: {nazwa_op}  ({len(wyplaty)} przelewów bankowych)")
    print(f"{'═'*60}")

    # znajdź ostatnią wypłatę z PRZED listopadem jako punkt startowy
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

        data_wyplaty = czas_wyplaty[:10]
        suma_kupujacych = sum(float(o["value"]["amount"]) for o in kupujacy)

        print(f"\n  PRZELEW: {data_wyplaty} | {abs(kwota_wyplaty):.2f} PLN")
        if kupujacy:
            for o in kupujacy:
                p = o.get("participant", {})
                nazwa = (p.get("companyName") or
                         f"{p.get('firstName','')} {p.get('lastName','')}".strip())
                kwota = float(o["value"]["amount"])
                pid   = o.get("payment", {}).get("id", "—")
                print(f"    {o['occurredAt'][:10]} | {kwota:>8.2f} PLN | {nazwa} | {pid}")
            print(f"    Σ kupujących: {suma_kupujacych:.2f} PLN → po opłatach: {abs(kwota_wyplaty):.2f} PLN")
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

# ── placeholder ──────────────────────────────────────────────────────────────
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