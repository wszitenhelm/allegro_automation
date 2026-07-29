"""
Frontend (Streamlit) do rozliczeń Allegro Finance — wgraj wyciąg, wybierz
miesiąc, dostań gotowe rozliczenie do zaksięgowania.

Uruchomienie lokalne:
  streamlit run app.py

Sekrety (ALLEGRO_*_CLIENT_ID/SECRET, APP_PASSWORD) czytane są z
.streamlit/secrets.toml lokalnie, albo z panelu "Secrets" na Streamlit
Community Cloud po wdrożeniu — patrz .streamlit/secrets.toml.example.
"""
import hmac
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
from pdf_highlight import zaznacz_dopasowane
from pdf_parser import parsuj_pdf_mbank
from rozliczenie import rozlicz_sklep

MIESIACE = [
    "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
    "lipiec", "sierpień", "wrzesień", "październik", "listopad", "grudzień",
]

st.set_page_config(
    page_title="Rozliczenia Allegro Finance",
    page_icon="logo.png" if Path("logo.png").exists() else None,
    layout="wide",
)

# Ukrywa wbudowaną ikonkę Streamlita "aplikacja się wykonuje" (biegnący
# ludzik) w prawym górnym rogu — nie ma do tego oficjalnego przełącznika,
# tylko przez CSS na stałym data-testid.
st.markdown(
    "<style>[data-testid='stStatusWidget'] {visibility: hidden;}</style>",
    unsafe_allow_html=True,
)


# ── ochrona hasłem ────────────────────────────────────────────────────────────
def sprawdz_haslo():
    haslo_wymagane = os.environ.get("APP_PASSWORD")
    if not haslo_wymagane:
        return True  # brak ustawionego hasła = brak ochrony (np. lokalnie)
    if st.session_state.get("zalogowany"):
        return True

    st.markdown("### Dostęp chroniony")
    haslo = st.text_input("Hasło", type="password")
    if st.button("Zaloguj"):
        # hmac.compare_digest zamiast == — porównanie w stałym czasie, żeby
        # różnica w czasie odpowiedzi nie zdradzała ile znaków hasła zgadza się
        # z prawdziwym (standardowa praktyka przy porównywaniu sekretów).
        if hmac.compare_digest(haslo, haslo_wymagane):
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
st.info(
    "**Zanim wgrasz wyciąg:** zaloguj się na "
    "[allegro.pl/logowanie](https://allegro.pl/logowanie) do sklepów "
    "**pigmejka** i **decor4** — każdy w osobnej przeglądarce (np. pigmejka w "
    "Safari, decor4 w Chrome). Dzięki temu autoryzacja poniżej zadziała bez "
    "przełączania się między kontami."
)

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

# Przycisk zablokowany dopóki nie ma wgranego pliku i przynajmniej jednego
# wybranego sklepu — nie ma sensu odpalać rozliczenia bez tych dwóch rzeczy.
rozlicz_kliknieto = st.button("Rozlicz", type="primary", disabled=plik is None or not sklepy)


def autoryzuj_w_appce(nazwa_sklepu, client_id, client_secret, status):
    """
    Wersja OAuth device flow dopasowana do Streamlit: link zamiast input().
    Różne sklepy mogą być zalogowane w różnych przeglądarkach (np. pigmejka
    w Safari, decor4 w Chrome) — nie wystarczy przycisk, który otwiera link
    w BIEŻĄCEJ przeglądarce. Link jest więc też dostępny do skopiowania (pod
    "Inna przeglądarka?"), żeby dało się go wkleić tam gdzie odpowiednie
    konto jest zalogowane.
    """
    device = zainicjuj_device_flow(client_id, client_secret)
    with status.container(border=True):
        st.markdown(f"**{nazwa_sklepu}** — zatwierdź dostęp w Allegro")
        st.caption(
            "Otwórz w przeglądarce, w której jesteś zalogowana na to konto, "
            "i zatwierdź dostęp — wracam tu automatycznie."
        )
        col_btn, col_link = st.columns([1, 1])
        col_btn.link_button(
            "Zatwierdź dostęp →", device["verification_uri_complete"], type="primary"
        )
        with col_link.popover("Inna przeglądarka?"):
            st.caption("Skopiuj link i wklej go tam, gdzie jesteś zalogowana:")
            st.code(device["verification_uri_complete"], language=None)
    with st.spinner(f"Czekam na zatwierdzenie dostępu dla {nazwa_sklepu}..."):
        return czekaj_na_token(client_id, client_secret, device, nazwa_sklepu)


if rozlicz_kliknieto and plik is not None:
    # plik.read() daje bajty wgranego pliku w pamięci, ale parsuj_pdf_mbank
    # (pdftotext) i zaznacz_dopasowane (fitz) potrzebują ścieżki do
    # prawdziwego pliku na dysku — stąd zapis do tymczasowego pliku,
    # usuwanego na końcu w bloku finally.
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(plik.read())
        sciezka_pdf = tmp.name

    try:
        with st.status("Przetwarzam wyciąg...", expanded=True) as status:
            status.markdown("Parsuję wyciąg PDF...")
            try:
                wyciag_przelewy = parsuj_pdf_mbank(sciezka_pdf)
            except RuntimeError as e:
                status.update(label="Błąd parsowania PDF", state="error")
                st.error(str(e))
                st.stop()
            status.markdown(
                f"Znaleziono **{len(wyciag_przelewy)}** przelewów Allegro Finance w wyciągu."
            )

            date_od, date_do, miesiac_od = zakres_dat(int(rok), int(miesiac))

            wiersze_csv = []

            for sklep in sklepy:
                status.divider()
                try:
                    auth_headers = autoryzuj_w_appce(
                        sklep["nazwa"], sklep["client_id"], sklep["client_secret"], status
                    )
                except RuntimeError as e:
                    status.update(label="Błąd autoryzacji Allegro", state="error")
                    st.error(str(e))
                    st.stop()
                with st.spinner(f"Pobieram i dopasowuję dane dla {sklep['nazwa']}..."):
                    wiersze, _stats, _operacje = rozlicz_sklep(
                        sklep["nazwa"], auth_headers, date_od, date_do, miesiac_od, wyciag_przelewy
                    )
                wiersze_csv.extend(wiersze)
                status.markdown(f"**{sklep['nazwa']}** gotowe.")

            # sortowanie w TEJ SAMEJ kolejności co na wyciągu bankowym (nie
            # tylko po dacie — kilka przelewów tego samego dnia ma inaczej
            # niedeterministyczną kolejność względem siebie, bo bez tego
            # zostają w kolejności w jakiej przetwarzane były sklepy/operatory)
            wiersze_csv.sort(key=lambda w: w["linia_idx"])

            # W tym momencie każdy wpis w wyciag_przelewy ma już ustaloną
            # ostateczną flagę "uzyta" (po wszystkich sklepach) — dokładnie to,
            # czego potrzeba do podświetlenia PDF-a. Błąd tutaj (np. plik PDF
            # w nietypowym formacie, którego fitz nie otworzy) nie powinien
            # zablokować dostępu do gotowej tabeli/CSV, więc tylko ostrzegamy.
            status.markdown("Przygotowuję podświetlony PDF...")
            try:
                pdf_podswietlony = zaznacz_dopasowane(sciezka_pdf, wyciag_przelewy)
            except Exception as e:
                status.markdown(
                    f"Nie udało się przygotować podświetlonego PDF ({e}) — "
                    "tabela i CSV działają normalnie."
                )
                pdf_podswietlony = None

            status.update(label="Gotowe!", state="complete")

        st.session_state["wyniki"] = {
            "wiersze_csv": wiersze_csv,
            "miesiac_od": miesiac_od,
            "pdf_podswietlony": pdf_podswietlony,
        }
    finally:
        os.unlink(sciezka_pdf)


# ── wyniki ────────────────────────────────────────────────────────────────────
# Wynik trzymany w session_state (nie w zwykłej zmiennej) — Streamlit od
# nowa wykonuje CAŁY skrypt przy każdej interakcji (np. kliknięcie wiersza
# tabeli niżej), więc bez tego tabela i przyciski pobrania znikałyby po
# każdym takim kliknięciu zamiast zostać na ekranie.
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

    # zdarzenie.selection.rows to lista indeksów zaznaczonych wierszy tabeli
    # (dzięki on_select="rerun" wyżej) — przy selection_mode="single-row"
    # ma co najwyżej jeden element.
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

    col_csv, col_pdf = st.columns(2)

    csv_bytes = df_widok.to_csv(index=False).encode("utf-8")
    col_csv.download_button(
        "Pobierz CSV (do księgowej)",
        data=csv_bytes,
        file_name=f"rozliczenie_{wyniki['miesiac_od'][:7]}.csv",
        mime="text/csv",
    )

    if wyniki.get("pdf_podswietlony"):
        col_pdf.download_button(
            "Pobierz wyciąg z zaznaczeniami (PDF)",
            data=wyniki["pdf_podswietlony"],
            file_name=f"wyciag_zaznaczony_{wyniki['miesiac_od'][:7]}.pdf",
            mime="application/pdf",
        )
        col_pdf.caption("Zielone wiersze = już rozliczone. Reszta — do sprawdzenia ręcznie.")
