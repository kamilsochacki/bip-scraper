# BIP Scraper

Scraper w Pythonie, który zbiera **rejestry zmian** z podanych BIP-ów (Biuletyn Informacji Publicznej), analizuje je **lokalnie modelem Bielik (Ollama)** i generuje **artykuł WordPress** z informacjami istotnymi dla mieszkańców.

Działa w całości lokalnie (Ollama + Bielik), można uruchamiać np. z **crona**.

## Przepływ

1. **Scrape** – pobranie wpisów z rejestrów zmian z 4 BIP-ów (Wolin, Dziwnów, Kamień Pomorski, Powiat Kamieński).
2. **Analiza (Bielik)** – wybór wpisów interesujących dla mieszkańców (uchwały, przetargi, konsultacje, obwieszczenia itd.).
3. **Artykuł (Bielik)** – wygenerowanie jednego artykułu WordPress (tytuł, lead, treść w HTML).

## Wymagania

- Python 3.10+
- [Ollama](https://ollama.com/) z modelem **Bielik** (np. `SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M`):
  ```bash
  ollama pull SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M
  ollama ps   # sprawdź pełną nazwę modelu, jeśli używasz innej wersji
  ```

## Instalacja

```bash
cd bip-scraper
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Konfiguracja

W `config.yaml` (w projekcie jest już przykładowa konfiguracja dla 4 BIP-ów powiatu kamieńskiego):

- **sources** – listę BIP-ów z `list_url` (strona rejestru zmian lub strona główna z „Ostatnio dodane”) i `rejestr_zmian: true`,
- **ollama** – `base_url` (domyślnie `http://localhost:11434`), `model` (pełna nazwa z `ollama ps`, np. `SpeakLeash/bielik-11b-v2.3-instruct:Q4_K_M`), `timeout`.

Źródła mogą mieć też `rss_url` (wtedy używany jest kanał RSS zamiast skrapowania HTML).

## Uruchomienie

- **Pełny pipeline (scrape → Bielik → artykuł)** – wynik do pliku:
  ```bash
  python run.py --ollama -o artykul.html
  ```
- **Artykuł na stdout**:
  ```bash
  python run.py --ollama -o -
  ```
- **Tylko zbierz dane** (JSON na stdout):
  ```bash
  python run.py --scrape-only
  ```
- **Zapisz surowy payload do pliku** (bez Ollama):
  ```bash
  python run.py -o dane.json
  ```

## Cron

Codzienne generowanie artykułu o 8:00:

```cron
0 8 * * *  .venv/bin/python run.py --ollama -o /ścieżka/do/artykul.html
```

## Payload do agenta

Agent dostaje jeden request POST (JSON) w postaci:

```json
{
  "entries": [
    {
      "title": "Tytuł ogłoszenia",
      "url": "https://bip.example.pl/...",
      "summary": "Skrót",
      "content": "Treść",
      "published": "2025-02-11T08:00:00",
      "source_name": "Nazwa BIP"
    }
  ],
  "instruction": "Na podstawie powyższych wpisów z BIP przygotuj artykuł nadający się do publikacji na WordPressie (tytuł, lead, treść)."
}
```

Agent może na tej podstawie wygenerować artykuł w formacie WordPress (np. HTML lub bloki Gutenberga). Tryb `--ollama` omija webhook i robi analizę oraz artykuł lokalnie w Bieliku.
