"""
Scraper BIP: pobiera najnowsze zmiany i ogłoszenia z podanych adresów.
Wspiera kanały RSS/Atom oraz fallback na skrapowanie HTML.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup


import io
import sys
import pytesseract
from pdf2image import convert_from_bytes
from pypdf import PdfReader

@dataclass
class BIPEntry:
    """Pojedynczy wpis z BIP (ogłoszenie / aktualność)."""
    title: str
    url: str
    summary: str
    content: str
    published: str | None
    source_name: str
    attachments: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        """Słownik gotowy do wysłania do agenta AI (np. pod artykuł WordPress)."""
        return {
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "content": self.content,
            "published": self.published,
            "source_name": self.source_name,
            "attachments": self.attachments,
        }


def _fetch(
    url: str,
    timeout: int = 15,
    user_agent: str | None = None,
) -> requests.Response:
    headers = {"User-Agent": user_agent or "BIP-Scraper/1.0 (Python)"}
    return requests.get(url, timeout=timeout, headers=headers)


def fetch_rss(
    rss_url: str,
    source_name: str,
    max_entries: int = 10,
    timeout: int = 15,
    user_agent: str | None = None,
) -> list[BIPEntry]:
    """Pobiera wpisy z kanału RSS/Atom."""
    resp = _fetch(rss_url, timeout=timeout, user_agent=user_agent)
    resp.raise_for_status()
    feed = feedparser.parse(
        resp.content,
        response_headers=dict(resp.headers),
        request_headers={"User-Agent": user_agent or "BIP-Scraper/1.0"},
    )
    entries: list[BIPEntry] = []
    base_url = feed.feed.get("link") or rss_url

    for i, e in enumerate(feed.entries):
        if i >= max_entries:
            break
        link = e.get("link") or ""
        if link and not link.startswith("http"):
            link = urljoin(base_url, link)
        content = e.get("content") or e.get("summary") or ""
        if isinstance(content, list):
            content = content[0].get("value", "") if content else ""
        elif hasattr(content, "value"):
            content = getattr(content, "value", str(content))
        published = None
        if e.get("published_parsed"):
            try:
                published = datetime(*e.published_parsed[:6]).isoformat()
            except (TypeError, IndexError):
                published = e.get("published", "")
        elif e.get("updated_parsed"):
            try:
                published = datetime(*e.updated_parsed[:6]).isoformat()
            except (TypeError, IndexError):
                published = e.get("updated", "")

        entries.append(
            BIPEntry(
                title=e.get("title") or "(bez tytułu)",
                url=link,
                summary=(e.get("summary") or "")[:500],
                content=content or (e.get("summary") or ""),
                published=published,
                source_name=source_name,
                raw=dict(e),
            )
        )
    return entries


def _normalize_list_url(base: str, href: str) -> str:
    if not href or href.startswith("#"):
        return ""
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def _extract_date_from_cell(cell) -> str | None:
    """Wyciąga datę z tekstu komórki (np. 'śr., 11/02/2026 - 14:42' lub '10 lut 2026, 12:34')."""
    if not cell:
        return None
    text = (cell.get_text() if hasattr(cell, "get_text") else str(cell)).strip()
    if not text or len(text) > 60:
        return None
    m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
    if m:
        return text
    m = re.search(r"(\d{1,2})\s+(lut|sty|mar|kwi|maj|cze|lip|sie|wrz|paź|lis|gru)[a-z]*\s+(\d{4})", text, re.I)
    if m:
        return text
    return None


def fetch_rejestr_zmian(
    list_url: str,
    source_name: str,
    max_entries: int = 25,
    timeout: int = 15,
    user_agent: str | None = None,
) -> list[BIPEntry]:
    """
    Pobiera wpisy z rejestru zmian BIP.
    Obsługuje: tabele (np. powiat kamienski, gmina wolin) oraz bloki „Ostatnio dodane”
    (np. BIP Dziwnów, Kamień Pomorski – układ Alfa).
    """
    resp = _fetch(list_url, timeout=timeout, user_agent=user_agent)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    base_url = list_url.rsplit("/", 1)[0] + "/" if "/" in list_url else list_url + "/"
    if not base_url.startswith("http"):
        parsed = urlparse(list_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}/"
    entries: list[BIPEntry] = []
    seen: set[str] = set()

    # 1) Tabela rejestru zmian (np. powiat kamienski: Zmieniono | Tytuł | Użytkownik | Informacja)
    tables = soup.find_all("table")
    
    # Check if tables are empty (generic DataTables structure often has empty table in HTML)
    is_empty_table = True
    if tables:
        for table in tables:
            if table.find("tbody") and table.find("tbody").find("tr"):
                is_empty_table = False
                break
            # Also check if table has rows directly (invalid HTML but possible)
            if table.find("tr") and not table.find("thead"):
                is_empty_table = False
                break

    if not tables or is_empty_table:
        # Try to find DataTables AJAX configuration
        import re
        import json
        
        # Look for script containing .DataTable and ajax
        scripts = soup.find_all("script")
        ajax_url = None
        for script in scripts:
            if script.string and ".DataTable" in script.string and "ajax" in script.string:
                match = re.search(r'"ajax":\s*"([^"]+)"', script.string)
                if match:
                    ajax_url = match.group(1)
                    break
        
        if ajax_url:
            print(f"DEBUG: Found DataTables AJAX URL: {ajax_url}", file=sys.stderr)
            # Construct full URL
            if ajax_url.startswith("/"):
                from urllib.parse import urlparse
                parsed_list_url = urlparse(list_url)
                full_ajax_url = f"{parsed_list_url.scheme}://{parsed_list_url.netloc}{ajax_url}"
            else:
                full_ajax_url = ajax_url # Assume absolute or relative to base?? Usually absolute path from root
            
            try:
                # Fetch JSON
                headers = {"User-Agent": user_agent} if user_agent else {}
                headers["X-Requested-With"] = "XMLHttpRequest"
                headers["Referer"] = list_url
                
                print(f"DEBUG: Fetching AJAX data from {full_ajax_url}", file=sys.stderr)
                resp = requests.get(full_ajax_url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                
                rows = []
                if 'data' in data:
                    rows = data['data']
                elif 'aaData' in data:
                    rows = data['aaData']
                
                for row in rows:
                    if len(entries) >= max_entries:
                        break
                    
                    # Mapping based on observation of bip.gminawolin.pl / bip.dziwnow.pl
                    # '0': Title (HTML or Text), '1': Module, '2': Type, '3': Date, '4': Author
                    # Keys might be integers or strings of integers
                    
                    raw_title = row.get('0') or row.get(0)
                    date_str = row.get('3') or row.get(3) or ""
                    author = row.get('4') or row.get(4) or ""
                    
                    if raw_title:
                        # Check for link in title
                        title_soup = BeautifulSoup(str(raw_title), "html.parser")
                        link = title_soup.find("a")
                        if link and link.get("href"):
                            title_text = link.get_text(strip=True)
                            href = link.get("href").replace("\\", "/")
                            if href.startswith("/"):
                                parsed_list_url = urlparse(list_url)
                                entry_url = f"{parsed_list_url.scheme}://{parsed_list_url.netloc}{href}"
                            else:
                                entry_url = href
                        else:
                            title_text = title_soup.get_text(strip=True) or str(raw_title)
                            entry_url = list_url # Fallback if no specific link
                        
                        entry = BIPEntry(
                            title=title_text,
                            url=entry_url,
                            published=str(date_str),
                            source_name=source_name,
                            summary=f"Data: {date_str}. Autor: {author}",
                            content=f"Log: {title_text}. Autor: {author}. Data: {date_str}",
                            attachments=[]
                        )
                        entries.append(entry)
                
                if entries:
                    return entries

            except Exception as e:
                print(f"ERROR fetching/parsing AJAX DataTables: {e}", file=sys.stderr)

    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            link_el = row.find("a", href=True)
            if not link_el:
                continue
            href = link_el.get("href", "").strip()
            url = _normalize_list_url(base_url, href)
            if not url or url in seen or any(x in url.lower() for x in ("javascript:", "mailto:", "#")):
                continue
            title = (link_el.get_text() or "").strip()
            if len(title) < 5:
                continue
            published = None
            for c in cells:
                if link_el in c.find_all("a"):
                    continue
                d = _extract_date_from_cell(c)
                if d:
                    published = d
                    break
            seen.add(url)
            entries.append(
                BIPEntry(
                    title=title,
                    url=url,
                    summary="",
                    content="",
                    published=published,
                    source_name=source_name,
                    raw={"list_url": list_url},
                )
            )
            if len(entries) >= max_entries:
                return entries

    # 2) Bloki „Ostatnio dodane” (np. .view-content, .node, element z datą + nagłówkiem)
    if entries:
        return entries
    date_pattern = re.compile(
        r"\d{1,2}\s+(sty|lut|mar|kwi|maj|cze|lip|sie|wrz|paź|lis|lis|gru)[a-z]*\s+\d{4}\s*,?\s*\d{1,2}:\d{2}",
        re.I,
    )
    for block in soup.select(".view-content .views-row, .node, .aktualnosc, [class*='last-added'], article, .item"):
        link = block.find("a", href=True)
        if not link:
            continue
        href = link.get("href", "").strip()
        url = _normalize_list_url(base_url, href)
        if not url or url in seen or "javascript:" in href.lower():
            continue
        title = (link.get_text() or "").strip()
        if len(title) < 5:
            continue
        published = None
        text = block.get_text() or ""
        m = date_pattern.search(text)
        if m:
            published = m.group(0).strip()
        seen.add(url)
        entries.append(
            BIPEntry(
                title=title,
                url=url,
                summary="",
                content="",
                published=published,
                source_name=source_name,
                raw={"list_url": list_url},
            )
        )
        if len(entries) >= max_entries:
            return entries

    # 3) Fallback: dowolna lista linków w głównej treści (np. strona główna BIP)
    main = soup.find("main") or soup.find("article") or soup.find(id="content") or soup.body
    if main:
        for link in main.find_all("a", href=True):
            href = link.get("href", "").strip()
            url = _normalize_list_url(base_url, href)
            if not url or url in seen or any(x in url.lower() for x in ("javascript:", "mailto:", "#", "rejestr-zmian")):
                continue
            title = (link.get_text() or "").strip()
            if len(title) < 10:  # wyższy próg dla fallbacku
                continue
            seen.add(url)
            entries.append(
                BIPEntry(
                    title=title,
                    url=url,
                    summary="",
                    content="",
                    published=None,
                    source_name=source_name,
                    raw={"list_url": list_url},
                )
            )
            if len(entries) >= max_entries:
                return entries

    return entries


def fetch_html_list(
    list_url: str,
    source_name: str,
    max_entries: int = 10,
    timeout: int = 15,
    user_agent: str | None = None,
) -> list[BIPEntry]:
    """
    Skrapuje stronę HTML w poszukiwaniu linków do ogłoszeń/aktualności.
    Szuka typowych elementów: .news-item, .ogloszenie, listy linków, artykuły.
    """
    resp = _fetch(list_url, timeout=timeout, user_agent=user_agent)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    base = list_url.rsplit("/", 1)[0] + "/" if "/" in list_url else list_url + "/"
    if not base.startswith("http"):
        base = f"{urlparse(list_url).scheme}://{urlparse(list_url).netloc}/"

    entries: list[BIPEntry] = []
    seen_urls: set[str] = set()

    # Typowe selektory na stronach BIP
    for selector in (
        "article",
        ".news-item",
        ".ogloszenie",
        ".aktualnosc",
        ".komunikat",
        "[class*='news']",
        "[class*='ogloszen']",
        ".list-item",
        "li a",
    ):
        for el in soup.select(selector):
            link = el if el.name == "a" else el.find("a")
            if not link or not link.get("href"):
                continue
            href = link.get("href", "").strip()
            url = _normalize_list_url(base, href)
            if not url or url in seen_urls:
                continue
            # Odrzuć linki do samej strony, pliki PDF bez opisu itp.
            if any(x in url.lower() for x in ("javascript:", "mailto:", "#")):
                continue
            title = (link.get_text() or "").strip() or "(bez tytułu)"
            if len(title) < 3:
                continue
            seen_urls.add(url)
            entries.append(
                BIPEntry(
                    title=title,
                    url=url,
                    summary="",
                    content="",
                    published=None,
                    source_name=source_name,
                    raw={"list_url": list_url},
                )
            )
            if len(entries) >= max_entries:
                return entries

    return entries


def fetch_source(
    source: dict,
    timeout: int = 15,
    user_agent: str | None = None,
) -> list[BIPEntry]:
    """
    Pobiera wpisy z jednego źródła: RSS, rejestr zmian (rejestr_zmian: true)
    albo zwykła lista HTML.
    """
    name = source.get("name") or "BIP"
    max_entries = source.get("max_entries") or 10

    if source.get("rss_url"):
        return fetch_rss(
            source["rss_url"],
            source_name=name,
            max_entries=max_entries,
            timeout=timeout,
            user_agent=user_agent,
        )
    if source.get("list_url"):
        if source.get("rejestr_zmian"):
            return fetch_rejestr_zmian(
                source["list_url"],
                source_name=name,
                max_entries=max_entries,
                timeout=timeout,
                user_agent=user_agent,
            )
        return fetch_html_list(
            source["list_url"],
            source_name=name,
            max_entries=max_entries,
            timeout=timeout,
            user_agent=user_agent,
        )
    return []


def extract_text_from_pdf(pdf_content: bytes) -> str:
    """Ekstrakcja tekstu z PDF (pypdf). Jeśli pusto, próba OCR (tesseract via pdf2image)."""
    text = ""
    try:
        reader = PdfReader(io.BytesIO(pdf_content))
        text_parts = []
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text_parts.append(extracted)
        text = "\n".join(text_parts).strip()
    except Exception as e:
        print(f"Błąd pypdf: {e}", file=sys.stderr)

    # Fallback OCR jeśli tekstu jest bardzo mało (np. skan)
    if len(text) < 50:
        print("Mało tekstu w PDF (skan?), uruchamiam OCR (tesseract)...", file=sys.stderr)
        try:
            # Konwersja PDF do obrazów (wymaga poppler w systemie)
            images = convert_from_bytes(pdf_content)
            ocr_text = ""
            for image in images:
                # Opcjonalnie: lang='pol' lub 'pol+eng'
                page_text = pytesseract.image_to_string(image, lang='pol+eng')
                ocr_text += page_text + "\n"
            
            if len(ocr_text.strip()) > len(text):
                 text = ocr_text
        except Exception as e:
            print(f"Błąd OCR: {e}", file=sys.stderr)
            if not text:
                return f"[Błąd odczytu PDF i OCR: {str(e)}]"

    return text


def fetch_entry_details(
    entry: BIPEntry,
    timeout: int = 15,
    user_agent: str | None = None,
) -> None:
    """
    Wchodzi na stronę wpisu, szuka załączników (PDF), pobiera je i wyciąga tekst.
    Modyfikuje obiekt entry inplace (uzupełnia pole attachments).
    """
    # Jeśli to link bezpośrednio do pliku (rzadkie w BIP, ale możliwe w RSS), pomijamy deep scraping
    if entry.url.lower().endswith(".pdf"):
        return

    try:
        resp = _fetch(entry.url, timeout=timeout, user_agent=user_agent)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        base_url = entry.url
        
        # Szukamy linków do załączników
        # Kryteria:
        # 1. href kończy się na .pdf
        # 2. treść linku zawiera "załącznik", "pobierz" itp.
        # 3. Klasa elementu sugeruje załącznik (np. 'att-link') - opcjonalnie
        
        candidates = soup.find_all("a", href=True)
        unique_links = set()
        
        for link_el in candidates:
            href = link_el.get("href", "").strip()
            text = link_el.get_text(" ", strip=True).lower()
            
            # Normalizacja URL
            full_url = urljoin(base_url, href)
            
            is_pdf_ext = full_url.lower().endswith(".pdf")
            is_attachment_text = any(kw in text for kw in ("załącznik", "zalacznik", "pobierz", "treść"))
            
            # Filtrujemy - musi być PDF lub jawnie nazwany załącznikiem (i prowadzić do pliku)
            if not (is_pdf_ext or (is_attachment_text and "pdf" in href.lower())):
                continue
                
            if full_url in unique_links:
                continue
            unique_links.add(full_url)
            
            # Pobieramy plik
            try:
                # Ograniczenie: pobieramy tylko PDFy
                # head first?
                file_resp = _fetch(full_url, timeout=timeout, user_agent=user_agent)
                if file_resp.status_code == 200 and "application/pdf" in file_resp.headers.get("Content-Type", "").lower():
                    raw_text = extract_text_from_pdf(file_resp.content)
                    # Limit tekstu załącznika, żeby nie zapchać kontekstu
                    trimmed_text = raw_text[:5000] + ("..." if len(raw_text) > 5000 else "")
                    
                    entry.attachments.append({
                        "name": text or link_el.get("title") or "Załącznik",
                        "url": full_url,
                        "text_content": trimmed_text,
                        "size": len(file_resp.content)
                    })
            except Exception as e:
                print(f"Błąd pobierania załącznika {full_url}: {e}", file=sys.stderr)

    except Exception as e:
        print(f"Błąd fetch_entry_details dla {entry.url}: {e}", file=sys.stderr)


def run_scraper(config: dict) -> list[BIPEntry]:
    """Uruchamia scraper dla wszystkich źródeł z configu."""
    sources = config.get("sources") or []
    scraper_cfg = config.get("scraper") or {}
    timeout = scraper_cfg.get("request_timeout", 15)
    user_agent = scraper_cfg.get("user_agent")

    all_entries: list[BIPEntry] = []
    for src in sources:
        try:
            entries = fetch_source(src, timeout=timeout, user_agent=user_agent)
            
            # Dla każdego wpisu pobieramy szczegóły (załączniki)
            print(f"Pobieranie szczegółów dla źródła {src.get('name')}... ({len(entries)} wpisów)", file=sys.stderr)
            for e in entries:
                try:
                    fetch_entry_details(e, timeout=timeout, user_agent=user_agent)
                except Exception as err:
                    print(f"Błąd pobierania szczegółów {e.url}: {err}", file=sys.stderr)
            
            all_entries.extend(entries)
        except Exception as e:
            # Loguj i idź dalej
            print(f"Błąd źródła {src.get('name', '?')}: {e}", file=sys.stderr)
    return all_entries
