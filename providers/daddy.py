"""
DaddyLive provider - estrae l'm3u8 VERO dalla catena completa.

Architettura:
- Discovery (dlive.sx, assetrage.net) via proxy CORS pubblici gratuiti
  (i proxy pubblici scaricano coi LORO IP, aggirando il blocco IP GitHub)
- Lo stream finale (CDN esterno) viene restituito DIRETTO al player
"""

import re
import base64
import json
import logging
from urllib.parse import urljoin, urlparse, quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE = "https://dlive.sx"
MIRRORS = ["https://dlive.sx", "https://dlstreams.st"]
EMBED_REFERER = "https://assetrage.net/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Proxy CORS pubblici e gratuiti: usati SOLO per le pagine di discovery.
# Sono loro a contattare dlive.sx, quindi il blocco IP di GitHub non si applica.
DISCOVERY_PROXIES = [
    lambda u: ("https://api.allorigins.win/raw?url=" + quote(u, safe="")),
    lambda u: ("https://api.codetabs.com/v1/proxy?quest=" + u),
    lambda u: ("https://corsproxy.io/?url=" + quote(u, safe="")),
    lambda u: ("https://api.cors.lol/?url=" + quote(u, safe="")),
]

_channel_cache = None
_stream_cache = {}


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _is_discovery_host(url: str) -> bool:
    """True se l'URL è una pagina di discovery (da proxyare), False se è lo stream (diretto)"""
    host = urlparse(url).netloc.lower()
    return ("dlive" in host) or ("assetrage" in host) or host.endswith(".sx") or host.endswith(".st")


def _fetch(url: str, timeout: int = 20) -> requests.Response:
    """
    Se è una pagina di discovery (dlive/assetrage): prova i proxy CORS pubblici.
    Se è un CDN/stream: connessione DIRETTA.
    """
    if not _is_discovery_host(url):
        # Stream/CDN: diretto
        return requests.get(url, headers=HEADERS, timeout=timeout)

    # Discovery: via proxy pubblici
    last_err = None
    for make_proxy in DISCOVERY_PROXIES:
        try:
            proxy_url = make_proxy(url)
            r = requests.get(proxy_url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and len(r.text) > 50:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"Discovery fallita per {url}: {last_err}")


def _decode_econfig(encoded: str) -> dict:
    """Decodifica _econfig: base64 → split 4 → rimuovi char[3] → riordina [2,0,3,1] → base64 → JSON"""
    raw = base64.b64decode(encoded).decode("utf-8", errors="ignore")

    parts = None
    for sep in ["|", ",", ";", ":"]:
        p = raw.split(sep)
        if len(p) == 4:
            parts = p
            break
    if parts is None:
        parts = re.split(r"[^A-Za-z0-9+/=]+", raw)

    if len(parts) != 4:
        raise ValueError(f"_econfig: attese 4 parti, trovate {len(parts)}")

    cleaned = [p[:3] + p[4:] if len(p) > 3 else p for p in parts]
    reordered = cleaned[2] + cleaned[0] + cleaned[3] + cleaned[1]
    decoded = base64.b64decode(reordered).decode("utf-8")
    return json.loads(decoded)


def fetch_channel_list() -> dict:
    """Ritorna {nome_canale: id} dalla pagina 24-7-channels"""
    global _channel_cache

    if _channel_cache is not None:
        return _channel_cache

    for mirror in MIRRORS:
        try:
            r = _fetch(f"{mirror}/24-7-channels.php")
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
        score = len(words & set(name.lower().split()))
        if score > best_score:
            best_score, best_id = score, cid

    if best_id and best_score >= max(1, len(words) - 1):
        return best_id

    raise ValueError(f"DaddyLive: canale '{query}' non trovato")


def _extract_real_stream(cid: int) -> str:
    """
    Catena completa: watch → player → iframe embed → _econfig → m3u8 VERO.
    Restituisce l'URL diretto del CDN (es. https://xxx.net:8443/hls/....m3u8?s=...&e=...)
    """
    # 1. Pagina watch (serve per cookie/referer logico)
    watch_url = f"{BASE}/watch.php?id={cid}"
    try:
        _fetch(watch_url, timeout=15)
    except Exception as e:
        logger.warning("Watch page fallita (continuo): %s", e)

    # 2. Player page
    player_url = f"{BASE}/cast/stream-{cid}.php"
    r = _fetch(player_url)
    player_html = r.text

    # 3. Cerca iframe (verso assetrage.net o altro embedder)
    m = re.search(
        r'iframe[^>]+src=["\'](https?://[^"\']+)["\']',
        player_html, re.IGNORECASE
    )

    if not m:
        # Fallback: m3u8 diretto nella pagina player
        m3 = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', player_html)
        if m3:
            return m3.group(0)
        raise RuntimeError(f"Nessun iframe/m3u8 nel player {player_url}")

    embed_url = m.group(1)

    # 4. Pagina embed → _econfig
    r2 = _fetch(embed_url)
    embed_html = r2.text

    m2 = re.search(r'_econfig\s*=\s*["\']([^"\']+)["\']', embed_html)
    if m2:
        try:
            cfg = _decode_econfig(m2.group(1))
            for key in ("stream_url", "url", "src", "file", "hls"):
                if cfg.get(key) and ".m3u8" in str(cfg[key]):
                    logger.info("Stream estratto da _econfig")
                    return str(cfg[key])
        except Exception as e:
            logger.warning("Decode _econfig fallito: %s", e)

    # Fallback: regex m3u8 nell'embed
    m3 = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', embed_html)
    if m3:
        logger.info("Stream estratto con regex diretta")
        return m3.group(0)

    raise RuntimeError(f"Stream non trovato per canale {cid}")


def get_stream(channel_name: str) -> dict:
    """Ritorna l'm3u8 VERO del CDN, da riprodurre DIRETTAMENTE"""
    channels = fetch_channel_list()
    cid = find_channel_id(channels, channel_name)

    real_url = _extract_real_stream(cid)

    return {
        "url": real_url,
        "headers": {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": EMBED_REFERER,
            "Origin": "https://assetrage.net",
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
    
