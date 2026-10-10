"""
DaddyLive provider - TRASFORMA la pagina web in m3u8 reale.

Catena: watch.php → player → iframe embed → _econfig → m3u8 VERO (CDN)
Direct-first: funziona da GitHub Actions (dlive.sx non blocca sempre).
Fallback proxy CORS pubblici gratuiti se il direct fallisce.
"""

import re
import base64
import json
import logging
from urllib.parse import quote, urlparse

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

# Fallback gratuiti SOLO se il direct fallisce (GitHub IP bloccato quel giorno)
DISCOVERY_PROXIES = [
    lambda u: "https://api.allorigins.win/raw?url=" + quote(u, safe=""),
    lambda u: "https://api.codetabs.com/v1/proxy?quest=" + u,
    lambda u: "https://corsproxy.io/?url=" + quote(u, safe=""),
    lambda u: "https://api.cors.lol/?url=" + quote(u, safe=""),
]

_channel_cache = None


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=1, backoff_factor=0.3, status_forcelist=[429, 503])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _is_discovery_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return ("dlive" in host) or ("assetrage" in host)


def _fetch(url: str, timeout: int = 12) -> requests.Response:
    """Direct prima; fallback proxy pubblici solo per pagine dlive/assetrage"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200 and len(r.content) > 50:
            return r
    except Exception:
        pass

    if not _is_discovery_host(url):
        raise RuntimeError(f"Host non raggiungibile: {url[:60]}")

    last_err = None
    for make_proxy in DISCOVERY_PROXIES:
        try:
            r = requests.get(make_proxy(url), headers=HEADERS, timeout=timeout + 5)
            if r.status_code == 200 and len(r.text) > 50:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"Discovery fallita ({url[:60]}): {last_err}")


def _decode_econfig(encoded: str) -> dict:
    """base64 → split 4 parti → rimuovi char[3] → riordina [2,0,3,1] → base64 → JSON"""
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
    return json.loads(base64.b64decode(reordered).decode("utf-8"))


def fetch_channel_list() -> dict:
    global _channel_cache
    if _channel_cache is not None:
        return _channel_cache

    for mirror in MIRRORS:
        try:
            html = _fetch(f"{mirror}/24-7-channels.php").text
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
    """LA MAGIA: pagina web → m3u8 vero"""
    try:
        _fetch(f"{BASE}/watch.php?id={cid}", timeout=8)
    except Exception:
        pass

    player_html = _fetch(f"{BASE}/cast/stream-{cid}.php").text

    m = re.search(
        r'iframe[^>]+src=["\'](https?://[^"\']+)["\']',
        player_html, re.IGNORECASE
    )
    if not m:
        m3 = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', player_html)
        if m3:
            return m3.group(0)
        raise RuntimeError("Nessun iframe/m3u8 nel player")

    embed_html = _fetch(m.group(1)).text

    m2 = re.search(r'_econfig\s*=\s*["\']([^"\']+)["\']', embed_html)
    if m2:
        try:
            cfg = _decode_econfig(m2.group(1))
            for key in ("stream_url", "url", "src", "file", "hls"):
                if cfg.get(key) and ".m3u8" in str(cfg[key]):
                    return str(cfg[key])
        except Exception:
            pass

    m3 = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', embed_html)
    if m3:
        return m3.group(0)

    raise RuntimeError(f"Stream non trovato per canale {cid}")


def get_stream(channel_name: str) -> dict:
    channels = fetch_channel_list()
    cid = find_channel_id(channels, channel_name)
    real_url = _extract_real_stream(cid)

    logger.info("STREAM REALE: %s", real_url[:80])

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
