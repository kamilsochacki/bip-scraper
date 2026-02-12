#!/usr/bin/env python3
"""
Skrypt do uruchamiania scrapera BIP (np. z crona).

Użycie:
  python run.py --ollama              # scrape → Bielik analiza → artykuł WordPress (lokalnie)
  python run.py --ollama -o art.html  # jak wyżej, zapisz artykuł do pliku
  python run.py                       # scrape + wyślij do agenta (jeśli webhook skonfigurowany)
  python run.py --scrape-only         # tylko zbierz dane, wypisz JSON na stdout
  python run.py -o plik.json          # zapisz payload do pliku (bez wysyłki)

Przykład crona (codziennie o 8:00, artykuł przez Bielika):
  0 8 * * * cd /ścieżka/do/bip-scraper && .venv/bin/python run.py --ollama -o artykul.html
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

import requests

# Uruchom z katalogu projektu
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bip_scraper.config import load_config
from bip_scraper.scraper import run_scraper
from bip_scraper.sender import build_payload, save_payload_to_file, send_to_agent
from bip_scraper.ollama_client import (
    analyze_for_residents,
    generate_wordpress_article,
    extract_facts,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BIP Scraper – rejestry zmian, analiza Bielik (Ollama), artykuł WordPress"
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="Ścieżka do config.yaml")
    parser.add_argument("--scrape-only", action="store_true", help="Tylko zbierz dane, wypisz JSON na stdout")
    parser.add_argument("--output", "-o", help="Zapisz wynik do pliku (JSON payload lub artykuł HTML przy --ollama)")
    parser.add_argument("--instruction", "-i", help="Dodatkowa instrukcja dla agenta AI (gdy bez --ollama)")
    parser.add_argument(
        "--ollama",
        action="store_true",
        help="Scrape → analiza Bielik (Ollama) → generowanie artykułu WordPress lokalnie",
    )
    parser.add_argument("--model-extractor", help="Model do ekstrakcji faktów (np. mistral)")
    parser.add_argument("--model-writer", help="Model do pisania artykułu (np. SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M)")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    entries = run_scraper(config)
    if not entries:
        print("Brak wpisów z BIP.", file=sys.stderr)
        return 0

    print(f"Pobrano {len(entries)} wpisów z rejestrów zmian BIP.", file=sys.stderr)

    # Zapisz snapshot pobranych wpisów lokalnie (timestamped), przydatne do debugowania
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = Path(f"bip_entries_{ts}.json")
        save_payload_to_file(entries, str(snapshot_path), instruction=args.instruction)
        print(f"Zapisano lokalny snapshot: {snapshot_path}", file=sys.stderr)
    except Exception as e:
        print(f"Nie udało się zapisać snapshotu: {e}", file=sys.stderr)

    # Tryb Ollama (Bielik): analiza → artykuł
    if args.ollama:
        ollama_cfg = config.get("ollama") or {}
        base_url = ollama_cfg.get("base_url") or "http://localhost:11434"
        
        # Default strategy: Single stage (Bielik only)
        # If --model-extractor is set, switch to Two-Stage (Extraction -> Synthesis)
        model_writer = args.model_writer or ollama_cfg.get("model") or "SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M"
        model_extractor = args.model_extractor or ollama_cfg.get("model_extractor")
        
        timeout = ollama_cfg.get("timeout") or 300
        out_path = args.output or "artykul.html"

        try:
            if model_extractor:
                # Two-Stage Pipeline
                print(f"I ETAP: Ekstrakcja faktów (Model: {model_extractor})...", file=sys.stderr)
                # Note: chunk_size=5 ensures we don't overload the extractor's context either, 
                # although Mistral/Llama usually have 8k-128k context.
                facts = extract_facts(entries, base_url=base_url, model=model_extractor, timeout=timeout)
                
                if not facts:
                    print("Ekstrakcja faktów zwróciła pusty wynik. Przerywam.", file=sys.stderr)
                    return 1

                print(f"II ETAP: Generowanie artykułu (Model: {model_writer})...", file=sys.stderr)
                artykul = generate_wordpress_article(facts, base_url=base_url, model=model_writer, timeout=timeout)
            else:
                # Legacy Single Stage
                print(f"Analiza jednoetapowa (Model: {model_writer})...", file=sys.stderr)
                analiza = analyze_for_residents(entries, base_url=base_url, model=model_writer, timeout=timeout)
                print("Generowanie artykułu WordPress...", file=sys.stderr)
                artykul = generate_wordpress_article(analiza, base_url=base_url, model=model_writer, timeout=timeout)
                
        except requests.exceptions.RequestException as e:
            print(f"Błąd połączenia z Ollama ({base_url}): {e}", file=sys.stderr)
            if getattr(e, "response") and e.response is not None and e.response.status_code == 404:
                print("Endpoint nie znaleziony (404). Sprawdź: curl -s http://localhost:11434/api/tags", file=sys.stderr)
                print("Jeśli port 11434 jest zajęty przez inną usługę, zatrzymaj ją i uruchom Ollamę (ollama serve).", file=sys.stderr)
            else:
                print("Upewnij się, że Ollama działa (ollama serve) i modele są pobrane.", file=sys.stderr)
            return 1

        if out_path == "-":
            print(artykul)
        else:
            Path(out_path).write_text(artykul, encoding="utf-8")
            print(f"Zapisano artykuł do {out_path}.", file=sys.stderr)
        return 0

    if args.scrape_only:
        payload = build_payload(entries, instruction=args.instruction)
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        return 0

    if args.output:
        save_payload_to_file(entries, args.output, instruction=args.instruction)
        print(f"Zapisano payload do {args.output}.", file=sys.stderr)
        return 0

    agent = config.get("agent") or {}
    webhook_url = (agent.get("webhook_url") or "").strip()
    if not webhook_url:
        print("Brak webhook_url w config.agent – zapisuję do bip_output.json.", file=sys.stderr)
        save_payload_to_file(entries, "bip_output.json", instruction=args.instruction)
        return 0

    try:
        r = send_to_agent(
            entries,
            webhook_url,
            api_key=agent.get("api_key") or None,
            api_key_header=agent.get("api_key_header") or "Authorization",
            instruction=args.instruction,
        )
        r.raise_for_status()
        print(f"Wysłano do agenta: {r.status_code}.", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        print(f"Błąd wysyłki do agenta: {e}", file=sys.stderr)
        if getattr(e, "response") and e.response is not None:
            print(e.response.text[:500], file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
