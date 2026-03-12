from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .console_ui import info, warn
from .template_tokens import expand_template

import unicodedata


DEFAULT_PROVIDER = "lrclib"
DEFAULT_TEMPLATE = "{Artist Name} {Track Title}"
_LRC_TS_RX = re.compile(r"^\s*\[?\d{1,2}:\d{2}(?:\.\d+)?\]?\s*")

# MusicBrainz requires a meaningful User-Agent:
# https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
_MB_USER_AGENT = "PyLy/1.0 ( https://github.com/pyly )"
_MB_RATE_LIMIT_S = 1.1  # MusicBrainz enforces ~1 req/sec per IP


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

@dataclass
class Provider:
   """
   Descriptor for a lyrics fetch provider.

   Attributes
   ----------
   name
       Canonical key used on the CLI (e.g. "lrclib").
   description
       One-line human description, shown by --list-providers.
   requires_scraping
       True when the provider works by scraping a website rather than
       calling a documented/public API.  PyLy refuses to use scraping
       providers unless --allow-provider-site-scraping is explicitly passed.
   fetch_fn
       Callable[[str, ...], FetchedLyrics | None].  Signature:
           fetch_fn(query: str, *, log_fn=None) -> FetchedLyrics | None
       Populated by _register_providers() after all fetch functions are
       defined at the bottom of this module.
   """
   name: str
   description: str
   requires_scraping: bool
   fetch_fn: Callable | None = field(default=None, repr=False)


# Keyed by provider name (lowercase).  Populated by _register_providers().
PROVIDER_REGISTRY: dict[str, Provider] = {}


def list_providers() -> list[Provider]:
   """Return all registered providers: API-only first, then scraping."""
   return sorted(PROVIDER_REGISTRY.values(), key=lambda p: (p.requires_scraping, p.name))


# ---------------------------------------------------------------------------
# Core config / result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FetchConfig:
   enabled: bool
   provider: str = DEFAULT_PROVIDER
   template: str = DEFAULT_TEMPLATE
   allow_scraping: bool = False
   artist: str | None = None
   title: str | None = None


@dataclass(frozen=True)
class TrackMeta:
   """Metadata about the fetched track from the provider."""
   provider_id: str | None = None
   mbid: str | None = None          # MusicBrainz recording MBID when available
   url: str | None = None
   artist: str | None = None
   album: str | None = None
   title: str | None = None
   duration_s: float | None = None  # seconds, from provider


@dataclass(frozen=True)
class FetchedLyrics:
   synced_lrc_text: str | None
   plain_text_lines: list[str] | None
   provider: str
   query: str
   cache_hit: bool
   meta: TrackMeta = field(default_factory=TrackMeta)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def parse_fetch_arg(value: str | None, allow_scraping: bool = False) -> FetchConfig | None:
   if value is None:
      return None

   raw = value.strip()
   if ":" in raw:
      provider, template = raw.split(":", 1)
      provider = provider.strip().lower() or DEFAULT_PROVIDER
      template = template.strip() or DEFAULT_TEMPLATE
      return FetchConfig(True, provider=provider, template=template,
                         allow_scraping=allow_scraping)

   if not raw:
      return FetchConfig(True, provider=DEFAULT_PROVIDER, template=DEFAULT_TEMPLATE,
                         allow_scraping=allow_scraping)

   lowered = raw.lower()
   if lowered in PROVIDER_REGISTRY:
      return FetchConfig(True, provider=lowered, template=DEFAULT_TEMPLATE,
                         allow_scraping=allow_scraping)

   # Treat as a freetext query template for the default provider
   return FetchConfig(True, provider=DEFAULT_PROVIDER, template=raw,
                      allow_scraping=allow_scraping)


def expand_query(template: str, audio_path: Path, layout: str | None = None) -> str:
   return expand_template(template, audio_path, layout=layout)


def _repair_mojibake(s: str) -> str:
   if not s:
      return s
   if "â" in s or "Ã" in s:
      for enc in ("cp1252", "latin-1"):
         try:
            fixed = s.encode(enc).decode("utf-8")
            if "â" not in fixed and "Ã" not in fixed:
               return unicodedata.normalize("NFC", fixed)
         except Exception:
            pass
   return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------------------
# Primary fetch entry point
# ---------------------------------------------------------------------------

def fetch_lyrics_data(
   config: FetchConfig,
   audio_path: Path,
   log_fn=None,
   layout: str | None = None,
) -> FetchedLyrics | None:
   provider_name = (config.provider or DEFAULT_PROVIDER).lower()
   template = config.template or DEFAULT_TEMPLATE
   query = expand_query(template, audio_path, layout=layout)
   query = _repair_mojibake(query)

   # Enrich config with structured artist/title from path guess if not already set
   if config.provider.lower() == "musicbrainz" and (not config.artist or not config.title):
      from .template_tokens import infer_path_guess
      guess = infer_path_guess(audio_path, layout)
      if guess.artist or guess.title:
         config = FetchConfig(
            enabled=config.enabled,
            provider=config.provider,
            template=config.template,
            allow_scraping=config.allow_scraping,
            artist=config.artist or guess.artist or None,
            title=config.title or guess.title or None,
         )

   if not query:
      _log_info("FETCH: skipped (empty query)", log_fn=log_fn)
      return None

   _log_info(f"FETCH: provider={provider_name} query={query}", log_fn=log_fn)

   # Registry lookup
   provider = PROVIDER_REGISTRY.get(provider_name)
   if provider is None:
      _log_warn(
         f"FETCH: unknown provider '{provider_name}' "
         f"(known: {', '.join(sorted(PROVIDER_REGISTRY))})",
         log_fn=log_fn,
      )
      return None

   # Scraping gate
   if provider.requires_scraping and not config.allow_scraping:
      _log_warn(
         f"FETCH: provider '{provider_name}' requires site scraping. "
         f"Pass --allow-provider-site-scraping to enable it.",
         log_fn=log_fn,
      )
      return None

   cache_path = _cache_path(provider_name, query)
   cached = _read_cache(cache_path)
   if cached is not None:
      _log_info(f"FETCH: cache hit ({provider_name})", log_fn=log_fn)
      if not cached.synced_lrc_text and not cached.plain_text_lines:
         return None
      return FetchedLyrics(
         synced_lrc_text=cached.synced_lrc_text,
         plain_text_lines=cached.plain_text_lines,
         provider=provider_name,
         query=query,
         cache_hit=True,
         meta=cached.meta,
      )

   _log_info(f"FETCH: cache miss ({provider_name})", log_fn=log_fn)

   try:
      fetched = provider.fetch_fn(query, log_fn=log_fn, config=config)
   except Exception as exc:
      _log_warn(f"FETCH: failed ({provider_name}) {exc}", log_fn=log_fn)
      return None

   if not fetched:
      _log_warn(f"FETCH: no lyrics found ({provider_name})", log_fn=log_fn)
      return None

   if not fetched.synced_lrc_text and not fetched.plain_text_lines:
      _log_warn(f"FETCH: no lyrics found ({provider_name})", log_fn=log_fn)
      return None

   _write_cache(cache_path, fetched)
   return FetchedLyrics(
      synced_lrc_text=fetched.synced_lrc_text,
      plain_text_lines=fetched.plain_text_lines,
      provider=provider_name,
      query=query,
      cache_hit=False,
      meta=fetched.meta,
   )


def fetch_by_url(url: str, log_fn=None) -> FetchedLyrics | None:
   """
   Fetch lyrics directly from a URL embedded via the [PyLy:<url>] tag.
   Supports lrclib API URLs and generic raw LRC responses.
   """
   url = url.strip()
   if not url:
      return None

   _log_info(f"FETCH URL: {url}", log_fn=log_fn)

   cache_path = _cache_path("url", url)
   cached = _read_cache(cache_path)
   if cached is not None:
      _log_info("FETCH URL: cache hit", log_fn=log_fn)
      if not cached.synced_lrc_text and not cached.plain_text_lines:
         return None
      return cached

   try:
      if "lrclib.net" in url:
         fetched = _fetch_lrclib_by_url(url)
      else:
         fetched = _fetch_raw_lrc_url(url)
   except Exception as exc:
      _log_warn(f"FETCH URL: failed {exc}", log_fn=log_fn)
      return None

   if fetched:
      _write_cache(cache_path, fetched)
   return fetched


def fetch_base_lyrics_lines(
   config: FetchConfig,
   audio_path: Path,
   log_fn=None,
   layout: str | None = None,
) -> list[str] | None:
   fetched = fetch_lyrics_data(config, audio_path, log_fn=log_fn, layout=layout, config=config)
   if not fetched:
      return None
   return fetched.plain_text_lines


# ---------------------------------------------------------------------------
# lrclib provider
# ---------------------------------------------------------------------------

def _fetch_lrclib(query: str, *, log_fn=None, **_kwargs) -> FetchedLyrics | None:
   """Freetext search against lrclib."""
   base = "https://lrclib.net/api/search"
   url = f"{base}?q={urllib.parse.quote_plus(query)}"
   req = urllib.request.Request(url, headers={"User-Agent": "PyLy/1.0"})

   with urllib.request.urlopen(req, timeout=8) as resp:
      payload = resp.read().decode("utf-8", errors="replace")

   data = json.loads(payload)
   return _extract_lrclib_lyrics(data, query=query)


def _fetch_lrclib_precise(
   artist: str,
   title: str,
   album: str = "",
   duration_s: float | None = None,
) -> FetchedLyrics | None:
   """
   Structured lrclib lookup using separate field params — far more accurate
   than freetext when we have clean MusicBrainz metadata.

   Endpoint: GET /api/get?artist_name=&track_name=&album_name=&duration=
   Returns a single best match or 404.
   """
   params: dict[str, str] = {
      "artist_name": artist,
      "track_name": title,
   }
   if album:
      params["album_name"] = album
   if duration_s is not None:
      params["duration"] = str(int(round(duration_s)))

   url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
   req = urllib.request.Request(url, headers={"User-Agent": "PyLy/1.0"})

   try:
      with urllib.request.urlopen(req, timeout=8) as resp:
         payload = resp.read().decode("utf-8", errors="replace")
      data = json.loads(payload)
      if isinstance(data, dict) and data.get("id"):
         return _lrclib_item_to_fetched(data, query=f"{artist} {title}")
   except Exception:
      pass
   return None


def _fetch_lrclib_by_url(url: str) -> FetchedLyrics | None:
   """
   Fetch a specific lrclib track by its API URL.
   e.g. https://lrclib.net/api/get/12345
   """
   api_url = url
   if not re.search(r"/api/get/\d+", url):
      match = re.search(r"/(\d+)(?:[?#].*)?$", url)
      if match:
         api_url = f"https://lrclib.net/api/get/{match.group(1)}"

   req = urllib.request.Request(api_url.rstrip("/"), headers={"User-Agent": "PyLy/1.0"})
   with urllib.request.urlopen(req, timeout=8) as resp:
      payload = resp.read().decode("utf-8", errors="replace")

   data = json.loads(payload)
   if isinstance(data, list):
      return _extract_lrclib_lyrics(data, query=url)
   return _lrclib_item_to_fetched(data, query=url) if isinstance(data, dict) else None


def _extract_lrclib_lyrics(data, query: str) -> FetchedLyrics | None:
   if isinstance(data, dict):
      return _lrclib_item_to_fetched(data, query=query)

   if isinstance(data, list):
      for item in data:
         if not isinstance(item, dict):
            continue
         entry = _lrclib_item_to_fetched(item, query=query)
         if entry and entry.synced_lrc_text:
            return entry
      for item in data:
         if not isinstance(item, dict):
            continue
         entry = _lrclib_item_to_fetched(item, query=query)
         if entry:
            return entry
   return None


def _lrclib_item_to_fetched(item: dict, query: str) -> FetchedLyrics | None:
   synced_text = _extract_synced_lrc_text(item)
   plain_text = item.get("plainLyrics") or item.get("lyrics")
   plain_lines = _text_to_lines(plain_text) if isinstance(plain_text, str) else None
   if synced_text and not plain_lines:
      plain_lines = _text_to_lines(synced_text)
   if not synced_text and not plain_lines:
      return None

   track_id = item.get("id")
   url = f"https://lrclib.net/api/get/{track_id}" if track_id else None
   duration = item.get("duration")

   meta = TrackMeta(
      provider_id=str(track_id) if track_id is not None else None,
      url=url,
      artist=item.get("artistName") or item.get("artist"),
      album=item.get("albumName") or item.get("album"),
      title=item.get("trackName") or item.get("title"),
      duration_s=float(duration) if duration is not None else None,
   )

   return FetchedLyrics(
      synced_lrc_text=synced_text,
      plain_text_lines=plain_lines,
      provider=DEFAULT_PROVIDER,
      query=query,
      cache_hit=False,
      meta=meta,
   )


def _extract_synced_lrc_text(item: dict) -> str | None:
   synced = item.get("syncedLyrics")
   if isinstance(synced, str) and synced.strip():
      return synced

   synced_lines = item.get("syncedLines") or item.get("synced_lyrics")
   if isinstance(synced_lines, list):
      lines: list[str] = []
      for row in synced_lines:
         if not isinstance(row, dict):
            continue
         ts = _line_timestamp(row)
         text = str(row.get("text", "")).strip()
         if ts and text:
            lines.append(f"[{ts}] {text}")
      if lines:
         return "\n".join(lines)
   return None


# ---------------------------------------------------------------------------
# MusicBrainz provider
#
# Strategy:
#   1. Search MusicBrainz recordings API → canonical artist/title/album/MBID/duration
#   2. Use that metadata for a structured lrclib /api/get lookup (much more
#      precise than freetext when field values are clean)
#   3. Fall back to lrclib freetext with the cleaned MB title/artist string
#
# The actual lyric text always comes from lrclib — MusicBrainz itself does
# not host lyric text.
# ---------------------------------------------------------------------------

_MB_SEARCH_URL = "https://musicbrainz.org/ws/2/recording"
_last_mb_request: float = 0.0


def _mb_get(url: str) -> dict:
   """Rate-limited GET to the MusicBrainz API (~1 req/sec enforced)."""
   global _last_mb_request
   elapsed = time.monotonic() - _last_mb_request
   if elapsed < _MB_RATE_LIMIT_S:
      time.sleep(_MB_RATE_LIMIT_S - elapsed)

   req = urllib.request.Request(
      url,
      headers={
         "User-Agent": _MB_USER_AGENT,
         "Accept": "application/json",
      },
   )
   with urllib.request.urlopen(req, timeout=10) as resp:
      payload = resp.read().decode("utf-8", errors="replace")
   _last_mb_request = time.monotonic()
   return json.loads(payload)


def _fetch_musicbrainz(query: str, *, config: FetchConfig | None = None, log_fn=None, **_kwargs) -> FetchedLyrics | None:
   """
   MusicBrainz → lrclib pipeline:
     1. Query MB recordings search to get canonical artist/title/album/MBID/duration
     2. Try lrclib structured /api/get with that metadata
     3. Fall back to lrclib freetext if structured lookup misses
   """
   if config and config.artist and config.title:
      mb_query = f'artist:"{config.artist}" AND recording:"{config.title}"'
   else:
      mb_query = query  # freetext fallback

   title_lower = (config.title or query).lower()
   exclude_remix = " AND NOT recording:remix" if "remix" not in title_lower else ""
   mb_query = f'artist:"{config.artist}" AND recording:"{config.title}"{exclude_remix}'

   params = urllib.parse.urlencode({
      "query": mb_query,
      "fmt": "json",
      "limit": "5",
   })
   search_url = f"{_MB_SEARCH_URL}?{params}"

   try:
      data = _mb_get(search_url)
   except Exception as exc:
      _log_warn(f"MB: search failed: {exc}", log_fn=log_fn)
      return None

   recordings = data.get("recordings") or []
   if not recordings:
      _log_warn("MB: no recordings found", log_fn=log_fn)
      return None

   noise_words = {"remix", "edit", "mix", "version", "instrumental", "karaoke"}
   query_words = set(query.lower().split())
   has_noise = query_words & noise_words

   filtered = [
      r for r in recordings
      if has_noise or not any(w in r.get("title", "").lower() for w in noise_words)
   ]
   recordings = filtered or recordings  # fall back to full list if everything got filtered

   best = max(recordings, key=lambda r: int(r.get("score", 0) or 0))
   mbid: str = best.get("id", "")
   mb_title: str = best.get("title", "")
   mb_duration_ms = best.get("length")
   mb_duration_s = mb_duration_ms / 1000.0 if mb_duration_ms else None

   # Build artist credit string
   credits = best.get("artist-credit") or []
   artist_parts: list[str] = []
   for credit in credits:
      if isinstance(credit, dict):
         name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
         joinphrase = credit.get("joinphrase") or ""
         if name:
            artist_parts.append(name + joinphrase)
   mb_artist = "".join(artist_parts).strip()

   # First release title as album
   mb_album = ""
   releases = best.get("releases") or []
   if releases and isinstance(releases[0], dict):
      mb_album = releases[0].get("title", "")

   _log_info(
      f"MB: resolved artist={mb_artist!r} title={mb_title!r} "
      f"album={mb_album!r} mbid={mbid} duration={mb_duration_s}",
      log_fn=log_fn,
   )

   # -- Attempt 1: structured lrclib lookup --
   if mb_artist and mb_title:
      result = _fetch_lrclib_precise(
         artist=mb_artist,
         title=mb_title,
         album=mb_album,
         duration_s=mb_duration_s,
      )
      if result:
         _log_info(f"MB: lrclib precise hit (lrclib id={result.meta.provider_id})", log_fn=log_fn)
         meta = TrackMeta(
            provider_id=result.meta.provider_id,
            mbid=mbid or None,
            url=result.meta.url,
            artist=mb_artist or result.meta.artist,
            album=mb_album or result.meta.album,
            title=mb_title or result.meta.title,
            duration_s=mb_duration_s or result.meta.duration_s,
         )
         return FetchedLyrics(
            synced_lrc_text=result.synced_lrc_text,
            plain_text_lines=result.plain_text_lines,
            provider="musicbrainz",
            query=query,
            cache_hit=False,
            meta=meta,
         )

   # -- Attempt 2: lrclib freetext with cleaned MB names --
   fb_query = f"{mb_artist} {mb_title}".strip() if (mb_artist and mb_title) else query
   _log_info(f"MB: precise miss, falling back to lrclib freetext ({fb_query!r})", log_fn=log_fn)

   result = _fetch_lrclib(fb_query)
   if result:
      meta = TrackMeta(
         provider_id=result.meta.provider_id,
         mbid=mbid or None,
         url=result.meta.url,
         artist=mb_artist or result.meta.artist,
         album=mb_album or result.meta.album,
         title=mb_title or result.meta.title,
         duration_s=mb_duration_s or result.meta.duration_s,
      )
      return FetchedLyrics(
         synced_lrc_text=result.synced_lrc_text,
         plain_text_lines=result.plain_text_lines,
         provider="musicbrainz",
         query=query,
         cache_hit=False,
         meta=meta,
      )

   return None


def _fetch_raw_lrc_url(url: str) -> FetchedLyrics | None:
   """Generic fallback: fetch URL, treat body as LRC text (or lrclib JSON)."""
   req = urllib.request.Request(url, headers={"User-Agent": "PyLy/1.0"})
   with urllib.request.urlopen(req, timeout=8) as resp:
      payload = resp.read().decode("utf-8", errors="replace")

   if not payload.strip():
      return None

   if payload.lstrip().startswith("{"):
      try:
         data = json.loads(payload)
         if isinstance(data, dict):
            return _lrclib_item_to_fetched(data, query=url)
      except Exception:
         pass

   synced_text = payload.strip()
   plain_lines = _text_to_lines(synced_text)
   return FetchedLyrics(
      synced_lrc_text=synced_text,
      plain_text_lines=plain_lines if plain_lines else None,
      provider="url",
      query=url,
      cache_hit=False,
      meta=TrackMeta(url=url),
   )


# ---------------------------------------------------------------------------
# LRC re-download: read embedded URL from existing .lrc
# ---------------------------------------------------------------------------

_PYLY_URL_TAG_RX = re.compile(r"^\[PyLy:(.+)\]\s*$", re.IGNORECASE)


def read_pyly_url_from_lrc(lrc_path: Path) -> str | None:
   """
   Scan an existing .lrc file for a [PyLy:<url>] header tag.
   Returns the URL string if found, else None.
   """
   try:
      for line in lrc_path.read_text(encoding="utf-8", errors="replace").splitlines():
         m = _PYLY_URL_TAG_RX.match(line.strip())
         if m:
            url = m.group(1).strip()
            if url:
               return url
   except Exception:
      pass
   return None


# ---------------------------------------------------------------------------
# Duration helper (local audio file)
# ---------------------------------------------------------------------------

def get_audio_duration_s(audio_path: Path) -> float | None:
   """Read duration from audio file via ffprobe. Returns seconds, or None."""
   import shutil
   import subprocess

   ffprobe = shutil.which("ffprobe")
   if not ffprobe:
      bundled = Path(__file__).resolve().parents[1] / "ff" / "ffprobe.exe"
      if bundled.exists():
         ffprobe = str(bundled)
      else:
         return None

   cmd = [
      ffprobe, "-v", "quiet",
      "-print_format", "json",
      "-show_format",
      str(audio_path),
   ]
   try:
      proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
      data = json.loads(proc.stdout or "{}")
      dur = (data.get("format") or {}).get("duration")
      if dur is not None:
         return float(dur)
   except Exception:
      pass
   return None


def format_lrc_duration(seconds: float) -> str:
   """Format seconds as mm:ss.xx for [length:] LRC tag."""
   if seconds < 0:
      seconds = 0.0
   total_cs = int(round(seconds * 100))
   mm = total_cs // (60 * 100)
   ss = (total_cs // 100) % 60
   xx = total_cs % 100
   return f"{mm:02d}:{ss:02d}.{xx:02d}"


# ---------------------------------------------------------------------------
# LRC header builder
# ---------------------------------------------------------------------------

def build_lrc_metadata_headers(
   fetched: FetchedLyrics | None,
   audio_path: Path | None = None,
   include_standard_tags: bool = True,
) -> list[str]:
   """
   Build LRC header tag lines from fetch metadata + local audio duration.

   Standard LRC tags : [ar:], [al:], [ti:], [length:], [url:], [id:]
   PyLy extension    : [PyLy:<url>] re-download marker, [re:PyLy], [by:PyLy]

   [url:] is the standard/readable source URL tag understood by some players.
   [PyLy:<url>] is PyLy's re-download marker pointing at the exact API
   endpoint — kept separate so the two serve different consumers cleanly.
   """
   headers: list[str] = []

   if include_standard_tags:
      meta = fetched.meta if fetched else TrackMeta()

      if meta.artist:
         headers.append(f"[ar:{meta.artist}]")
      if meta.album:
         headers.append(f"[al:{meta.album}]")
      if meta.title:
         headers.append(f"[ti:{meta.title}]")

      # Duration: prefer local audio measurement, fall back to provider value
      duration_s: float | None = None
      if audio_path:
         duration_s = get_audio_duration_s(audio_path)
      if duration_s is None and meta.duration_s is not None:
         duration_s = meta.duration_s
      if duration_s is not None:
         headers.append(f"[length:{format_lrc_duration(duration_s)}]")

      if meta.url:
         headers.append(f"[url:{meta.url}]")        # standard player tag
      if meta.provider_id:
         headers.append(f"[id:{meta.provider_id}]")
      if meta.mbid:
         headers.append(f"[mbid:{meta.mbid}]")      # MusicBrainz recording ID
      if meta.url:
         headers.append(f"[PyLy:{meta.url}]")       # re-download marker

   headers.append("[re:PyLy]")
   headers.append("[by:PyLy]")

   return headers


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _line_timestamp(row: dict) -> str | None:
   if isinstance(row.get("timestamp"), str):
      ts = row["timestamp"].strip()
      if ts:
         return ts

   for key in ("time", "start", "startTime", "start_time"):
      val = row.get(key)
      if isinstance(val, (int, float)):
         return _seconds_to_lrc_timestamp(float(val))
      if isinstance(val, str):
         sval = val.strip()
         if not sval:
            continue
         if ":" in sval:
            return sval
         try:
            return _seconds_to_lrc_timestamp(float(sval))
         except ValueError:
            continue
   return None


def _seconds_to_lrc_timestamp(seconds: float) -> str:
   if seconds < 0:
      seconds = 0.0
   total_centis = int(round(seconds * 100.0))
   mins, rem = divmod(total_centis, 6000)
   secs, centis = divmod(rem, 100)
   return f"{mins:02d}:{secs:02d}.{centis:02d}"


def _text_to_lines(text: str | None) -> list[str]:
   if not text:
      return []
   lines: list[str] = []
   for raw in text.splitlines():
      s = raw.strip()
      if not s:
         continue
      s = _LRC_TS_RX.sub("", s).strip()
      if s:
         lines.append(s)
   return lines


def _cache_path(provider: str, query: str) -> Path:
   key = f"{provider}:{query}".encode("utf-8")
   digest = hashlib.sha256(key).hexdigest()
   cache_dir = Path(".pyly_cache")
   cache_dir.mkdir(parents=True, exist_ok=True)
   return cache_dir / f"{provider}-{digest}.json"


def _read_cache(path: Path) -> FetchedLyrics | None:
   try:
      raw = json.loads(path.read_text(encoding="utf-8"))
   except Exception:
      return None

   # Backward-compatible old cache schema (plain lines list only)
   if isinstance(raw, dict) and isinstance(raw.get("lines"), list):
      lines = [str(x) for x in raw["lines"] if str(x).strip()]
      return FetchedLyrics(
         synced_lrc_text=None,
         plain_text_lines=lines,
         provider=DEFAULT_PROVIDER,
         query="",
         cache_hit=True,
         meta=TrackMeta(),
      )

   if isinstance(raw, dict):
      synced = raw.get("synced_lrc_text")
      plain = raw.get("plain_text_lines")
      synced_value = synced if isinstance(synced, str) and synced.strip() else None
      plain_value = [str(x) for x in plain if str(x).strip()] if isinstance(plain, list) else None

      meta_raw = raw.get("meta") or {}
      meta = TrackMeta(
         provider_id=meta_raw.get("provider_id"),
         mbid=meta_raw.get("mbid"),
         url=meta_raw.get("url"),
         artist=meta_raw.get("artist"),
         album=meta_raw.get("album"),
         title=meta_raw.get("title"),
         duration_s=float(meta_raw["duration_s"]) if meta_raw.get("duration_s") is not None else None,
      ) if isinstance(meta_raw, dict) else TrackMeta()

      if synced_value or plain_value:
         return FetchedLyrics(
            synced_lrc_text=synced_value,
            plain_text_lines=plain_value,
            provider=DEFAULT_PROVIDER,
            query="",
            cache_hit=True,
            meta=meta,
         )
   return None


def _write_cache(path: Path, fetched: FetchedLyrics) -> None:
   try:
      meta = fetched.meta
      payload = {
         "synced_lrc_text": fetched.synced_lrc_text,
         "plain_text_lines": fetched.plain_text_lines,
         "meta": {
            "provider_id": meta.provider_id,
            "mbid": meta.mbid,
            "url": meta.url,
            "artist": meta.artist,
            "album": meta.album,
            "title": meta.title,
            "duration_s": meta.duration_s,
         },
      }
      path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
   except Exception:
      pass


def _log_info(message: str, log_fn=None) -> None:
   print(info(message))
   if log_fn:
      log_fn(message)


def _log_warn(message: str, log_fn=None) -> None:
   print(warn(message))
   if log_fn:
      log_fn(message)


# ---------------------------------------------------------------------------
# Provider registration
# Placed at the bottom so all fetch functions are defined before we reference
# them.  Add new providers here — nowhere else needs to change.
# ---------------------------------------------------------------------------

def _register_providers() -> None:
   _entries = [
      Provider(
         name="lrclib",
         description="lrclib.net — free, open-source LRC database (API)",
         requires_scraping=False,
         fetch_fn=_fetch_lrclib,
      ),
      Provider(
         name="musicbrainz",
         description=(
            "MusicBrainz metadata resolver + lrclib structured lookup (API). "
            "Adds recording MBID; respects MB rate limit (~1 req/sec)."
         ),
         requires_scraping=False,
         fetch_fn=_fetch_musicbrainz,
      ),
      # --------------- scraping providers go below this line ---------------
      # Example (not yet implemented):
      # Provider(
      #     name="genius",
      #     description="Genius.com — plain lyrics via site scraping",
      #     requires_scraping=True,
      #     fetch_fn=_fetch_genius,
      # ),
   ]
   for p in _entries:
      PROVIDER_REGISTRY[p.name] = p


_register_providers()
