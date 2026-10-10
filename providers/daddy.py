"""
DaddyLive provider - estrae l'm3u8 reale da bembed.sbs e lo instrada
attraverso il Cloudflare Worker che spacchetta i segmenti travestiti.
"""

import re
import time
import logging
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE = "https://dlive.sx"
MIRRORS = ["https://dlive.sx", "https://dlstreams.st"]
EMBED_TEMPLATE = "https://bembed.sbs/premiumtv/daddy.php?id={cid}"
PLAYER_REFERER = "https://dlive.sx/stream/stream-{cid}.php"

# IL TUO WORKER CLOUDFLARE
WORKER_BASE = "https://daddy-deploy.simo-scorrano-11.workers.dev"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Fallback gratuiti solo se il direct fallisce (GitHub IP bloccati)
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


def _fetch(url: str, referer: str = None, timeout: int = 12, must_contain: str = None) -> requests.Response:
    """Direct prima; fallback proxy CORS pubblici."""
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200 and len(r.content) > 50:
            if must_contain is None or must_contain in r.text:
                return r
    except Exception:
        pass

    for make_proxy in DISCOVERY_PROXIES:
        try:
            r = requests.get(make_proxy(url), headers=headers, timeout=timeout + 5)
            if r.status_code == 200 and len(r.text) > 50:
                if must_contain is None or must_contain in r.text:
                    return r
        except Exception:
            continue

    raise RuntimeError(f"Fetch fallita: {url[:70]}")


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
    """bembed.sbs contiene const SRC con l'm3u8 vero."""
    embed_url = EMBED_TEMPLATE.format(cid=cid)
    referer = PLAYER_REFERER.format(cid=cid)

    html = _fetch(embed_url, referer=referer, must_contain="const SRC").text

    m = re.search(r'const\s+SRC\s*=\s*"([^"]+?\.m3u8)"', html)
    if not m:
        raise RuntimeError(f"const SRC non trovato per canale {cid}")

    src = m.group(1)
    sep = "&" if "?" in src else "?"
    src = f"{src}{sep}_={int(time.time() * 1000)}"
    return src


def get_stream(channel_name: str) -> dict:
    channels = fetch_channel_list()
    cid = find_channel_id(channels, channel_name)
    real_url = _extract_real_stream(cid)

    # Instrada TUTTO attraverso il worker (manifest + segmenti)
    worker_url = f"{WORKER_BASE}/?u={quote(real_url, safe='')}"

    logger.info("STREAM VIA WORKER: %s", worker_url[:80])

    return {
        "url": worker_url,
        "headers": {},  # Il worker gestisce tutto da solo
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
