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
   album: str | None = None
   title: str | None = None
   year: int | None = None
   track: int | None = None
   disc: int | None = None


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


# ---------------------------------------------------------------------------
# MusicBrainz release lookup (used to enrich album.nfo from an embedded MBID)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MBTrack:
   position: int | None
   title: str
   length_ms: int | None
   recording_id: str
   artist_credit: str


@dataclass(frozen=True)
class MBRelease:
   release_id: str
   release_group_id: str
   title: str
   album_artist: str
   date: str
   year: str
   label: str
   barcode: str
   tracks: list[MBTrack]


def _mb_join_credit(credit) -> str:
   """Render an artist-credit list into a display string (handles feat./joins)."""
   if not isinstance(credit, list):
      return ""
   parts: list[str] = []
   for entry in credit:
      if not isinstance(entry, dict):
         continue
      name = entry.get("name") or (entry.get("artist") or {}).get("name") or ""
      join = entry.get("joinphrase") or ""
      if name:
         parts.append(f"{name}{join}")
   return "".join(parts).strip()


def fetch_mb_release(release_mbid: str, log_fn=None) -> MBRelease | None:
   """Fetch a MusicBrainz release (with recordings + credits + labels)."""
   inc = "recordings+artist-credits+labels+release-groups"
   params = urllib.parse.urlencode({"inc": inc, "fmt": "json"})
   url = f"https://musicbrainz.org/ws/2/release/{release_mbid}?{params}"
   try:
      data = _mb_get(url)
   except Exception as exc:
      _log_warn(f"MB RELEASE: lookup failed ({release_mbid}): {exc}", log_fn=log_fn)
      return None
   if not isinstance(data, dict) or not data.get("id"):
      return None
   return _parse_mb_release(data)


def resolve_release_from_group(rg_mbid: str, log_fn=None) -> str | None:
   """Pick a release id from a release-group MBID (first listed release)."""
   params = urllib.parse.urlencode({"inc": "releases", "fmt": "json"})
   url = f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}?{params}"
   try:
      data = _mb_get(url)
   except Exception as exc:
      _log_warn(f"MB GROUP: lookup failed ({rg_mbid}): {exc}", log_fn=log_fn)
      return None
   releases = data.get("releases") if isinstance(data, dict) else None
   if isinstance(releases, list):
      for rel in releases:
         if isinstance(rel, dict) and rel.get("id"):
            return rel["id"]
   return None


def _parse_mb_release(data: dict) -> MBRelease:
   date = str(data.get("date") or "")
   year_match = re.search(r"\b(19|20)\d{2}\b", date)

   label = ""
   for li in data.get("label-info") or []:
      lab = (li or {}).get("label") or {}
      if lab.get("name"):
         label = lab["name"]
         break

   tracks: list[MBTrack] = []
   for medium in data.get("media") or []:
      for t in (medium or {}).get("tracks") or []:
         if not isinstance(t, dict):
            continue
         rec = t.get("recording") or {}
         pos = t.get("position") or t.get("number")
         try:
            pos = int(pos) if pos is not None else None
         except (TypeError, ValueError):
            pos = None
         length = t.get("length") or rec.get("length")
         try:
            length = int(length) if length is not None else None
         except (TypeError, ValueError):
            length = None
         tracks.append(MBTrack(
            position=pos,
            title=str(rec.get("title") or t.get("title") or "").strip(),
            length_ms=length,
            recording_id=str(rec.get("id") or ""),
            artist_credit=_mb_join_credit(rec.get("artist-credit")),
         ))

   return MBRelease(
      release_id=str(data.get("id") or ""),
      release_group_id=str((data.get("release-group") or {}).get("id") or ""),
      title=str(data.get("title") or "").strip(),
      album_artist=_mb_join_credit(data.get("artist-credit")),
      date=date,
      year=year_match.group(0) if year_match else "",
      label=label,
      barcode=str(data.get("barcode") or "").strip(),
      tracks=tracks,
   )


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
      proc = subprocess.run(
         cmd, capture_output=True, text=True,
         encoding="utf-8", errors="replace", check=False, timeout=10,
      )
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
# Scraping providers (require --allow-provider-site-scraping)
#
# These talk to a site's undocumented browser backend / embedded page state
# rather than a public API, so they are gated behind the scraping opt-in.
# ---------------------------------------------------------------------------

_BROWSER_UA = (
   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def _http_get_text(url: str, headers: dict, timeout: int = 15) -> str | None:
   try:
      req = urllib.request.Request(url, headers=headers)
      with urllib.request.urlopen(req, timeout=timeout) as resp:
         return resp.read().decode("utf-8", errors="replace")
   except Exception:
      return None


def _http_get_json(url: str, headers: dict, timeout: int = 15) -> dict | None:
   text = _http_get_text(url, headers, timeout)
   if not text:
      return None
   try:
      return json.loads(text)
   except Exception:
      return None


# --- lyricradar.com (ls.1010diy.com backend) --------------------------------
# Flow: /songs?keyword=<query> returns candidates, each with a `song_info`
# string; /lyrics?keyword=<url-encoded song_info> returns {syncedLyrics,
# plainLyrics}. The backend aggregates several sources (QQ Music, Kugou,
# NetEase), so we take the first candidate that actually has lyrics.

_LYRICRADAR_HEADERS = {
   "Accept": "*/*",
   "Origin": "https://lyricradar.com",
   "Referer": "https://lyricradar.com/",
   "User-Agent": _BROWSER_UA,
}


def _fetch_lyricradar(query: str, *, log_fn=None, **_kwargs) -> FetchedLyrics | None:
   search_url = "https://ls.1010diy.com/songs?" + urllib.parse.urlencode({"keyword": query, "page": "1"})
   data = _http_get_json(search_url, _LYRICRADAR_HEADERS)
   results = (data or {}).get("data") or []
   if not isinstance(results, list):
      return None

   for item in results[:6]:
      if not isinstance(item, dict):
         continue
      song_info = item.get("song_info")
      if not song_info:
         continue
      lyr_url = "https://ls.1010diy.com/lyrics?" + urllib.parse.urlencode({"keyword": song_info})
      payload = _http_get_json(lyr_url, _LYRICRADAR_HEADERS)
      entry = (payload or {}).get("data") or {}
      synced = entry.get("syncedLyrics")
      plain = entry.get("plainLyrics")
      synced = synced if isinstance(synced, str) and synced.strip() else None
      plain_lines = _text_to_lines(plain) if isinstance(plain, str) else None
      if synced and not plain_lines:
         plain_lines = _text_to_lines(synced)
      if not synced and not plain_lines:
         continue

      artists = item.get("str_artist") or item.get("artist")
      artist = ", ".join(artists) if isinstance(artists, list) else (artists or None)
      duration = item.get("duration")
      meta = TrackMeta(
         artist=artist,
         album=item.get("album"),
         title=item.get("title"),
         duration_s=(float(duration) / 1000.0) if isinstance(duration, (int, float)) and duration else None,
      )
      _log_info(f"lyricradar: matched {item.get('full_title')!r} ({item.get('source')})", log_fn=log_fn)
      return FetchedLyrics(
         synced_lrc_text=synced,
         plain_text_lines=plain_lines,
         provider="lyricradar",
         query=query,
         cache_hit=False,
         meta=meta,
      )
   return None


# --- genius.com -------------------------------------------------------------
# Flow: /api/search/song?q=<query> returns song hits; the song page embeds
# window.__PRELOADED_STATE__ = JSON.parse('...'), whose songPage.lyricsData.body
# is a nested node tree. Flatten it to text, dropping ads and bracketed
# section/credit headers ([Chorus], [Produced by ...]).

_GENIUS_HEADERS = {"User-Agent": _BROWSER_UA, "Accept": "application/json, text/html"}
_GENIUS_STATE_MARKER = "__PRELOADED_STATE__ = JSON.parse('"


def _fetch_genius(query: str, *, log_fn=None, **_kwargs) -> FetchedLyrics | None:
   search_url = "https://genius.com/api/search/song?" + urllib.parse.urlencode({"q": query})
   data = _http_get_json(search_url, _GENIUS_HEADERS)
   result = _genius_first_song(data)
   if not result:
      _log_warn("genius: no search result", log_fn=log_fn)
      return None

   url = result.get("url")
   html = _http_get_text(url, _GENIUS_HEADERS, timeout=20) if url else None
   body = _genius_extract_body(html) if html else None
   lines = _genius_body_to_lines(body) if body else None
   if not lines:
      _log_warn(f"genius: no lyrics extracted for {url}", log_fn=log_fn)
      return None

   meta = TrackMeta(
      url=url,
      artist=(result.get("primary_artist") or {}).get("name"),
      title=result.get("title"),
   )
   _log_info(f"genius: matched {result.get('full_title')!r}", log_fn=log_fn)
   return FetchedLyrics(
      synced_lrc_text=None,
      plain_text_lines=lines,
      provider="genius",
      query=query,
      cache_hit=False,
      meta=meta,
   )


def _genius_first_song(data: dict | None) -> dict | None:
   response = (data or {}).get("response") or {}
   hits: list = []
   sections = response.get("sections")
   if isinstance(sections, list):
      for section in sections:
         hits.extend((section or {}).get("hits") or [])
   else:
      hits = response.get("hits") or []
   for hit in hits:
      result = (hit or {}).get("result") or {}
      if result.get("url"):
         return result
   return None


def _genius_extract_body(html: str):
   idx = html.find(_GENIUS_STATE_MARKER)
   if idx < 0:
      return None
   i = idx + len(_GENIUS_STATE_MARKER)
   buf: list[str] = []
   while i < len(html):
      ch = html[i]
      if ch == "\\":            # keep escape pairs intact for _js_unescape
         buf.append(html[i:i + 2])
         i += 2
         continue
      if ch == "'":             # unescaped quote ends the literal
         break
      buf.append(ch)
      i += 1
   try:
      state = json.loads(_js_unescape("".join(buf)))
   except Exception:
      return None
   return (((state.get("songPage") or {}).get("lyricsData") or {}).get("body"))


def _js_unescape(s: str) -> str:
   out: list[str] = []
   i = 0
   simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
             "\\": "\\", "'": "'", '"': '"', "/": "/"}
   while i < len(s):
      ch = s[i]
      if ch == "\\" and i + 1 < len(s):
         nxt = s[i + 1]
         if nxt == "u" and i + 6 <= len(s):
            try:
               out.append(chr(int(s[i + 2:i + 6], 16)))
               i += 6
               continue
            except ValueError:
               pass
         elif nxt == "x" and i + 4 <= len(s):
            try:
               out.append(chr(int(s[i + 2:i + 4], 16)))
               i += 4
               continue
            except ValueError:
               pass
         out.append(simple.get(nxt, nxt))
         i += 2
         continue
      out.append(ch)
      i += 1
   return "".join(out)


def _genius_body_to_lines(body) -> list[str] | None:
   pieces: list[str] = []
   _genius_flatten(body if isinstance(body, list) else (body or {}).get("children", []), pieces)
   lines: list[str] = []
   for raw in "".join(pieces).split("\n"):
      line = raw.strip()
      if not line:
         continue
      if re.fullmatch(r"\[.*\]", line):   # section / credit headers
         continue
      lines.append(line)
   return lines or None


def _genius_flatten(node, out: list[str]) -> None:
   if isinstance(node, str):
      out.append(node)
   elif isinstance(node, list):
      for child in node:
         _genius_flatten(child, out)
   elif isinstance(node, dict):
      tag = node.get("tag")
      if tag == "br":
         out.append("\n")
      elif tag == "inread-ad":
         return
      else:
         children = node.get("children")
         if children:
            _genius_flatten(children, out)


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
      Provider(
         name="lyricradar",
         description=(
            "lyricradar.com — multi-source lyrics (synced LRC + plain) via the "
            "site's browser backend. May be censored depending on the source."
         ),
         requires_scraping=True,
         fetch_fn=_fetch_lyricradar,
      ),
      Provider(
         name="genius",
         description="Genius.com — plain lyrics scraped from the song page",
         requires_scraping=True,
         fetch_fn=_fetch_genius,
      ),
   ]
   for p in _entries:
      PROVIDER_REGISTRY[p.name] = p


_register_providers()
