"""Cienki klient Allegro API: OAuth device flow + pobieranie z paginacją."""
import sys
import time
import requests

from config import BASE_URL, HEADERS


def zainicjuj_device_flow(client_id, client_secret):
    """Krok 1 OAuth device flow: zwraca dict z 'verification_uri_complete', 'device_code', 'interval'."""
    r = requests.post(
        f"{BASE_URL}/auth/oauth/device",
        auth=(client_id, client_secret),
        data={"client_id": client_id},
    )
    r.raise_for_status()
    return r.json()


def czekaj_na_token(client_id, client_secret, device, nazwa_sklepu="sklep"):
    """
    Krok 2 OAuth device flow: odpytuje o token dopóki użytkownik nie zatwierdzi
    dostępu w przeglądarce (albo nie skończy się limit prób). Zwraca auth_headers.
    Wspólne dla CLI (autoryzuj) i frontendu (app.py), które różnią się tylko tym,
    jak proszą użytkownika o kliknięcie w link.
    """
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
            return {**HEADERS, "Authorization": f"Bearer {data['access_token']}"}
        if data.get("error") == "authorization_pending":
            time.sleep(interval)
        else:
            sys.exit(f"[{nazwa_sklepu}] Błąd autoryzacji: {data}")


def autoryzuj(nazwa_sklepu, client_id, client_secret):
    """Device-code OAuth flow dla jednego sklepu/konta Allegro (wersja CLI). Zwraca auth_headers."""
    print(f"\n[{nazwa_sklepu}] Pobieram kod autoryzacji...")
    device = zainicjuj_device_flow(client_id, client_secret)

    print(f">>> [{nazwa_sklepu}] Wejdź na: {device['verification_uri_complete']}")
    print(">>> Zatwierdź dostęp w przeglądarce, a potem wróć tutaj i naciśnij Enter.")
    input()

    print(f"[{nazwa_sklepu}] Pobieram token...")
    auth_headers = czekaj_na_token(client_id, client_secret, device, nazwa_sklepu)
    print(f"[{nazwa_sklepu}] Token OK.\n")
    return auth_headers


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
