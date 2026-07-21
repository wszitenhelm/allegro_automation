"""Rdzeń rozliczenia: dopasowanie wypłat do wyciągu + walidacja, per sklep."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from allegro_api import pobierz_wszystkie
from config import API_URL, LIMIT, TOLERANCJA_DNI

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


def _nazwa_uczestnika(op):
    p = op.get("participant", {})
    return p.get("companyName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()


def _jako_lista(operacje):
    """Zamienia listę operacji (kupujący/zwroty) na proste dicty do wyświetlenia w UI."""
    return [
        {"data": o["occurredAt"][:10], "kwota": float(o["value"]["amount"]), "nazwa": _nazwa_uczestnika(o)}
        for o in operacje
    ]


def rozlicz_sklep(nazwa_sklepu, auth_headers, date_od, date_do, miesiac_od, wyciag_przelewy):
    """
    Pobiera dane jednego sklepu/konta Allegro i dopasowuje jego wypłaty do
    WSPÓLNEJ puli wyciag_przelewy (współdzielonej między sklepami przez
    flagę "uzyta" — jeden wpis z wyciągu można przypisać tylko raz, do
    jednego sklepu/operatora — mutowana w miejscu przez wywołującego dla
    kolejnych sklepów).

    Zwraca (wiersze_csv, stats_operator, wszystkie_operacje) dla tego sklepu.
    """
    print("\n" + "#" * 60)
    print(f"# SKLEP: {nazwa_sklepu}")
    print("#" * 60)

    print("\n" + "=" * 60)
    print("ROZBICIE PRZELEWÓW BANKOWYCH NA KUPUJĄCYCH (per operator)")
    print("=" * 60)
    # jedno pobranie bez filtra 'group' wystarcza na wpłaty (INCOME), zwroty
    # (REFUND) i wypłaty (PAYOUT) — filtrujemy lokalnie zamiast pobierać te
    # same dane trzy razy osobno z API
    wszystkie_operacje = pobierz_wszystkie(
        f"{API_URL}/payments/payment-operations",
        {"limit": LIMIT, "occurredAt.gte": date_od, "occurredAt.lte": date_do, "currency": "PLN"},
        auth_headers,
    )
    wszystkie_operacje.sort(key=lambda x: x["occurredAt"])

    suma_wplat = sum(float(o["value"]["amount"]) for o in wszystkie_operacje if o.get("group") == "INCOME")
    print(f"Wpłaty od kupujących {date_od[:10]} – {date_do[:10]}: "
          f"{sum(1 for o in wszystkie_operacje if o.get('group') == 'INCOME')}  |  Suma: {suma_wplat:.2f} PLN")

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
        # wyciag_przelewy jest dzielony (global, "uzyta" flaga) między
        # sklepami i operatorami, żeby ta sama para (data, kwota) nie
        # została dopasowana dwa razy. Gdy więcej niż jeden wpis z wyciągu
        # pasuje kwotą w oknie ±1 dnia, wybierany jest ten z najmniejszą
        # różnicą dni (najbliższy).
        wyplaty_all = sorted(
            [o for o in ops_op if o.get("type") == "PAYOUT" and o["occurredAt"] >= miesiac_od],
            key=lambda x: x["occurredAt"]
        )
        wyplaty = []
        for o in wyplaty_all:
            kwota_abs = round(abs(float(o["value"]["amount"])), 2)
            data_api  = data_lokalna(o["occurredAt"])
            kandydaci = sorted(
                (w for w in wyciag_przelewy
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
                         and o["occurredAt"] < miesiac_od]
        prev_time = wyplaty_przed[-1]["occurredAt"] if wyplaty_przed else date_od

        stats_operator_sklepu.setdefault(operator, {
            "liczba_przelewow": 0, "suma_przelewow": 0.0,
            "suma_zamowien": 0.0, "suma_oplat": 0.0, "suma_zwrotow": 0.0,
        })

        for wyplata in wyplaty:
            czas_wyplaty      = wyplata["occurredAt"]
            kwota_wyplaty_abs = round(abs(float(wyplata["value"]["amount"])), 2)

            kupujacy    = [o for o in wplaty if prev_time < o["occurredAt"] <= czas_wyplaty]
            zwroty_okna = [o for o in zwroty_op if prev_time < o["occurredAt"] <= czas_wyplaty]

            data_wyplaty     = czas_wyplaty[:10]
            suma_kupujacych  = sum(float(o["value"]["amount"]) for o in kupujacy)
            suma_zwrotow_abs = sum(abs(float(o["value"]["amount"])) for o in zwroty_okna)
            # Pobranie opłat Allegro liczone jako reszta z równania (nie z
            # osobnego zapytania do billing-entries): rzeczywiste opłaty
            # Allegro (prowizja + dostawa itd.) są rozliczane z opóźnieniem,
            # które nie pokrywa się z oknem między przelewami, więc nie da
            # się ich wiarygodnie dopasować per przelew. Wzór:
            #   Σ zamówień − kwota przelewu − zwroty = Pobranie opłat Allegro
            oplaty_rzeczywiste = round(suma_kupujacych - kwota_wyplaty_abs - suma_zwrotow_abs, 2)

            print(f"\n  PRZELEW: {data_wyplaty} | {kwota_wyplaty_abs:.2f} PLN  "
                  f"[Σ zamówień: {suma_kupujacych:.2f} - pobranie opłat Allegro: "
                  f"{oplaty_rzeczywiste:.2f} - zwroty: {suma_zwrotow_abs:.2f}]")
            for o in kupujacy:
                print(f"    wpłata: {o['occurredAt']} | {float(o['value']['amount']):>8.2f} PLN | "
                      f"{_nazwa_uczestnika(o)}")
            for o in zwroty_okna:
                print(f"    zwrot:  {o['occurredAt']} | {float(o['value']['amount']):>8.2f} PLN | "
                      f"{_nazwa_uczestnika(o)}")

            wiersze_csv_sklepu.append({
                "sklep": nazwa_sklepu,
                "data": data_wyplaty,
                "operator": operator,
                "kwota_przelewu": f"{kwota_wyplaty_abs:.2f}",
                "l_kupujacych": str(len(kupujacy)),
                "suma_zamowien": f"{suma_kupujacych:.2f}",
                "oplaty": f"{oplaty_rzeczywiste:.2f}",
                "zwroty": f"{suma_zwrotow_abs:.2f}",
                # nie trafiają do CSV (csv.DictWriter pisze tylko zdefiniowane
                # fieldnames) — używane przez frontend do pokazania szczegółów
                # po kliknięciu w wiersz
                "kupujacy_lista": _jako_lista(kupujacy),
                "zwroty_lista": _jako_lista(zwroty_okna),
            })
            st = stats_operator_sklepu[operator]
            st["liczba_przelewow"] += 1
            st["suma_przelewow"] += kwota_wyplaty_abs
            st["suma_zamowien"] += suma_kupujacych
            st["suma_oplat"] += oplaty_rzeczywiste
            st["suma_zwrotow"] += suma_zwrotow_abs

            prev_time = czas_wyplaty

    suma_zwrotow = sum(float(o["value"]["amount"]) for o in wszystkie_operacje if o.get("group") == "REFUND")
    l_zwrotow = sum(1 for o in wszystkie_operacje if o.get("group") == "REFUND")
    print(f"\nZwroty {date_od[:10]} – {date_do[:10]}: {l_zwrotow}  |  Suma: {suma_zwrotow:.2f} PLN")

    return wiersze_csv_sklepu, stats_operator_sklepu, wszystkie_operacje
