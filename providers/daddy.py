"""
DaddyLive provider - connessione DIRETTA con header completi da browser
"""

import re
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE = "https://dlive.sx"
MIRRORS = ["https://dlive.sx", "https://dlstreams.st"]

# Header completi che simulano Chrome reale
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
    "Referer": f"{BASE}/",
    "Origin": BASE,
}

_channel_cache = None


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def fetch_channel_list(session: requests.Session) -> dict:
    """Ritorna {nome_canale: id} dalla pagina 24-7-channels"""
    global _channel_cache

    if _channel_cache is not None:
        logger.info("DaddyLive: usando cache (%d canali)", len(_channel_cache))
        return _channel_cache

    for mirror in MIRRORS:
        try:
            url = f"{mirror}/24-7-channels.php"
            r = session.get(url, timeout=10)
            r.raise_for_status()
            html = r.text

            pattern = re.compile(
                r'href="(?:[^"]*?/)?watch\.php\?id=(\d+)"[^>]*data-title="([^"]+)"',
                re.IGNORECASE
            )

            channels = {}
            for m in pattern.finditer(html):
                cid = int(m.group(1))
                name = m.group(2).strip()
                if name:
                    channels[name] = cid

            if channels:
                logger.info("DaddyLive: trovati %d canali", len(channels))
                _channel_cache = channels
                return channels
        except Exception as e:
            logger.warning("DaddyLive mirror %s fallito: %s", mirror, e)

    raise RuntimeError("DaddyLive: nessun mirror raggiungibile")


def find_channel_id(channels: dict, query: str) -> int:
    """Cerca ID canale"""
    q = query.lower().strip()

    for name, cid in channels.items():
        if name.lower() == q:
            return cid

    for name, cid in channels.items():
        if q in name.lower():
            return cid

    words = set(q.split())
    best_score, best_id = 0, None
    for name, cid in channels.items():
        name_words = set(name.lower().split())
        score = len(words & name_words)
        if score > best_score:
            best_score, best_id = score, cid

    if best_id and best_score >= max(1, len(words) - 1):
        return best_id

    raise ValueError(f"DaddyLive: canale '{query}' non trovato")


def get_stream(channel_name: str) -> dict:
    """Dato nome canale, ritorna URL diretto del player con header completi"""
    session = _get_session()
    channels = fetch_channel_list(session)
    cid = find_channel_id(channels, channel_name)

    stream_url = f"{BASE}/cast/stream-{cid}.php"

    return {
        "url": stream_url,
        "headers": {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": f"{BASE}/watch.php?id={cid}",
            "Origin": BASE,
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        },
        "source": "daddylive",
        "channel_id": cid,
    }


def resolve(channel: dict, stream: dict) -> dict:
    query = stream.get("query") or channel.get("name", "")
    result = get_stream(query)

    return {
        "url": result["url"],
        "provider": "DaddyLive",
        "quality": "HD",
        "headers": result.get("headers", {}),
        "proxy": "direct",
        "proxy_required": False,
    }
  
