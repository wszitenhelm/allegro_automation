"""
Generuje kopię wgranego wyciągu PDF z zielonym podświetleniem linii, które
zostały dopasowane do wypłat z Allegro (patrz rozliczenie.py) — żeby
księgowa mogła jednym rzutem oka zobaczyć, które wpisy są już rozliczone,
a które (bez podświetlenia) trzeba jeszcze sprawdzić ręcznie.

Celowo NIE re-implementuje wykrywania przelewów z pdf_parser.py (to
najbardziej dopracowana i przetestowana część systemu — nie chcemy dwóch
niezależnych, mogących się rozjechać implementacji tej samej logiki).
Zamiast tego, dla każdego wpisu już oznaczonego jako "uzyta" (czyli
dopasowanego), szuka w PDF-ie (przez PyMuPDF/fitz) linii zaczynającej się
od tej samej daty, której PIERWSZA liczba zgadza się z tą samą kwotą —
dokładnie ta sama reguła, co przy parsowaniu (patrz DATA_LINIA_RE i
kwota_z_liczb w pdf_parser.py, importowane tutaj, nie kopiowane).
"""
import fitz  # pip install pymupdf

from pdf_parser import DATA_LINIA_RE, KWOTA_RE, kwota_z_liczb

# Jasnozielony marker — na tyle jasny, żeby tekst pod spodem zostawał
# czytelny, ale wystarczająco widoczny, żeby wyłapać wzrokiem od razu.
KOLOR_PODSWIETLENIA = (0.80, 1.0, 0.80)

TOLERANCJA_Y = 3  # punkty PDF — rozstrzyga czy dwa fragmenty tekstu są "w tym samym wierszu"

# Odstęp pionowy (punkty PDF) między KOLEJNYMI wizualnymi liniami tekstu.
# Zmierzone na prawdziwym wyciągu: linie NALEŻĄCE do tej samej transakcji
# (data → nazwa kontrahenta → numer konta → tytuł przelewu) mają odstęp
# ok. 0.7pt, a przejście do NASTĘPNEJ transakcji (albo do stopki strony)
# to ok. 7-8pt. Próg 3pt wyraźnie rozdziela te dwa przypadki.
MAX_PRZERWA_W_WIERSZU = 3


def _linie_strony(page):
    """
    Zwraca listę (bbox, tekst) dla każdego WIZUALNEGO wiersza tekstu na
    stronie — odpowiednik jednej linii z pdftotext -layout, ale z pozycją
    (bbox), której pdftotext nie daje.

    Uwaga: fitz grupuje tekst w "linie" na poziomie struktury PDF (bloki/
    linie z get_text("dict")), NIE wizualnie — data i kwota w tym samym
    wierszu tabeli są tu często osobnymi obiektami tekstowymi (osobne
    kolumny), więc trafiają do różnych "linii" fitza mimo tej samej
    pozycji Y. Dlatego budujemy wiersze sami: zbieramy WSZYSTKIE fragmenty
    tekstu (spany) ze strony i grupujemy je po środku Y z tolerancją
    TOLERANCJA_Y, sortując w każdej grupie po X (lewo→prawo) — to
    odtwarza to, co pdftotext -layout robi automatycznie.
    """
    spany = [
        s
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for s in line.get("spans", [])
        if s["text"].strip()
    ]
    spany.sort(key=lambda s: ((s["bbox"][1] + s["bbox"][3]) / 2, s["bbox"][0]))

    grupy = []
    grupa_biezaca = []
    y_ref = None
    for s in spany:
        y_mid = (s["bbox"][1] + s["bbox"][3]) / 2
        if y_ref is not None and abs(y_mid - y_ref) > TOLERANCJA_Y:
            grupy.append(grupa_biezaca)
            grupa_biezaca = []
            y_ref = None
        grupa_biezaca.append(s)
        if y_ref is None:
            y_ref = y_mid
    if grupa_biezaca:
        grupy.append(grupa_biezaca)

    linie = []
    for grupa in grupy:
        grupa.sort(key=lambda s: s["bbox"][0])
        tekst = " ".join(s["text"] for s in grupa)
        x0 = min(s["bbox"][0] for s in grupa)
        y0 = min(s["bbox"][1] for s in grupa)
        x1 = max(s["bbox"][2] for s in grupa)
        y1 = max(s["bbox"][3] for s in grupa)
        linie.append(((x0, y0, x1, y1), tekst))
    return linie


def _zasieg_calego_wiersza(linie, i):
    """
    Wpis w wyciągu mBank zajmuje kilka wizualnych linii (data+kwota, potem
    nazwa kontrahenta, numer konta, tytuł przelewu...) — linie[i] to tylko
    ta PIERWSZA (z datą). Ta funkcja zwraca bbox obejmujący WSZYSTKIE linie
    należące do tego samego wpisu: idzie w dół od linii[i] dopóki odstęp do
    kolejnej linii jest "normalny" (patrz MAX_PRZERWA_W_WIERSZU) i dopóki
    kolejna linia sama nie zaczyna nowego wpisu (własna data na początku).
    """
    (x0, y0, x1, y1), _ = linie[i]
    min_x0, max_x1 = x0, x1
    ostatnie_y1 = y1
    for j in range(i + 1, len(linie)):
        (nx0, ny0, nx1, ny1), ntekst = linie[j]
        if DATA_LINIA_RE.match(ntekst) or (ny0 - ostatnie_y1) > MAX_PRZERWA_W_WIERSZU:
            break
        min_x0 = min(min_x0, nx0)
        max_x1 = max(max_x1, nx1)
        y1 = ny1
        ostatnie_y1 = ny1
    return (min_x0, y0, max_x1, y1)


def zaznacz_dopasowane(sciezka_pdf, wyciag_przelewy):
    """
    Zwraca bajty PDF (kopia sciezka_pdf) z zielonym podświetleniem wierszy
    odpowiadających wpisom z wyciag_przelewy, które mają "uzyta"=True.
    Wpisy bez dopasowania (uzyta=False) zostają bez zmian — brak
    podświetlenia to sygnał dla księgowej "sprawdź to ręcznie".

    Zwraca None, jeśli nic nie zostało dopasowane (nie ma czego podświetlać).
    """
    dopasowane = [w for w in wyciag_przelewy if w["uzyta"]]
    if not dopasowane:
        return None

    # Ta sama para (data, kwota) może się powtórzyć — np. dwa różne
    # przelewy tego samego dnia na tę samą kwotę — więc liczymy ile razy
    # dany klucz trzeba jeszcze znaleźć, zamiast szukać tylko pierwszego
    # wystąpienia (co podświetliłoby dwa razy tę samą linię).
    do_znalezienia = {}
    for w in dopasowane:
        klucz = (w["data"], w["kwota"])
        do_znalezienia[klucz] = do_znalezienia.get(klucz, 0) + 1

    doc = fitz.open(sciezka_pdf)
    try:
        for page in doc:
            if not any(do_znalezienia.values()):
                break
            linie = _linie_strony(page)
            for i, ((x0, y0, x1, y1), tekst) in enumerate(linie):
                dopasowanie_daty = DATA_LINIA_RE.match(tekst)
                if not dopasowanie_daty:
                    continue
                liczby = kwota_z_liczb(KWOTA_RE.findall(tekst))
                if not liczby:
                    continue
                klucz = (dopasowanie_daty.group(1), liczby[0])
                if do_znalezienia.get(klucz, 0) <= 0:
                    continue
                do_znalezienia[klucz] -= 1

                rx0, ry0, rx1, ry1 = _zasieg_calego_wiersza(linie, i)
                rect = fitz.Rect(rx0 - 1, ry0 - 1, rx1 + 1, ry1 + 1)
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=KOLOR_PODSWIETLENIA)
                annot.update()
        return doc.tobytes()
    finally:
        doc.close()
