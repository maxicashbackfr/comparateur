"""Client HTTP poli : robots.txt respecté, cadence limitée, cache sur disque.

Ces trois règles ne sont pas du confort. Un crawler impoli se fait bloquer, et
un adaptateur bloqué est une source perdue — le risque n°1 du §11.
"""
from __future__ import annotations

import hashlib
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings


class RobotsDisallowed(RuntimeError):
    """Levée quand robots.txt interdit l'URL. Jamais rattrapée pour contourner."""


class PoliteClient:
    def __init__(self, settings: Settings, *, use_cache: bool = True) -> None:
        self.settings = settings
        self.use_cache = use_cache
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Language": "fr-FR,fr;q=0.9"},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        if use_cache:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- robots ---------------------------------------------------------------

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        host = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{host}/robots.txt")
            try:
                response = self._client.get(f"{host}/robots.txt")
                parser.parse(response.text.splitlines())
            except httpx.HTTPError:
                # robots.txt injoignable : on se comporte comme s'il interdisait
                # tout. Le silence n'est pas une autorisation.
                parser.disallow_all = True
            self._robots[host] = parser
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.settings.user_agent, url)

    # -- cache ----------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:24]
        return self.settings.cache_dir / f"{digest}.html"

    def _cached(self, url: str) -> str | None:
        if not self.use_cache:
            return None
        path = self._cache_path(url)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.settings.cache_ttl:
            return None
        return path.read_text(encoding="utf-8")

    # -- fetch ----------------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.settings.min_delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.time()

    def get(self, url: str) -> str:
        cached = self._cached(url)
        if cached is not None:
            return cached
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt interdit : {url}")
        self._throttle(url)
        response = self._client.get(url)
        response.raise_for_status()
        if self.use_cache:
            self._cache_path(url).write_text(response.text, encoding="utf-8")
        return response.text

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
