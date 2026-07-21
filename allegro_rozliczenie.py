"""
Allegro Finance - rozbicie przelewów bankowych na kupujących + walidacja.

Użycie:
  python3 allegro_rozliczenie.py wyciag.pdf 2025-11

Wymagania:
  pip install -r requirements.txt
  brew install poppler   (dla pdftotext)

Opcjonalnie (podsumowanie tekstowe): ustaw ANTHROPIC_API_KEY w .env.
Bez tego skrypt działa normalnie, tylko pomija ten krok.
"""
import os
import csv
import json
import requests
import time
import sys
import re
import subprocess
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["ALLEGRO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_CLIENT_SECRET"]
LIMIT         = 100
TOLERANCJA    = 0.00  # dopuszczalna różnica przy walidacji (grosze zaokrągleń)

# ── PARSOWANIE PDF ────────────────────────────────────────────────────────────
# Uwaga (decyzja projektowa): treść wyciągu bankowego (PDF) jest przetwarzana
# wyłącznie lokalnie (pdftotext + regex) i nigdy nie jest wysyłana do żadnego
# zewnętrznego LLM/API. Wyciąg zawiera dane wrażliwe (IBAN, kontrahenci spoza
# Allegro). Jedyne dane trafiające do LLM (patrz generuj_podsumowanie_llm) to
# już zagregowane liczby per operator (ile przelewów, jakie sumy) — nigdy
# surowy tekst wyciągu ani dane osobowe kupujących (imię/nazwisko z Allegro API).

UUID_RE  = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE
)
# kwota w formacie polskim: 1 031,75 lub 235,83 (nie łapie dat DD.MM.YYYY)
KWOTA_RE = re.compile(r'\b(\d{1,3}(?:[ \xa0]\d{3})*),(\d{2})\b')
# linia zaczynająca się od daty YYYY-MM-DD = nagłówek wpisu w mBank
DATA_LINIA_RE = re.compile(r'^\s*(\d{4}-\d{2}-\d{2})')


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
    Wyciąga (data, kwota) przelewów Allegro Finance z PDF mBank (przez pdftotext).
    Zwraca listę dictów: [{"data": "YYYY-MM-DD", "kwota": float, "uzyta": False}, ...]

    Strategia: dla każdej linii z UUID+Allegro skanuje wstecz do linii daty
    (YYYY-MM-DD...) i bierze PIERWSZĄ liczbę z tej linii — to kwota przelewu,
    a data z tej samej linii — to data zaksięgowania w mBank.
    W mBank layout kwota przelewu jest zawsze przed saldem bieżącym.

    Data jest potrzebna do dopasowania: ta sama kwota może wystąpić kilka razy
    w wyciągu w różnych dniach (różne operatory/wypłaty), więc samo liczenie
    wystąpień kwoty (bez daty) nie wystarcza do jednoznacznego dopasowania.
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
    przelewy = []
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
            dopasowanie_daty = DATA_LINIA_RE.match(linie[j])
            if dopasowanie_daty:
                if j not in przetworzone:
                    liczby = _kwota_z_liczb(KWOTA_RE.findall(linie[j]))
                    if liczby:
                        przelewy.append({
                            "data": dopasowanie_daty.group(1),
                            "kwota": liczby[0],
                            "uzyta": False,
                        })
                    przetworzone.add(j)
                break

    if not przelewy:
        sys.exit(
            "Nie znaleziono przelewów Allegro Finance w pliku PDF.\n"
            "Upewnij się że to wyciąg mBank z przelewami Allegro Finance."
        )

    podglad = sorted((p["data"], p["kwota"]) for p in przelewy)
    print(f"[PDF] Znaleziono {len(przelewy)} przelewów Allegro Finance: {podglad}")
    return przelewy


def ustal_parametry():
    """Parsuje argumenty i zwraca (DATE_OD, DATE_DO, MIESIAC_OD, WYCIAG_PRZELEWY)."""
    args = sys.argv[1:]
    pdf_plik  = next((a for a in args if a.lower().endswith(".pdf")), None)
    miesiac_s = next((a for a in args if re.match(r'^\d{4}-\d{2}$', a)), None)

    if not pdf_plik or not miesiac_s:
        print(__doc__)
        sys.exit("Podaj plik PDF i miesiąc, np.:  python3 allegro_rozliczenie.py wyciag.pdf 2025-11")

    przelewy = parsuj_pdf_mbank(pdf_plik)
    rok   = int(miesiac_s[:4])
    mies  = int(miesiac_s[5:7])

    ostatni_dzien = calendar.monthrange(rok, mies)[1]
    miesiac_od    = date(rok, mies, 1)
    prev          = (miesiac_od - timedelta(days=1)).replace(day=1)

    date_od      = f"{prev.isoformat()}T00:00:00Z"
    date_do      = f"{rok}-{mies:02d}-{ostatni_dzien:02d}T23:59:59Z"
    miesiac_od_s = f"{miesiac_od.isoformat()}T00:00:00Z"

    return date_od, date_do, miesiac_od_s, przelewy


DATE_OD, DATE_DO, MIESIAC_OD, WYCIAG_PRZELEWY = ustal_parametry()

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
    #print(f"{data_op} | {kwota:>8.2f} PLN | {operator:<8} | {pid} | {nazwa}")

print(f"\nŁącznie wpłat: {len(ops)}  |  Suma: {suma_wplat:.2f} PLN")

# ── KROK 4: opłaty Allegro (prowizje z wpływów) ───────────────────────────────
# Pobrane PRZED rozbiciem na operatorów, bo są potrzebne do walidacji per przelew
# (patrz KROK 5): to jest RZECZYWISTA prowizja, nie wyliczana jako dopełnienie.
print("\n" + "=" * 60)
print(f"OPŁATY ALLEGRO (prowizje z wpływów)  {DATE_OD[:10]} – {DATE_DO[:10]}")
print("=" * 60)

billing = pobierz_wszystkie(
    f"{API_URL}/billing/billing-entries",
    {
        "limit": LIMIT,
        "occurredAt.gte": DATE_OD,
        "occurredAt.lte": DATE_DO,
    },
)

oplaty_pobrania = [b for b in billing if b["type"]["name"] == "Pobranie opłat z wpływów"]
suma_oplat = sum(float(b["value"]["amount"]) for b in oplaty_pobrania)
#for b in oplaty_pobrania:
    #print(f"{b['occurredAt'][:10]} | {float(b['value']['amount']):>8.2f} PLN | Pobranie opłat z wpływów")
print(f"\nŁącznie pozycji: {len(oplaty_pobrania)}  |  Suma: {suma_oplat:.2f} PLN")

# ── KROK 5: grupowanie wpłat wg wypłat + walidacja — per operator ────────────
print("\n" + "=" * 60)
print("ROZBICIE PRZELEWÓW BANKOWYCH NA KUPUJĄCYCH (per operator) + WALIDACJA")
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

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

def data_lokalna(occurred_at_iso):
    """
    occurredAt z API jest w UTC. Wyciąg mBank i panel Allegro pokazują czas
    lokalny (Europe/Warsaw) — dla transakcji blisko północy surowa data UTC
    i data lokalna mogą różnić się o 1 dzień. Porównujemy więc zawsze po
    dacie lokalnej, a nie po surowym occurredAt[:10].
    """
    dt = datetime.fromisoformat(occurred_at_iso.replace("Z", "+00:00"))
    return dt.astimezone(WARSAW_TZ).date()

# zbierz unikalne operatory
operatory = sorted(set(operator_z_op(o) for o in wszystkie_operacje))

wiersze_csv = []      # do eksportu CSV — patrz KROK 7
stats_operator = {}   # operator -> {"ok", "rozbieznosci", "suma", "suma_rozbieznosci"} — do LLM, bez PII

for operator in operatory:
    ops_op = [o for o in wszystkie_operacje if operator_z_op(o) == operator]
    wplaty    = [o for o in ops_op if o.get("group") == "INCOME"]
    zwroty_op = [o for o in ops_op if o.get("group") == "REFUND"]

    # Wypłaty bankowe z miesiąca — dopasowane do wyciągu po KWOCIE i DACIE
    # LOKALNEJ (patrz data_lokalna) z dodatkową tolerancją ±1 dzień na
    # opóźnienie księgowania w banku (weekend/dzień roboczy).
    # WYCIAG_PRZELEWY jest dzielony (global, "uzyta" flaga) między operatorami,
    # żeby ta sama para (data, kwota) nie została dopasowana dwa razy. Gdy
    # więcej niż jeden wpis z wyciągu pasuje kwotą w oknie ±1 dnia, wybierany
    # jest ten z najmniejszą różnicą dni (najbliższy).
    TOLERANCJA_DNI = 1
    wyplaty_all = sorted(
        [o for o in ops_op if o.get("type") == "PAYOUT" and o["occurredAt"] >= MIESIAC_OD],
        key=lambda x: x["occurredAt"]
    )
    wyplaty = []
    for o in wyplaty_all:
        kwota_abs = round(abs(float(o["value"]["amount"])), 2)
        data_api  = data_lokalna(o["occurredAt"])
        kandydaci = sorted(
            (w for w in WYCIAG_PRZELEWY
             if not w["uzyta"] and w["kwota"] == kwota_abs
             and abs((date.fromisoformat(w["data"]) - data_api).days) <= TOLERANCJA_DNI),
            key=lambda w: abs((date.fromisoformat(w["data"]) - data_api).days)
        )
        if kandydaci:
            kandydaci[0]["uzyta"] = True
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

    stats_operator.setdefault(operator, {
        "ok": 0, "rozbieznosci": 0, "suma": 0.0, "suma_rozbieznosci": 0.0,
    })

    for wyplata in wyplaty:
        czas_wyplaty      = wyplata["occurredAt"]
        kwota_wyplaty_abs = round(abs(float(wyplata["value"]["amount"])), 2)

        kupujacy    = [o for o in wplaty if prev_time < o["occurredAt"] <= czas_wyplaty]
        zwroty_okna = [o for o in zwroty_op if prev_time < o["occurredAt"] <= czas_wyplaty]
        oplaty_okna = [b for b in oplaty_pobrania if prev_time < b["occurredAt"] <= czas_wyplaty]

        data_wyplaty       = czas_wyplaty[:10]
        suma_kupujacych    = sum(float(o["value"]["amount"]) for o in kupujacy)
        suma_zwrotow_abs   = sum(abs(float(o["value"]["amount"])) for o in zwroty_okna)
        oplaty_rzeczywiste = sum(abs(float(b["value"]["amount"])) for b in oplaty_okna)

        # Walidacja: suma wpłat − suma zwrotów − suma prowizji Allegro = kwota przelewu
        roznica = round(suma_kupujacych - suma_zwrotow_abs - oplaty_rzeczywiste - kwota_wyplaty_abs, 2)
        status  = "OK" if abs(roznica) <= TOLERANCJA else f"ROZBIEZNOSC ({roznica:+.2f} PLN)"

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
        if kupujacy or zwroty_okna or oplaty_okna:
            print(f"    Σ kupujących: {suma_kupujacych:.2f} - prowizja: {oplaty_rzeczywiste:.2f} "
                  f"- zwroty: {suma_zwrotow_abs:.2f} = {kwota_wyplaty_abs:.2f} PLN  [{status}]")
        else:
            print(f"    (brak wpłat w tym oknie)  [{status}]")

        wiersze_csv.append({
            "data": data_wyplaty,
            "operator": operator,
            "kwota_przelewu": f"{kwota_wyplaty_abs:.2f}",
            "l_kupujacych": len(kupujacy),
            "oplaty": f"{oplaty_rzeczywiste:.2f}",
            "zwroty": f"{suma_zwrotow_abs:.2f}",
            "status": status,
        })
        st = stats_operator[operator]
        st["suma"] += kwota_wyplaty_abs
        if status == "OK":
            st["ok"] += 1
        else:
            st["rozbieznosci"] += 1
            st["suma_rozbieznosci"] += abs(roznica)

        prev_time = czas_wyplaty

# ── kwoty z wyciągu bez odpowiadającej wypłaty w Allegro API ─────────────────
sieroty = [w for w in WYCIAG_PRZELEWY if not w["uzyta"]]
if sieroty:
    print("\n" + "=" * 60)
    print("UWAGA: kwoty z wyciągu BEZ odpowiadającej wypłaty w Allegro API")
    print("=" * 60)
    for w in sieroty:
        print(f"  {w['data']} | {w['kwota']:>8.2f} PLN  — sprawdź ręcznie")
        wiersze_csv.append({
            "data": w["data"],
            "operator": "NIEZNANY",
            "kwota_przelewu": f"{w['kwota']:.2f}",
            "l_kupujacych": "",
            "oplaty": "",
            "zwroty": "",
            "status": "ROZBIEZNOSC (brak w API)",
        })

# ── KROK 6: zwroty ────────────────────────────────────────────────────────────
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
    #print(f"{data_op} | {kwota:>8.2f} PLN | {typ:<20} | {pid} | {nazwa}")

if not zwroty:
    print("Brak zwrotów w tym okresie.")

print(f"\nŁącznie zwrotów: {len(zwroty)}  |  Suma: {suma_zwrotow:.2f} PLN")


# ── KROK 7: eksport CSV + opcjonalne podsumowanie LLM ────────────────────────

def generuj_podsumowanie_llm(stats):
    """
    Generuje 2-3 zdania podsumowania po polsku na podstawie WYŁĄCZNIE
    zagregowanych liczb per operator (patrz `stats` — bez PII, bez treści
    wyciągu). Zwraca None (i drukuje powód) jeśli brak klucza API/pakietu,
    żeby reszta skryptu (CSV, walidacja) działała niezależnie od LLM.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[LLM] Brak ANTHROPIC_API_KEY w .env — pomijam podsumowanie tekstowe.")
        return None
    try:
        import anthropic
    except ImportError:
        print("[LLM] Brak pakietu `anthropic` (pip install anthropic) — pomijam podsumowanie tekstowe.")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "Jesteś asystentem księgowej sklepu e-commerce. Poniżej masz WYŁĄCZNIE "
        "zagregowane liczby (bez danych osobowych, bez numerów kont) z "
        "miesięcznego rozliczenia Allegro Finance, per operator płatności. "
        "Napisz 2-3 zdania podsumowania po polsku: ile przelewów się zgadza, "
        "ile ma rozbieżności i na jaką łączną kwotę, czy coś wymaga uwagi "
        "księgowej przed zaksięgowaniem.\n\n"
        f"{json.dumps(stats, ensure_ascii=False, indent=2)}"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        print(f"[LLM] Błąd wywołania API ({e}) — pomijam podsumowanie tekstowe.")
        return None


print("\n" + "=" * 60)
print("EKSPORT ROZLICZENIA")
print("=" * 60)

plik_csv = f"rozliczenie_{MIESIAC_OD[:7]}.csv"
with open(plik_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f, fieldnames=["data", "operator", "kwota_przelewu", "l_kupujacych",
                       "oplaty", "zwroty", "status"]
    )
    writer.writeheader()
    writer.writerows(wiersze_csv)
print(f"Zapisano: {plik_csv}  ({len(wiersze_csv)} wierszy)")

stats_dla_llm = [{"operator": op, **dane} for op, dane in stats_operator.items()]
if sieroty:
    stats_dla_llm.append({
        "operator": "NIEZNANY (brak w API)",
        "ok": 0,
        "rozbieznosci": len(sieroty),
        "suma": 0.0,
        "suma_rozbieznosci": round(sum(w["kwota"] for w in sieroty), 2),
    })

podsumowanie = generuj_podsumowanie_llm(stats_dla_llm)
if podsumowanie:
    print("\n--- Podsumowanie ---")
    print(podsumowanie)
