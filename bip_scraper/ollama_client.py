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
    payload: dict[str, Any] = {
        "model": model, 
        "prompt": prompt, 
        "stream": stream,
        "options": {
            "num_ctx": 16384
        }
    }
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
        json={
            "model": model, 
            "messages": messages, 
            "stream": stream,
            "options": {
                "num_ctx": 16384
            }
        },
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
            # Log the error from /api/generate (e.g., "model not found")
            try:
                err_msg = e.response.json().get("error", e.response.text)
                print(f"WARN: /api/generate returned 404: {err_msg}. Trying /api/chat fallback...", file=sys.stderr)
            except Exception:
                print(f"WARN: /api/generate returned 404. Trying /api/chat fallback...", file=sys.stderr)
            
            return _ollama_chat(base_url, model, prompt, system, stream, timeout)
        
        # Re-raise other errors (500, etc.)
        if e.response is not None:
             print(f"ERROR: Ollama API returned {e.response.status_code}: {e.response.text}", file=sys.stderr)
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
        
        if e.attachments:
            lines.append("   ZAŁĄCZNIKI (PDF):")
            for att in e.attachments:
                name = att.get("name", "Plik")
                # Skracamy treść załącznika, by nie przepełnić promptu
                content = (att.get("text_content") or "").strip()
                if content:
                    # Wycinamy białe znaki i bierzemy fragment
                    snippet = " ".join(content.split())[:800]
                    lines.append(f"     -> {name}: {snippet}...")
        
        lines.append("")
    return "\n".join(lines)


SYSTEM_ANALIZA = """Jesteś ekspertem od informacji publicznych (BIP). Twoim zadaniem jest wybór informacji istotnych dla mieszkańców powiatu (gminy, miasta). 
Opieraj się WYŁĄCZNIE na dostarczonym tekście, w tym na treści ZAŁĄCZNIKÓW (PDF). Nie wymyślaj faktów, dat ani kwot. Jeśli czegoś nie ma w tekście, nie pisz o tym.
Zwracaj uwagę na: uchwały i zarządzenia wpływające na codzienne życie, przetargi i zamówienia publiczne (szczegóły w załącznikach), konsultacje społeczne, obwieszczenia, zmiany w prawie miejscowym.
Pomijaj wewnętrzne procedury, czysto techniczne zmiany i powtórzenia."""

PROMPT_ANALIZA = """Poniżej lista wpisów z rejestrów zmian kilku BIP-ów. Część wpisów zawiera sekcję "ZAŁĄCZNIKI (PDF)" z wyciągniętą treścią dokumentów.

Przeanalizuj dokładnie tytuły oraz treść załączników. Wybierz te wpisy, które są ważne dla mieszkańców (np. inwestycje, podatki, utrudnienia, ważne terminy).

Dla każdego wybranego wpisu podaj:
1. Konkretny tytuł/temat (np. "Przetarg na remont ulicy X", "Konsultacje w sprawie Y").
2. Kluczowe szczegóły znaleziona w załącznikach (np. termin składania ofert, kwota, data spotkania, numer działki).
3. Dlaczego to ważne dla mieszkańca.

Jeśli wpis jest techniczną zmianą w BIP bez znaczenia dla ogółu -> POMIŃ GO.

---
{tekst_wpisow}
---
Odpowiedz w formie listy punktowanej. Pisz zwięźle i konkretnie. Nie halucynuj."""

SYSTEM_ARTYKUL = """Jesteś rzetelnym dziennikarzem lokalnym. Piszesz artykuł na podstawie dostarczonej analizy. 
Twoim priorytetem jest prawda i konkret. Nie dodawaj "upiększaczy" ani zmyślonych opinii mieszkańców. 
Opieraj się na faktach z analizy (daty, nazwy, kwoty). Styl: informacyjny, prosty, zrozumiały."""

PROMPT_ARTYKUL = """Na podstawie poniższej analizy wpisów z BIP przygotuj artykuł do publikacji.

Struktura:
1. Chwytliwy, ale prawdziwy tytuł.
2. Lead (wstęp) streszczający najważniejsze newsy (maks 3-4 zdania).
3. Rozwinięcie:
   - Opisz kolejne tematy, grupując je logicznie (np. "Inwestycje i przetargi", "Sprawy urzędowe").
   - Używaj konkretów (daty, numery działek, nazwy ulic) jeśli były w analizie.
   - Jeśli analiza wspomina o załączniku, napisz "Szczegóły w załączniku na stronie BIP".
   - Przy każdym temacie (lub na końcu sekcji) dodaj link "Zobacz w BIP" kierujący do źródła (jeśli URL jest dostępny w analizie).
4. Zakończenie: Link do źródeł (ogólne odesłanie do BIP).

Format HTML (używaj <h3> dla nagłówków sekcji, <p> dla treści, <ul>/<li> dla wyliczeń, <a href="..."> dla linków).

---
ANALIZA WPISÓW:
{tekst_analizy}
---
Generuj artykuł."""


def chunk_entries(entries: list[BIPEntry], chunk_size: int) -> list[list[BIPEntry]]:
    """Dzieli listę wpisów na mniejsze fragmenty (batche)."""
    return [entries[i : i + chunk_size] for i in range(0, len(entries), chunk_size)]


def analyze_for_residents(
    entries: list[BIPEntry],
    base_url: str = "http://localhost:11434",
    model: str = "SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M",
    timeout: int = 300,
    chunk_size: int = 5,
) -> str:
    """
    Wysyła listę wpisów BIP do Bielika w partiach (batchach),
    aby nie przekroczyć okna kontekstowego (Map).
    Następnie łączy wyniki (Reduce).
    """
    batches = chunk_entries(entries, chunk_size)
    combined_analysis = []
    
    print(f"Analiza w {len(batches)} częściach (po max {chunk_size} wpisów)...")

    for i, batch in enumerate(batches, 1):
        print(f"  -> Przetwarzanie części {i}/{len(batches)}...")
        tekst = entries_to_text(batch)
        prompt = PROMPT_ANALIZA.format(tekst_wpisow=tekst)
        
        try:
            response = ollama_generate(
                base_url,
                model,
                prompt,
                system=SYSTEM_ANALIZA,
                stream=False,
                timeout=timeout,
            )
            combined_analysis.append(f"--- CZĘŚĆ {i} ---\n{response}")
        except Exception as e:
            print(f"Błąd analizy części {i}: {e}")
            combined_analysis.append(f"--- CZĘŚĆ {i} (BŁĄD) ---\n")

    return "\n\n".join(combined_analysis)


SYSTEM_EXTRACTION = """Jesteś analitykiem danych. Twoim zadaniem jest wyciągnięcie suchych faktów z tekstu w formacie JSON.
Nie interpretuj przedwcześnie, nie pisz esejów. Interesują nas:
- Daty (terminy składania ofert, spotkań, wydarzeń)
- Kwoty (budżet, cena, wadium)
- Numery (działek, uchwał, dróg)
- Cel/Temat (np. sprzedaż działki, remont drogi, sesja rady)
Jeśli tekst to błąd OCR lub bełkot -> POMIŃ CAŁKOWICIE."""

PROMPT_EXTRACTION = """Przeanalizuj poniższe wpisy BIP (w tym treść załączników).
Dla każdego wpisu, który zawiera konkretne informacje (inwestycje, prawo, finanse), wygeneruj obiekt JSON w liście.

Format wyjściowy (tylko JSON, bez markdowna):
[
  {{
    "tytul": "Skrócony tytuł",
    "fakt": "Krótki opis co się dzieje (np. Przetarg na X)",
    "szczegoly": "Kluczowe dane: 200 tys. zł, działka 123/4, termin do 15.05",
    "zrodlo": "Nazwa źródła",
    "url": "Pełny link do wpisu (przepisany z wejścia)"
  }}
]

Jeśli wpis jest nieistotny lub błędem OCR -> nie dodawaj go do listy.

WPISY:
{tekst_wpisow}
"""

def extract_facts(
    entries: list[BIPEntry],
    base_url: str = "http://localhost:11434",
    model: str = "mistral",
    timeout: int = 300,
    chunk_size: int = 5,
) -> str:
    """
    Etap 1: Ekstrakcja faktów.
    Korzysta z lekkiego modelu (Mistral/Llama) do wyciągnięcia konkretów z szumu OCR.
    Zwraca zagregowaną listę faktów (tekst JSON-like).
    """
    batches = chunk_entries(entries, chunk_size)
    combined_facts = []
    
    print(f"Ekstrakcja faktów w {len(batches)} częściach (model: {model})...")

    for i, batch in enumerate(batches, 1):
        print(f"  -> Ekstrakcja części {i}/{len(batches)}...")
        tekst = entries_to_text(batch)
        prompt = PROMPT_EXTRACTION.format(tekst_wpisow=tekst)
        
        try:
            # Wymuszamy format JSON jeśli model to obsługuje, ale tekstowo też ok
            response = ollama_generate(
                base_url,
                model,
                prompt,
                system=SYSTEM_EXTRACTION,
                stream=False,
                timeout=timeout,
            )
            combined_facts.append(response)
        except Exception as e:
            print(f"Błąd ekstrakcji części {i}: {e}")

    
    if not combined_facts:
        print("BŁĄD: Ekstrakcja faktów zakończyła się niepowodzeniem (pusta lista).", file=sys.stderr)
        return ""
        
    return "\n".join(combined_facts)


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
