"""
Parsowanie wyciągu PDF z mBank — czysta logika, bez side-effectów poza
odczytem pliku. Zgłasza RuntimeError zamiast sys.exit(), żeby dało się to
wywołać z innego procesu (np. z przyszłego frontendu) bez zabijania go.

Uwaga (decyzja projektowa): treść wyciągu bankowego jest przetwarzana
wyłącznie lokalnie (pdftotext + regex) i nigdy nie jest wysyłana do żadnego
zewnętrznego LLM/API. Wyciąg zawiera dane wrażliwe (IBAN, kontrahenci spoza
Allegro). Jedyne dane trafiające do LLM (patrz llm_summary.py) to już
zagregowane liczby per sklep/operator — nigdy surowy tekst wyciągu ani dane
osobowe kupujących.
"""
import re
import subprocess

UUID_RE = re.compile(
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
        raise RuntimeError(
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
        raise RuntimeError(
            "Nie znaleziono przelewów Allegro Finance w pliku PDF.\n"
            "Upewnij się że to wyciąg mBank z przelewami Allegro Finance."
        )

    podglad = sorted((p["data"], p["kwota"]) for p in przelewy)
    print(f"[PDF] Znaleziono {len(przelewy)} przelewów Allegro Finance: {podglad}")
    return przelewy
