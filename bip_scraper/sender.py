"""
Wysyłanie zebranych wpisów BIP do agenta AI (webhook/API).
Payload jest przygotowany pod przerobienie na artykuł WordPress.
"""
import json
from typing import Any

import requests

from .scraper import BIPEntry


def build_payload(entries: list[BIPEntry], instruction: str | None = None) -> dict:
    """
    Buduje jeden payload do wysłania do agenta.
    Agent może na tej podstawie wygenerować artykuł WordPress.
    """
    body = {
        "entries": [e.to_payload() for e in entries],
        "instruction": instruction or (
            "Na podstawie powyższych wpisów z BIP przygotuj artykuł "
            "nadający się do publikacji na WordPressie (tytuł, lead, treść)."
        ),
    }
    return body


def send_to_agent(
    entries: list[BIPEntry],
    webhook_url: str,
    *,
    api_key: str | None = None,
    api_key_header: str = "Authorization",
    instruction: str | None = None,
    timeout: int = 30,
) -> requests.Response:
    """
    Wysyła zebrane wpisy do agenta AI (POST JSON).
    """
    if not webhook_url:
        raise ValueError("Brak webhook_url w konfiguracji agenta.")
    payload = build_payload(entries, instruction=instruction)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if api_key and api_key_header:
        if api_key_header.lower() == "authorization" and not api_key.lower().startswith("bearer "):
            api_key = f"Bearer {api_key}"
        headers[api_key_header] = api_key

    return requests.post(
        webhook_url,
        json=payload,
        headers=headers,
        timeout=timeout,
    )


def save_payload_to_file(entries: list[BIPEntry], path: str, instruction: str | None = None) -> None:
    """Zapisuje payload do pliku JSON (np. do ręcznego przekazania agentowi)."""
    payload = build_payload(entries, instruction=instruction)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
