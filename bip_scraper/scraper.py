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


@dataclass
class BIPEntry:
    """Pojedynczy wpis z BIP (ogłoszenie / aktualność)."""
    title: str
    url: str
    summary: str
    content: str
    published: str | None
    source_name: str
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
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            link_el = row.find("a", href=True)
            if not link_el:
                continue
            href = link_el.get("href", "").strip()
            url = _normalize_list_url(list_url, href)
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
        url = _normalize_list_url(list_url, href)
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
            url = _normalize_list_url(list_url, href)
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
            url = _normalize_list_url(list_url, href)
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
            all_entries.extend(entries)
        except Exception as e:
            # Loguj i idź dalej
            print(f"Błąd źródła {src.get('name', '?')}: {e}")
    return all_entries
