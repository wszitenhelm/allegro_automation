"""Rdzeń rozliczenia: dopasowanie wypłat do wyciągu + walidacja, per sklep."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from allegro_api import pobierz_wszystkie, pobierz_dane_faktury, BrakUprawnienDoZamowien
from config import API_URL, LIMIT, TOLERANCJA_DNI
from nbp import kurs_nbp

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

NAZWY_OPERATOROW = {
    "AF":       "Allegro Finance",
    "AF_PAYU":  "Allegro Finance — PayU",
    "AF_P24":   "Allegro Finance — Przelewy24 (PayPro)",
    "PAYPRO":   "Allegro Finance — Przelewy24 (PayPro)",
}

TOLERANCJA_KURSU = 0.10  # +-10% miedzy oczekiwana kwota PLN (wg kursu NBP) a wpisem z wyciagu


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


def _waluta_op(op):
    return op.get("value", {}).get("currency", "PLN")


def _jako_lista(operacje):
    """Zamienia listę operacji (zwroty) na proste dicty do wyświetlenia w UI."""
    return [
        {"data": o["occurredAt"][:10], "kwota": float(o["value"]["amount"]),
         "waluta": _waluta_op(o), "nazwa": _nazwa_uczestnika(o)}
        for o in operacje
    ]


def _kupujacy_jako_lista(operacje, auth_headers, stan_faktur):
    """
    Jak _jako_lista, ale dla kupujących dodatkowo dogrywa dane faktury (jeśli
    kupujący jej zażądał) i dopisuje je do nazwy. `stan_faktur` to
    współdzielony dict {"dostepne": bool} — jeśli raz natrafimy na brak
    uprawnień do zamówień, przestajemy próbować dla kolejnych wpłat zamiast
    odpytywać API bez sensu setki razy o to samo.
    """
    wynik = []
    for o in operacje:
        nazwa = _nazwa_uczestnika(o)
        if stan_faktur["dostepne"]:
            payment_id = o.get("payment", {}).get("id")
            if payment_id:
                try:
                    wymagana, opis = pobierz_dane_faktury(payment_id, auth_headers)
                except BrakUprawnienDoZamowien as e:
                    stan_faktur["dostepne"] = False
                    print(f"[faktury] {e}")
                    wymagana, opis = False, ""
                if wymagana:
                    nazwa = f"{nazwa} — FAKTURA: {opis}" if opis else f"{nazwa} — FAKTURA"
        wynik.append({"data": o["occurredAt"][:10], "kwota": float(o["value"]["amount"]),
                      "waluta": _waluta_op(o), "nazwa": nazwa})
    return wynik


def _znajdz_najblizszy(wyciag_przelewy, pasuje_kwota, data_api):
    """
    Wspólna logika dopasowania używana w obu przebiegach (PLN i waluty
    obce) — różnią się tylko tym, CO znaczy "kwota pasuje" (patrz
    pasuje_kwota, przekazywane jako funkcja przez wywołującego).

    Wśród jeszcze niewykorzystanych wpisów z wyciągu ("uzyta"=False), w
    tolerancji ±TOLERANCJA_DNI dni od data_api, zwraca ten z najbliższą
    datą, dla którego pasuje_kwota(wpis["kwota"]) jest prawdziwe — albo
    None, jeśli żaden nie pasuje. Od razu oznacza znaleziony wpis jako
    "uzyta" (mutacja w miejscu — ta sama pula wyciag_przelewy jest
    współdzielona między kolejnymi sklepami/operatorami/walutami, patrz
    rozlicz_sklep, żeby jeden wpis z wyciągu nie trafił do dwóch miejsc).
    """
    kandydaci = sorted(
        (w for w in wyciag_przelewy
         if not w["uzyta"] and pasuje_kwota(w["kwota"])
         and abs((date.fromisoformat(w["data"]) - data_api).days) <= TOLERANCJA_DNI),
        key=lambda w: abs((date.fromisoformat(w["data"]) - data_api).days)
    )
    if not kandydaci:
        return None
    kandydaci[0]["uzyta"] = True
    return kandydaci[0]


def _przetworz_okna(nazwa_sklepu, nazwa_op, waluta, wyplaty_dopasowane, wplaty, zwroty_op,
                     prev_time_start, auth_headers, stan_faktur, wiersze_csv_sklepu, stats_operator_sklepu):
    """
    Wspólna logika dla PLN i walut obcych: dla każdej dopasowanej pary
    (wypłata, wpis_z_wyciągu) liczy okno kupujących/zwrotów w TEJ SAMEJ
    walucie co wypłata, wzór rezydualny (Σ zamówień − kwota przelewu − zwroty
    = pobranie opłat Allegro), buduje wiersz CSV i drukuje log. Dopisuje
    bezpośrednio do wiersze_csv_sklepu/stats_operator_sklepu (przez referencję).

    Dla walut obcych "kwota_przelewu" w wyniku pokazuje ZARÓWNO kwotę z
    wyciągu w PLN, JAK I oryginalną kwotę wypłaty (np. "185.30 zł (44.17 EUR)")
    — ale reszta liczb (Suma Zamówień, Pobranie opłat, Zwroty) zostaje w
    oryginalnej walucie, bez przeliczania kursem — tak jak ustalone z użytkowniczką.
    """
    # Osobny klucz per (operator, waluta), żeby staty PLN i np. EUR tego samego
    # operatora nie zlały się w jedną (bezsensowną) sumę różnych walut.
    klucz_stats = nazwa_op if waluta == "PLN" else f"{nazwa_op} ({waluta})"
    stats_operator_sklepu.setdefault(klucz_stats, {
        "liczba_przelewow": 0, "suma_przelewow": 0.0,
        "suma_zamowien": 0.0, "suma_oplat": 0.0, "suma_zwrotow": 0.0,
    })

    prev_time = prev_time_start
    for wyplata, wyciag_wpis in wyplaty_dopasowane:
        czas_wyplaty      = wyplata["occurredAt"]
        kwota_wyplaty_abs = round(abs(float(wyplata["value"]["amount"])), 2)

        kupujacy    = [o for o in wplaty if prev_time < o["occurredAt"] <= czas_wyplaty]
        zwroty_okna = [o for o in zwroty_op if prev_time < o["occurredAt"] <= czas_wyplaty]

        data_wyplaty     = wyciag_wpis["data"]
        suma_kupujacych  = sum(float(o["value"]["amount"]) for o in kupujacy)
        suma_zwrotow_abs = sum(abs(float(o["value"]["amount"])) for o in zwroty_okna)
        # Pobranie opłat Allegro liczone jako reszta z równania, w oryginalnej
        # walucie wypłaty (rzeczywiste opłaty Allegro rozliczają się z
        # opóźnieniem, które nie pokrywa się z oknem między przelewami, więc
        # nie da się ich wiarygodnie dopasować per przelew z osobnego zapytania):
        #   Σ zamówień − kwota przelewu − zwroty = Pobranie opłat Allegro
        oplaty_rzeczywiste = round(suma_kupujacych - kwota_wyplaty_abs - suma_zwrotow_abs, 2)

        kupujacy_szczegoly = _kupujacy_jako_lista(kupujacy, auth_headers, stan_faktur)
        zwroty_szczegoly = _jako_lista(zwroty_okna)

        if waluta == "PLN":
            kwota_przelewu_opis = f"{kwota_wyplaty_abs:.2f}"
        else:
            kwota_przelewu_opis = f"{wyciag_wpis['kwota']:.2f} zł ({kwota_wyplaty_abs:.2f} {waluta})"

        print(f"\n  PRZELEW: {data_wyplaty} | {kwota_przelewu_opis}  "
              f"[Σ zamówień: {suma_kupujacych:.2f} {waluta} - pobranie opłat Allegro: "
              f"{oplaty_rzeczywiste:.2f} {waluta} - zwroty: {suma_zwrotow_abs:.2f} {waluta}]")
        for s in kupujacy_szczegoly:
            print(f"    wpłata: {s['data']} | {s['kwota']:>8.2f} {s['waluta']} | {s['nazwa']}")
        for s in zwroty_szczegoly:
            print(f"    zwrot:  {s['data']} | {s['kwota']:>8.2f} {s['waluta']} | {s['nazwa']}")

        wiersze_csv_sklepu.append({
            "sklep": nazwa_sklepu,
            "data": data_wyplaty,
            "operator": nazwa_op,
            "kwota_przelewu": kwota_przelewu_opis,
            "waluta": waluta,
            "l_kupujacych": str(len(kupujacy)),
            "suma_zamowien": f"{suma_kupujacych:.2f}",
            "oplaty": f"{oplaty_rzeczywiste:.2f}",
            "zwroty": f"{suma_zwrotow_abs:.2f}",
            # nie trafiają do CSV (csv.DictWriter pisze tylko zdefiniowane
            # fieldnames) — używane przez frontend do pokazania szczegółów
            # po kliknięciu w wiersz
            "kupujacy_lista": kupujacy_szczegoly,
            "zwroty_lista": zwroty_szczegoly,
        })
        st = stats_operator_sklepu[klucz_stats]
        st["liczba_przelewow"] += 1
        st["suma_przelewow"] += kwota_wyplaty_abs
        st["suma_zamowien"] += suma_kupujacych
        st["suma_oplat"] += oplaty_rzeczywiste
        st["suma_zwrotow"] += suma_zwrotow_abs

        prev_time = czas_wyplaty


def rozlicz_sklep(nazwa_sklepu, auth_headers, date_od, date_do, miesiac_od, wyciag_przelewy):
    """
    Pobiera dane jednego sklepu/konta Allegro i dopasowuje jego wypłaty do
    WSPÓLNEJ puli wyciag_przelewy (współdzielonej między sklepami przez
    flagę "uzyta" — jeden wpis z wyciągu można przypisać tylko raz, do
    jednego sklepu/operatora/waluty — mutowana w miejscu przez wywołującego
    dla kolejnych sklepów).

    Dopasowanie odbywa się w DWÓCH przebiegach:
    1. Wypłaty w PLN — dopasowanie po kwocie + dacie (pewne, jak dotychczas).
    2. Wypłaty w innej walucie (sprzedaż zagraniczna, np. przez PayU na
       allegro.cz/sk/hu) — wyciąg mBank jest w PLN, więc nie da się dopasować
       po kwocie wprost. Dopasowanie po DACIE wśród wpisów z wyciągu, które
       NIE zostały już wykorzystane w przebiegu 1 (PLN ma pierwszeństwo, bo
       jest jednoznaczne), zweryfikowane kursem NBP z danego dnia: oczekiwana
       kwota PLN = kwota_oryginalna × kurs_NBP, musi się zgadzać z wpisem
       z wyciągu z tolerancją ±10% (Allegro/bank mogą użyć nieco innego kursu
       niż średni NBP tego dnia).

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
    # same dane trzy razy osobno z API.
    # Uwaga: bez filtra 'currency' — sprzedaż zagraniczna (allegro.cz/sk/hu
    # przez PayU) przychodzi w EUR/CZK/HUF, a to wcześniej było po cichu
    # odrzucane przez sztywny filtr "currency": "PLN" na tym zapytaniu.
    wszystkie_operacje = pobierz_wszystkie(
        f"{API_URL}/payments/payment-operations",
        {"limit": LIMIT, "occurredAt.gte": date_od, "occurredAt.lte": date_do},
        auth_headers,
    )
    wszystkie_operacje.sort(key=lambda x: x["occurredAt"])

    waluty_wystepujace = sorted(set(_waluta_op(o) for o in wszystkie_operacje))
    if waluty_wystepujace != ["PLN"]:
        print(f"[diagnostyka] waluty występujące w operacjach: {waluty_wystepujace}")

    suma_wplat = sum(float(o["value"]["amount"]) for o in wszystkie_operacje
                     if o.get("group") == "INCOME" and _waluta_op(o) == "PLN")
    print(f"Wpłaty od kupujących (PLN) {date_od[:10]} – {date_do[:10]}: "
          f"{sum(1 for o in wszystkie_operacje if o.get('group') == 'INCOME' and _waluta_op(o) == 'PLN')}"
          f"  |  Suma: {suma_wplat:.2f} PLN")

    operatory = sorted(set(operator_z_op(o) for o in wszystkie_operacje))

    wiersze_csv_sklepu = []
    stats_operator_sklepu = {}
    stan_faktur = {"dostepne": True}

    # ── PRZEBIEG 1: wypłaty w PLN — dopasowanie po kwocie + dacie ────────────
    for operator in operatory:
        ops_op = [o for o in wszystkie_operacje if operator_z_op(o) == operator]
        wplaty_pln    = [o for o in ops_op if o.get("group") == "INCOME" and _waluta_op(o) == "PLN"]
        zwroty_op_pln = [o for o in ops_op if o.get("group") == "REFUND" and _waluta_op(o) == "PLN"]

        # Wypłaty bankowe z miesiąca — dopasowane do wyciągu po KWOCIE i DACIE
        # LOKALNEJ (patrz data_lokalna) z dodatkową tolerancją ±1 dzień na
        # opóźnienie księgowania w banku (weekend/dzień roboczy).
        wyplaty_all = sorted(
            [o for o in ops_op if o.get("type") == "PAYOUT" and o["occurredAt"] >= miesiac_od
             and _waluta_op(o) == "PLN"],
            key=lambda x: x["occurredAt"]
        )
        wyplaty_dopasowane = []
        for o in wyplaty_all:
            kwota_abs = round(abs(float(o["value"]["amount"])), 2)
            data_api  = data_lokalna(o["occurredAt"])
            wpis = _znajdz_najblizszy(
                wyciag_przelewy, lambda k, kwota_abs=kwota_abs: k == kwota_abs, data_api
            )
            if wpis:
                wyplaty_dopasowane.append((o, wpis))

        if not wyplaty_dopasowane:
            continue

        nazwa_op = NAZWY_OPERATOROW.get(operator, operator)
        print(f"\n{'═'*60}")
        print(f"OPERATOR: {nazwa_op}  ({len(wyplaty_dopasowane)} przelewów bankowych, PLN)")
        print(f"{'═'*60}")

        wyplaty_przed = [o for o in ops_op if o.get("type") == "PAYOUT"
                         and o["occurredAt"] < miesiac_od and _waluta_op(o) == "PLN"]
        prev_time_start = wyplaty_przed[-1]["occurredAt"] if wyplaty_przed else date_od

        _przetworz_okna(nazwa_sklepu, nazwa_op, "PLN", wyplaty_dopasowane, wplaty_pln, zwroty_op_pln,
                        prev_time_start, auth_headers, stan_faktur, wiersze_csv_sklepu, stats_operator_sklepu)

    # ── PRZEBIEG 2: wypłaty w innej walucie — dopasowanie po dacie + kurs NBP ─
    # Tylko wśród wpisów z wyciągu, które NIE zostały wykorzystane w przebiegu
    # 1 (PLN dopasowuje się jednoznacznie po kwocie i ma pierwszeństwo).
    for operator in operatory:
        ops_op = [o for o in wszystkie_operacje if operator_z_op(o) == operator]
        waluty_obce = sorted(set(_waluta_op(o) for o in ops_op) - {"PLN"})

        for waluta in waluty_obce:
            wplaty_waluta    = [o for o in ops_op if o.get("group") == "INCOME" and _waluta_op(o) == waluta]
            zwroty_op_waluta = [o for o in ops_op if o.get("group") == "REFUND" and _waluta_op(o) == waluta]

            wyplaty_all = sorted(
                [o for o in ops_op if o.get("type") == "PAYOUT" and o["occurredAt"] >= miesiac_od
                 and _waluta_op(o) == waluta],
                key=lambda x: x["occurredAt"]
            )
            if not wyplaty_all:
                continue

            wyplaty_dopasowane = []
            for o in wyplaty_all:
                kwota_oryg_abs = round(abs(float(o["value"]["amount"])), 2)
                data_api = data_lokalna(o["occurredAt"])
                try:
                    kurs = kurs_nbp(waluta, data_api)
                except RuntimeError as e:
                    print(f"    UWAGA: {e} — pomijam dopasowanie wypłaty {waluta} {kwota_oryg_abs} "
                          f"z {data_api}, sprawdź ręcznie.")
                    continue
                oczekiwana_pln = kwota_oryg_abs * kurs
                wpis = _znajdz_najblizszy(
                    wyciag_przelewy,
                    lambda k, oczekiwana_pln=oczekiwana_pln:
                        abs(k - oczekiwana_pln) <= TOLERANCJA_KURSU * oczekiwana_pln,
                    data_api,
                )
                if wpis:
                    wyplaty_dopasowane.append((o, wpis))
                else:
                    print(f"    UWAGA: nie znaleziono w wyciągu przelewu pasującego do wypłaty "
                          f"{waluta} {kwota_oryg_abs} z {data_api} (oczekiwano ok. {oczekiwana_pln:.2f} PLN "
                          f"wg kursu NBP {kurs}) — sprawdź ręcznie.")

            if not wyplaty_dopasowane:
                continue

            nazwa_op = NAZWY_OPERATOROW.get(operator, operator)
            print(f"\n{'═'*60}")
            print(f"OPERATOR: {nazwa_op}  ({len(wyplaty_dopasowane)} przelewów bankowych, {waluta})")
            print(f"{'═'*60}")

            wyplaty_przed = [o for o in ops_op if o.get("type") == "PAYOUT"
                             and o["occurredAt"] < miesiac_od and _waluta_op(o) == waluta]
            prev_time_start = wyplaty_przed[-1]["occurredAt"] if wyplaty_przed else date_od

            _przetworz_okna(nazwa_sklepu, nazwa_op, waluta, wyplaty_dopasowane, wplaty_waluta,
                            zwroty_op_waluta, prev_time_start, auth_headers, stan_faktur,
                            wiersze_csv_sklepu, stats_operator_sklepu)

    suma_zwrotow = sum(float(o["value"]["amount"]) for o in wszystkie_operacje
                       if o.get("group") == "REFUND" and _waluta_op(o) == "PLN")
    l_zwrotow = sum(1 for o in wszystkie_operacje
                    if o.get("group") == "REFUND" and _waluta_op(o) == "PLN")
    print(f"\nZwroty PLN {date_od[:10]} – {date_do[:10]}: {l_zwrotow}  |  Suma: {suma_zwrotow:.2f} PLN")

    return wiersze_csv_sklepu, stats_operator_sklepu, wszystkie_operacje
