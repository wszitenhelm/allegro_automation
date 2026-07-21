"""
Opcjonalne podsumowanie tekstowe rozliczenia (Anthropic API).

Dostaje WYŁĄCZNIE zagregowane liczby per sklep/operator — nigdy treść
wyciągu ani dane osobowe kupujących (patrz pdf_parser.py). Zwraca None
(i drukuje powód) jeśli brak klucza API/pakietu, żeby reszta programu
(CSV) działała niezależnie od tego kroku.
"""
import os
import json


def generuj_podsumowanie_llm(stats):
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
        "miesięcznego rozliczenia Allegro Finance, per sklep i operator płatności: "
        "liczba przelewów bankowych, suma przelewów, suma zamówień kupujących, "
        "suma pobranych opłat Allegro, suma zwrotów. Napisz 2-3 zdania "
        "podsumowania po polsku dla księgowej — ile przelewów, na jakie kwoty, "
        "jakie opłaty i zwroty w danym miesiącu.\n\n"
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
