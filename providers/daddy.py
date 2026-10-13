"""Provider DaddyLive - stream via Val.town (unwrap segmenti self-healing).

Il CDN DaddyLive serve i segmenti travestiti da immagini (PNG/WebP TikTok).
L'Edge Function li scarica, li unwrappa in TS e li serve a VLC.
Su Cloudflare moriva dopo ~2 min (unwrap 191ms > muro CPU 10ms).
Su Val.town (1 min wall clock/run, 100k run/giorno) funziona.

URL stabili: https://simo11.val.run/daddy-stream/live?ch=<ID>
dove <ID> e' l'ID numerico del canale DaddyLive (es. 869).
"""

import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

WORKER_BASE = "https://simo11.val.run/daddy-stream"


def resolve(channel, stream):
    """URL stabile: l'Edge Function risolve e unwrappa DaddyLive a ogni richiesta."""
    query = stream.get("query") or channel.get("name", "")
    if not query:
        raise ValueError("DaddyLive: nessun ID canale configurato")

    encoded = quote(str(query))
    stable_url = f"{WORKER_BASE}/live?ch={encoded}"

    logger.info("DaddyLive: %s -> %s", query, stable_url)

    return {
        "url": stable_url,
        "provider": "DaddyLive",
        "quality": "HD",
        "headers": {},  # tutto gestito dall'Edge Function
        "extra_vlcopts": [],
        "proxy": "direct",
        "proxy_required": False,
    }
    
