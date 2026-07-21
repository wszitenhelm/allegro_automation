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
from dotenv import load_dotenv

load_dotenv()


def _sklep_z_env(prefix, nazwa):
    client_id = os.environ.get(f"ALLEGRO_{prefix}_CLIENT_ID")
    client_secret = os.environ.get(f"ALLEGRO_{prefix}_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    return {"nazwa": nazwa, "client_id": client_id, "client_secret": client_secret}


SKLEPY = [s for s in [
    _sklep_z_env("PIGMEJKA", "pigmejka-pl"),
    _sklep_z_env("DECOR", "decor4-pl"),
] if s is not None]

if not SKLEPY:
    sys.exit(
        "Brak skonfigurowanych sklepów w .env. Ustaw co najmniej:\n"
        "  ALLEGRO_PIGMEJKA_CLIENT_ID / ALLEGRO_PIGMEJKA_CLIENT_SECRET\n"
        "(opcjonalnie też ALLEGRO_DECOR_CLIENT_ID / ALLEGRO_DECOR_CLIENT_SECRET)"
    )

LIMIT          = 100
TOLERANCJA     = 0.00  # dopuszczalna różnica przy walidacji (grosze zaokrągleń)
TOLERANCJA_DNI = 1      # dopuszczalne przesunięcie daty (opóźnienie księgowe banku)

# ── PARSOWANIE PDF ────────────────────────────────────────────────────────────
# Uwaga (decyzja projektowa): treść wyciągu bankowego (PDF) jest przetwarzana
# wyłącznie lokalnie (pdftotext + regex) i nigdy nie jest wysyłana do żadnego
# zewnętrznego LLM/API. Wyciąg zawiera dane wrażliwe (IBAN, kontrahenci spoza
# Allegro). Jedyne dane trafiające do LLM (patrz generuj_podsumowanie_llm) to
# już zagregowane liczby per sklep/operator (ile przelewów, jakie sumy) — nigdy
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
    w wyciągu w różnych dniach (różne operatory/wypłaty/sklepy), więc samo
    liczenie wystąpień kwoty (bez daty) nie wystarcza do jednoznacznego
    dopasowania.
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

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

NAZWY_OPERATOROW = {
    "AF":       "Allegro Finance (bezpośredni)",
    "AF_PAYU":  "Allegro Finance — PayU",
    "AF_P24":   "Allegro Finance — Przelewy24 (PayPro)",
    "PAYPRO":   "Allegro Finance — Przelewy24 (PayPro)",
}


def data_lokalna(occurred_at_iso):
    """
    occurredAt z API jest w UTC. Wyciąg mBank i panel Allegro pokazują czas
    lokalny (Europe/Warsaw) — dla transakcji blisko północy surowa data UTC
    i data lokalna mogą różnić się o 1 dzień. Porównujemy więc zawsze po
    dacie lokalnej, a nie po surowym occurredAt[:10].
    """
    dt = datetime.fromisoformat(occurred_at_iso.replace("Z", "+00:00"))
    return dt.astimezone(WARSAW_TZ).date()


def operator_z_op(op):
    return op.get("wallet", {}).get("paymentOperator", "UNKNOWN")


def autoryzuj(nazwa_sklepu, client_id, client_secret):
    """Device-code OAuth flow dla jednego sklepu/konta Allegro. Zwraca auth_headers."""
    print(f"\n[{nazwa_sklepu}] Pobieram kod autoryzacji...")
    r = requests.post(
        f"{BASE_URL}/auth/oauth/device",
        auth=(client_id, client_secret),
        data={"client_id": client_id},
    )
    r.raise_for_status()
    device = r.json()

    print(f">>> [{nazwa_sklepu}] Wejdź na: {device['verification_uri_complete']}")
    print(">>> Zatwierdź dostęp w przeglądarce, a potem wróć tutaj i naciśnij Enter.")
    input()

    print(f"[{nazwa_sklepu}] Pobieram token...")
    interval = device.get("interval", 5)
    while True:
        r = requests.post(
            f"{BASE_URL}/auth/oauth/token",
            auth=(client_id, client_secret),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device["device_code"],
            },
        )
        data = r.json()
        if "access_token" in data:
            print(f"[{nazwa_sklepu}] Token OK.\n")
            return {**HEADERS, "Authorization": f"Bearer {data['access_token']}"}
        if data.get("error") == "authorization_pending":
            time.sleep(interval)
        else:
            sys.exit(f"[{nazwa_sklepu}] Błąd autoryzacji: {data}")


def pobierz_wszystkie(url, params, auth_headers):
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


def rozlicz_sklep(nazwa_sklepu, auth_headers):
    """
    Pobiera dane jednego sklepu/konta Allegro i dopasowuje jego wypłaty do
    WSPÓLNEJ, globalnej puli WYCIAG_PRZELEWY (współdzielonej między sklepami
    przez flagę "uzyta" — jeden wpis z wyciągu można przypisać tylko raz,
    do jednego sklepu/operatora).

    Zwraca (wiersze_csv, stats_operator, wszystkie_operacje) dla tego sklepu —
    wszystkie_operacje jest potrzebne później do diagnostyki sierot (patrz
    sekcja główna).
    """
    print("\n" + "#" * 60)
    print(f"# SKLEP: {nazwa_sklepu}")
    print("#" * 60)

    print("=" * 60)
    print(f"WPŁATY OD KUPUJĄCYCH  {DATE_OD[:10]} – {DATE_DO[:10]}")
    print("=" * 60)
    ops = pobierz_wszystkie(
        f"{API_URL}/payments/payment-operations",
        {"group": "INCOME", "limit": LIMIT, "occurredAt.gte": DATE_OD,
         "occurredAt.lte": DATE_DO, "currency": "PLN"},
        auth_headers,
    )
    suma_wplat = sum(float(op["value"]["amount"]) for op in ops)
    print(f"Łącznie wpłat: {len(ops)}  |  Suma: {suma_wplat:.2f} PLN")

    # opłaty Allegro (prowizje z wpływów) — RZECZYWISTA prowizja, potrzebna
    # do walidacji per przelew niżej, nie wyliczana jako dopełnienie.
    print("\n" + "=" * 60)
    print(f"OPŁATY ALLEGRO (prowizje z wpływów)  {DATE_OD[:10]} – {DATE_DO[:10]}")
    print("=" * 60)
    billing = pobierz_wszystkie(
        f"{API_URL}/billing/billing-entries",
        {"limit": LIMIT, "occurredAt.gte": DATE_OD, "occurredAt.lte": DATE_DO},
        auth_headers,
    )
    oplaty_pobrania = [b for b in billing if b["type"]["name"] == "Pobranie opłat z wpływów"]
    suma_oplat = sum(float(b["value"]["amount"]) for b in oplaty_pobrania)
    print(f"Łącznie pozycji: {len(oplaty_pobrania)}  |  Suma: {suma_oplat:.2f} PLN")

    print("\n" + "=" * 60)
    print("ROZBICIE PRZELEWÓW BANKOWYCH NA KUPUJĄCYCH (per operator) + WALIDACJA")
    print("=" * 60)
    wszystkie_operacje = pobierz_wszystkie(
        f"{API_URL}/payments/payment-operations",
        {"limit": LIMIT, "occurredAt.gte": DATE_OD, "occurredAt.lte": DATE_DO, "currency": "PLN"},
        auth_headers,
    )
    wszystkie_operacje.sort(key=lambda x: x["occurredAt"])
    operatory = sorted(set(operator_z_op(o) for o in wszystkie_operacje))

    wiersze_csv_sklepu = []
    stats_operator_sklepu = {}

    for operator in operatory:
        ops_op = [o for o in wszystkie_operacje if operator_z_op(o) == operator]
        wplaty    = [o for o in ops_op if o.get("group") == "INCOME"]
        zwroty_op = [o for o in ops_op if o.get("group") == "REFUND"]

        # Wypłaty bankowe z miesiąca — dopasowane do wyciągu po KWOCIE i DACIE
        # LOKALNEJ (patrz data_lokalna) z dodatkową tolerancją ±1 dzień na
        # opóźnienie księgowania w banku (weekend/dzień roboczy).
        # WYCIAG_PRZELEWY jest dzielony (global, "uzyta" flaga) między
        # sklepami i operatorami, żeby ta sama para (data, kwota) nie
        # została dopasowana dwa razy. Gdy więcej niż jeden wpis z wyciągu
        # pasuje kwotą w oknie ±1 dnia, wybierany jest ten z najmniejszą
        # różnicą dni (najbliższy).
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

        wyplaty_przed = [o for o in ops_op if o.get("type") == "PAYOUT"
                         and o["occurredAt"] < MIESIAC_OD]
        prev_time = wyplaty_przed[-1]["occurredAt"] if wyplaty_przed else DATE_OD

        stats_operator_sklepu.setdefault(operator, {
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

            print(f"\n  PRZELEW: {data_wyplaty} | {kwota_wyplaty_abs:.2f} PLN  "
                  f"[Σ kupujących: {suma_kupujacych:.2f} - prowizja: {oplaty_rzeczywiste:.2f} "
                  f"- zwroty: {suma_zwrotow_abs:.2f}]  [{status}]")

            wiersze_csv_sklepu.append({
                "sklep": nazwa_sklepu,
                "data": data_wyplaty,
                "operator": operator,
                "kwota_przelewu": f"{kwota_wyplaty_abs:.2f}",
                "l_kupujacych": len(kupujacy),
                "oplaty": f"{oplaty_rzeczywiste:.2f}",
                "zwroty": f"{suma_zwrotow_abs:.2f}",
                "status": status,
            })
            st = stats_operator_sklepu[operator]
            st["suma"] += kwota_wyplaty_abs
            if status == "OK":
                st["ok"] += 1
            else:
                st["rozbieznosci"] += 1
                st["suma_rozbieznosci"] += abs(roznica)

            prev_time = czas_wyplaty

    print("\n" + "=" * 60)
    print(f"ZWROTY  {DATE_OD[:10]} – {DATE_DO[:10]}")
    print("=" * 60)
    zwroty = pobierz_wszystkie(
        f"{API_URL}/payments/payment-operations",
        {"group": "REFUND", "limit": LIMIT, "occurredAt.gte": DATE_OD,
         "occurredAt.lte": DATE_DO, "currency": "PLN"},
        auth_headers,
    )
    suma_zwrotow = sum(float(op["value"]["amount"]) for op in zwroty)
    if not zwroty:
        print("Brak zwrotów w tym okresie.")
    print(f"Łącznie zwrotów: {len(zwroty)}  |  Suma: {suma_zwrotow:.2f} PLN")

    return wiersze_csv_sklepu, stats_operator_sklepu, wszystkie_operacje


def generuj_podsumowanie_llm(stats):
    """
    Generuje 2-3 zdania podsumowania po polsku na podstawie WYŁĄCZNIE
    zagregowanych liczb per sklep/operator (patrz `stats` — bez PII, bez
    treści wyciągu). Zwraca None (i drukuje powód) jeśli brak klucza API/
    pakietu, żeby reszta skryptu (CSV, walidacja) działała niezależnie od LLM.
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
        "Jesteś asystentem księgowej sklepów e-commerce. Poniżej masz WYŁĄCZNIE "
        "zagregowane liczby (bez danych osobowych, bez numerów kont) z "
        "miesięcznego rozliczenia Allegro Finance, per sklep i operator płatności. "
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


# ── uruchom rozliczenie dla każdego skonfigurowanego sklepu ──────────────────
wiersze_csv = []
stats_wszystkie = {}                       # (sklep, operator) -> stats
operacje_wszystkich_sklepow = []           # do diagnostyki sierot niżej

for sklep in SKLEPY:
    auth_headers = autoryzuj(sklep["nazwa"], sklep["client_id"], sklep["client_secret"])
    wiersze, stats, operacje_sklepu = rozlicz_sklep(sklep["nazwa"], auth_headers)
    wiersze_csv.extend(wiersze)
    for operator, dane in stats.items():
        stats_wszystkie[(sklep["nazwa"], operator)] = dane
    operacje_wszystkich_sklepow.extend(
        [{**o, "_sklep": sklep["nazwa"]} for o in operacje_sklepu]
    )

# ── kwoty z wyciągu bez odpowiadającej wypłaty w ŻADNYM ze sklepów ───────────
sieroty = [w for w in WYCIAG_PRZELEWY if not w["uzyta"]]
if sieroty:
    print("\n" + "=" * 60)
    print("UWAGA: kwoty z wyciągu BEZ odpowiadającej wypłaty w żadnym sklepie")
    print("=" * 60)
    for w in sieroty:
        print(f"  {w['data']} | {w['kwota']:>8.2f} PLN  — sprawdź ręcznie")
        # diagnostyka: pokaż zbliżone PAYOUT-y z API (kwota w promieniu 1 PLN,
        # w dowolnym ze sklepów), żeby zobaczyć czy to np. przesunięcie daty
        # a nie brak wypłaty w ogóle.
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

# ── eksport CSV + opcjonalne podsumowanie LLM ────────────────────────────────
print("\n" + "=" * 60)
print("EKSPORT ROZLICZENIA")
print("=" * 60)

plik_csv = f"rozliczenie_{MIESIAC_OD[:7]}.csv"
with open(plik_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f, fieldnames=["sklep", "data", "operator", "kwota_przelewu",
                       "l_kupujacych", "oplaty", "zwroty", "status"]
    )
    writer.writeheader()
    writer.writerows(wiersze_csv)
print(f"Zapisano: {plik_csv}  ({len(wiersze_csv)} wierszy)")

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
