"""
Frontend (Streamlit) do rozliczeń Allegro Finance — wgraj wyciąg, wybierz
miesiąc, dostań gotowe rozliczenie do zaksięgowania.

Uruchomienie lokalne:
  streamlit run app.py

Sekrety (ALLEGRO_*_CLIENT_ID/SECRET, ANTHROPIC_API_KEY, APP_PASSWORD)
czytane są z .streamlit/secrets.toml lokalnie, albo z panelu "Secrets" na
Streamlit Community Cloud po wdrożeniu — patrz .streamlit/secrets.toml.example.
"""
import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

# Sekrety z panelu Streamlit trafiają do os.environ, żeby config.py (oparty
# na zmiennych środowiskowych, tak samo jak wersja CLI) działał bez zmian.
# Brak pliku secrets.toml (np. lokalnie, z samym .env) jest tu normalny.
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(_k, str(_v))
except FileNotFoundError:
    pass

from allegro_api import zainicjuj_device_flow, czekaj_na_token
from config import wczytaj_sklepy, zakres_dat, KOLUMNY_WYNIKU, NAZWY_KOLUMN_WYNIKU
from llm_summary import generuj_podsumowanie_llm
from pdf_parser import parsuj_pdf_mbank
from rozliczenie import rozlicz_sklep

MIESIACE = [
    "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
    "lipiec", "sierpień", "wrzesień", "październik", "listopad", "grudzień",
]

st.set_page_config(
    page_title="Rozliczenia Allegro Finance",
    page_icon="logo.png" if Path("logo.png").exists() else "📊",
    layout="wide",
)


# ── ochrona hasłem ────────────────────────────────────────────────────────────
def sprawdz_haslo():
    haslo_wymagane = os.environ.get("APP_PASSWORD")
    if not haslo_wymagane:
        return True  # brak ustawionego hasła = brak ochrony (np. lokalnie)
    if st.session_state.get("zalogowany"):
        return True

    st.markdown("### 🔒 Dostęp chroniony")
    haslo = st.text_input("Hasło", type="password")
    if st.button("Zaloguj"):
        if haslo == haslo_wymagane:
            st.session_state["zalogowany"] = True
            st.rerun()
        else:
            st.error("Złe hasło.")
    return False


if not sprawdz_haslo():
    st.stop()


# ── nagłówek ──────────────────────────────────────────────────────────────────
if Path("logo.png").exists():
    st.image("logo.png", width=320)
st.title("Rozliczenia Allegro Finance")
st.markdown("#### mniej czasu na księgowanie = więcej czasu z rodziną")

st.divider()

sklepy_wszystkie = wczytaj_sklepy()
if not sklepy_wszystkie:
    st.error(
        "Brak skonfigurowanych sklepów. Ustaw w Secrets co najmniej "
        "`ALLEGRO_PIGMEJKA_CLIENT_ID` / `ALLEGRO_PIGMEJKA_CLIENT_SECRET`."
    )
    st.stop()


# ── formularz: wyciąg + miesiąc + sklepy ─────────────────────────────────────
plik = st.file_uploader("Wgraj wyciąg bankowy (PDF)", type="pdf")

col_rok, col_miesiac = st.columns(2)
rok = col_rok.number_input("Rok", min_value=2020, max_value=2100, value=2025, step=1)
miesiac_nazwa = col_miesiac.selectbox("Miesiąc", MIESIACE, index=10)
miesiac = MIESIACE.index(miesiac_nazwa) + 1

nazwy_wybrane = st.multiselect(
    "Które sklepy rozliczyć?",
    options=[s["nazwa"] for s in sklepy_wszystkie],
    default=[s["nazwa"] for s in sklepy_wszystkie],
)
sklepy = [s for s in sklepy_wszystkie if s["nazwa"] in nazwy_wybrane]

if sklepy and len(sklepy) < len(sklepy_wszystkie):
    st.info(
        "Rozliczasz tylko wybrane sklepy z tego wyciągu. Przelewy należące do "
        "pominiętych sklepów (" + ", ".join(
            s["nazwa"] for s in sklepy_wszystkie if s["nazwa"] not in nazwy_wybrane
        ) + ") po prostu nie pojawią się w wyniku, bo nie są sprawdzane."
    )

rozlicz_kliknieto = st.button("Rozlicz", type="primary", disabled=plik is None or not sklepy)


def autoryzuj_w_appce(nazwa_sklepu, client_id, client_secret, status):
    """Wersja OAuth device flow dopasowana do Streamlit: link zamiast input()."""
    device = zainicjuj_device_flow(client_id, client_secret)
    status.write(
        f"**{nazwa_sklepu}**: otwórz link i zatwierdź dostęp w Allegro, "
        f"potem wróć tutaj — czekam automatycznie."
    )
    status.link_button(
        f"Zatwierdź dostęp do {nazwa_sklepu} →",
        device["verification_uri_complete"],
    )
    with st.spinner(f"Czekam na zatwierdzenie dostępu dla {nazwa_sklepu}..."):
        return czekaj_na_token(client_id, client_secret, device, nazwa_sklepu)


if rozlicz_kliknieto and plik is not None:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(plik.read())
        sciezka_pdf = tmp.name

    try:
        with st.status("Przetwarzam wyciąg...", expanded=True) as status:
            status.write("Parsuję wyciąg PDF...")
            try:
                wyciag_przelewy = parsuj_pdf_mbank(sciezka_pdf)
            except RuntimeError as e:
                status.update(label="Błąd parsowania PDF", state="error")
                st.error(str(e))
                st.stop()
            status.write(f"Znaleziono {len(wyciag_przelewy)} przelewów Allegro Finance w wyciągu.")

            date_od, date_do, miesiac_od = zakres_dat(int(rok), int(miesiac))

            wiersze_csv = []
            stats_wszystkie = {}

            for sklep in sklepy:
                status.write(f"Logowanie do **{sklep['nazwa']}**...")
                auth_headers = autoryzuj_w_appce(
                    sklep["nazwa"], sklep["client_id"], sklep["client_secret"], status
                )
                status.write(f"Pobieram i dopasowuję dane dla **{sklep['nazwa']}**...")
                wiersze, stats, _ = rozlicz_sklep(
                    sklep["nazwa"], auth_headers, date_od, date_do, miesiac_od, wyciag_przelewy
                )
                wiersze_csv.extend(wiersze)
                for operator, dane in stats.items():
                    stats_wszystkie[(sklep["nazwa"], operator)] = dane
                status.write(f"✅ {sklep['nazwa']} gotowe.")

            status.update(label="Gotowe!", state="complete")

        st.session_state["wyniki"] = {
            "wiersze_csv": wiersze_csv,
            "stats_wszystkie": stats_wszystkie,
            "miesiac_od": miesiac_od,
        }
    finally:
        os.unlink(sciezka_pdf)


# ── wyniki ────────────────────────────────────────────────────────────────────
if "wyniki" in st.session_state:
    wyniki = st.session_state["wyniki"]
    wiersze_csv = wyniki["wiersze_csv"]

    st.divider()
    st.subheader("Wynik rozliczenia")

    df_widok = pd.DataFrame(wiersze_csv)[KOLUMNY_WYNIKU].rename(columns=NAZWY_KOLUMN_WYNIKU)

    st.caption("Kliknij w wiersz, żeby zobaczyć listę kupujących i zwrotów dla tego przelewu.")
    zdarzenie = st.dataframe(
        df_widok,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    wybrane = zdarzenie.selection.rows if zdarzenie and zdarzenie.selection else []
    if wybrane:
        wiersz = wiersze_csv[wybrane[0]]
        st.markdown(
            f"**Szczegóły: {wiersz['data']} | {wiersz['kwota_przelewu']} PLN | {wiersz['sklep']}**"
        )
        col_kupujacy, col_zwroty = st.columns(2)
        with col_kupujacy:
            st.markdown("**Kupujący**")
            lista = wiersz.get("kupujacy_lista") or []
            if lista:
                st.dataframe(pd.DataFrame(lista), hide_index=True, use_container_width=True)
            else:
                st.caption("Brak kupujących w tym oknie.")
        with col_zwroty:
            st.markdown("**Zwroty**")
            lista = wiersz.get("zwroty_lista") or []
            if lista:
                st.dataframe(pd.DataFrame(lista), hide_index=True, use_container_width=True)
            else:
                st.caption("Brak zwrotów w tym oknie.")

    csv_bytes = df_widok.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Pobierz CSV (do księgowej)",
        data=csv_bytes,
        file_name=f"rozliczenie_{wyniki['miesiac_od'][:7]}.csv",
        mime="text/csv",
    )

    stats_dla_llm = [
        {"sklep": sklep, "operator": operator, **dane}
        for (sklep, operator), dane in wyniki["stats_wszystkie"].items()
    ]
    podsumowanie = generuj_podsumowanie_llm(stats_dla_llm)
    if podsumowanie:
        st.info(podsumowanie)
