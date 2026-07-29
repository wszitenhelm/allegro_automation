"""
Kursy walut NBP (Narodowy Bank Polski) — publiczne, darmowe API, standard
w polskiej księgowości. Używane do sprawdzenia czy dopasowanie (po samej
dacie) wypłaty w obcej walucie do wpisu z wyciągu ma sens kwotowo.
"""
import requests
from datetime import timedelta

_cache = {}


def kurs_nbp(waluta, dzien):
    """
    Zwraca średni kurs NBP (tabela A) danej waluty na dany dzień (datetime.date).
    NBP nie publikuje kursów w weekendy/święta — w takim wypadku cofa się
    dzień po dniu (max 7 prób) do najbliższego wcześniejszego dnia roboczego.
    Wynik cache'owany w pamięci procesu (ta sama waluta/dzień pyta NBP tylko raz).
    """
    klucz = (waluta, dzien)
    if klucz in _cache:
        return _cache[klucz]

    d = dzien
    for _ in range(7):
        r = requests.get(
            f"https://api.nbp.pl/api/exchangerates/rates/A/{waluta}/{d.isoformat()}/",
            params={"format": "json"},
        )
        if r.status_code == 200:
            kurs = r.json()["rates"][0]["mid"]
            _cache[klucz] = kurs
            return kurs
        d -= timedelta(days=1)
    raise RuntimeError(f"Brak kursu NBP dla {waluta} w okolicach {dzien}")
