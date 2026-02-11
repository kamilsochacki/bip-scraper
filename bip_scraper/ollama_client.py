"""
Klient Ollama (lokalnie) – analiza wpisów BIP przez Bielika
i generowanie artykułu WordPress.
"""
import json
from typing import Any

import requests

from .scraper import BIPEntry


def _ollama_generate_legacy(base_url: str, model: str, prompt: str, system: str | None, stream: bool, timeout: int) -> str:
    """POST /api/generate (klasyczne API Ollama)."""
    root = base_url.rstrip("/")
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": stream}
    if system:
        payload["system"] = system
    r = requests.post(f"{root}/api/generate", json=payload, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def _ollama_chat(base_url: str, model: str, prompt: str, system: str | None, stream: bool, timeout: int) -> str:
    """POST /api/chat (API czatu – fallback gdy /api/generate zwraca 404)."""
    root = base_url.rstrip("/")
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    r = requests.post(
        f"{root}/api/chat",
        json={"model": model, "messages": messages, "stream": stream},
        timeout=timeout,
    )
    r.raise_for_status()
    msg = r.json().get("message") or {}
    return (msg.get("content") or "").strip()


def ollama_generate(
    base_url: str,
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    stream: bool = False,
    timeout: int = 300,
) -> str:
    """
    Wywołuje model przez API Ollama. Próbuje /api/generate,
    przy 404 używa /api/chat (niektóre instalacje/proxy mają tylko chat).
    """
    try:
        return _ollama_generate_legacy(base_url, model, prompt, system, stream, timeout)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return _ollama_chat(base_url, model, prompt, system, stream, timeout)
        raise


def entries_to_text(entries: list[BIPEntry], max_title_len: int = 120) -> str:
    """Formatuje listę wpisów do czytelnego tekstu dla modelu."""
    lines = []
    for i, e in enumerate(entries, 1):
        title = (e.title[: max_title_len] + "...") if len(e.title) > max_title_len else e.title
        lines.append(f"{i}. [{title}]\n   Źródło: {e.source_name}\n   URL: {e.url}")
        if e.published:
            lines.append(f"   Data: {e.published}")
        if e.summary:
            lines.append(f"   Skrót: {e.summary[:200]}...")
        lines.append("")
    return "\n".join(lines)


SYSTEM_ANALIZA = """Jesteś ekspertem od informacji publicznych (BIP). Twoim zadaniem jest wybór informacji istotnych dla mieszkańców powiatu (gminy, miasta). Zwracaj uwagę na: uchwały i zarządzenia wpływające na codzienne życie, przetargi i zamówienia publiczne, konsultacje społeczne, obwieszczenia, zmiany w prawie miejscowym, nabory na stanowiska, sprawy środowiska i planowania. Pomijaj wewnętrzne procedury, czysto techniczne zmiany i powtórzenia."""

PROMPT_ANALIZA = """Poniżej lista wpisów z rejestrów zmian kilku BIP-ów (Biuletynów Informacji Publicznych) z powiatu kamieńskiego.

Wybierz te wpisy, które mogą **interesować mieszkańców powiatu** (np. uchwały, zarządzenia, przetargi, obwieszczenia, konsultacje społeczne, nabory, inwestycje). Dla każdego wybranego wpisu podaj:
- krótki tytuł / temat (jedna linia),
- dlaczego to może być ważne dla mieszkańca (jedno-dwa zdania).

Pomijaj wpisy czysto wewnętrzne, techniczne lub mało istotne.

---
{tekst_wpisow}
---
Odpowiedz w formie listy (numeracja). Tylko wybrane wpisy."""

SYSTEM_ARTYKUL = """Jesteś redaktorem serwisu informacyjnego dla mieszkańców powiatu. Piszesz zwięzłe, zrozumiałe artykuły na podstawie informacji z BIP. Styl: neutralny, rzeczowy, bez żargonu prawniczego tam gdzie to możliwe."""

PROMPT_ARTYKUL = """Na podstawie poniższej analizy wpisów z BIP przygotuj **jeden artykuł** do publikacji na stronie WordPress.

Wymagania:
- **Tytuł** – jeden, przyciągający uwagę (np. „Co nowego w BIP-ach powiatu? Uchwały, przetargi i konsultacje”).
- **Lead** – 2–4 zdania podsumowujące najważniejsze informacje.
- **Treść** – krótkie akapity (możesz użyć nagłówków <h3>), z linkami do źródeł BIP tam gdzie to sensowne. Format HTML dopasowany do WordPress (prosty HTML: <p>, <h3>, <a>, <ul>/<li>).
- Na końcu krótka zachęta do sprawdzenia pełnych informacji w BIP (np. „Szczegóły w Biuletynach Informacji Publicznej poszczególnych gmin i starostwa.”).

---
{tekst_analizy}
---
Wygeneruj gotowy artykuł (tytuł, lead, treść w HTML)."""


def analyze_for_residents(
    entries: list[BIPEntry],
    base_url: str = "http://localhost:11434",
    model: str = "SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M",
    timeout: int = 300,
) -> str:
    """
    Wysyła listę wpisów BIP do Bielika z prośbą o wybór tych,
    które mogą interesować mieszkańców. Zwraca odpowiedź modelu (lista wybranych + uzasadnienia).
    """
    tekst = entries_to_text(entries)
    prompt = PROMPT_ANALIZA.format(tekst_wpisow=tekst)
    return ollama_generate(
        base_url,
        model,
        prompt,
        system=SYSTEM_ANALIZA,
        stream=False,
        timeout=timeout,
    )


def generate_wordpress_article(
    analysis_text: str,
    base_url: str = "http://localhost:11434",
    model: str = "SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M",
    timeout: int = 300,
) -> str:
    """
    Na podstawie wyniku analizy (lista wybranych wpisów + uzasadnienia)
    generuje artykuł WordPress (tytuł, lead, treść HTML).
    """
    prompt = PROMPT_ARTYKUL.format(tekst_analizy=analysis_text)
    return ollama_generate(
        base_url,
        model,
        prompt,
        system=SYSTEM_ARTYKUL,
        stream=False,
        timeout=timeout,
    )
