"""
Cover art: --embed-art / -g.

Finds an album's front cover and embeds it into the audio files.

Where the image comes from (resolved once per album folder, never per track):

* without --overwrite
    A local image in the audio file's folder wins:
    ``(album|poster|default|cover|folder).<jpg|jpeg|png|webp|bmp|gif>``, in
    that priority order, case-insensitive. Only when there is none is the
    provider asked. Files that already carry embedded art are skipped before
    any of this happens, so a finished album costs no network calls at all.
* with --overwrite
    The provider is asked even when a local image exists, and existing
    embedded art is replaced. When the provider finds nothing, the local
    image is used instead.

Downloaded images live in a temp folder for the run and are never written into
the music folder. ffmpeg embeds with stream copy (the audio is never
re-encoded); mp3/flac/m4a take only JPEG or PNG, so webp/bmp/gif are converted
to a temporary JPEG first. The user's own image file is never modified.

Providers, and what they need:

    musicbrainz  Cover Art Archive. No key. Uses the MBIDs in album.nfo, else
                 a MusicBrainz release-group search (shares the ~1 req/s
                 limiter in lyrics_fetch).
    theaudiodb   searchalbum.php. Optional key; without one, the public test
                 key from the docs. Held to 20 requests/minute regardless.
    apple        iTunes Search API. No key.
    discogs      Database search. Needs a personal access token.
    spotify      Web API search. Needs "client_id:client_secret".
    youtube      Data API v3 search thumbnail. Needs an API key.
    google       Not available: no free official image-search API.
    amazon       Not available: the product API needs an affiliate account.

Keys come from (first found wins): the inline ``provider=KEY`` value,
$PYLY_<PROVIDER>_KEY, then ~/.pyly/<provider>-key. They are only ever sent to
the provider's own API host, and never printed, cached or logged.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .console_ui import warn
from .file_metadata import (
   _ffmpeg_exe,
   _ffprobe_exe,
   _norm_loose,
   _strip_trailing_qualifiers,
   actual_metadata,
   expected_metadata,
   find_album_nfo,
   find_artist_nfo,
   read_embedded_tags,
   read_nfo_mbids,
   replace_with_backup,
)
from .lyrics_fetch import _MB_USER_AGENT, _mb_get, _mxm_artist_ok, _mxm_sim, fetch_mb_release


ART_PROVIDERS = ("musicbrainz", "theaudiodb", "discogs", "google", "amazon", "apple", "spotify", "youtube")
DEFAULT_ART_PROVIDER = "musicbrainz"

# Providers that can't be offered cleanly. Named here (rather than unknown) so
# the error says why instead of "unknown provider".
_UNAVAILABLE = {
   "google": "Google has no free official image-search API, and scraping Google Images is too brittle to ship",
   "amazon": "Amazon's product API needs an approved affiliate account, and scraping amazon.com is too brittle to ship",
}
_NO_KEY = {"musicbrainz", "apple"}                    # never take a key
_KEY_REQUIRED = {"discogs", "spotify", "youtube"}     # skipped (with a warning) without one

# Local image names, highest priority first; within one name, JPEG beats PNG
# beats the formats that need converting.
LOCAL_ART_NAMES = ("album", "poster", "default", "cover", "folder")
LOCAL_ART_EXTS = ("jpg", "jpeg", "png", "webp", "bmp", "gif")

# Containers ffmpeg can write an attached picture into with stream copy.
_EMBEDDABLE = {".mp3", ".flac", ".m4a"}
_NO_ART_CONTAINERS = {".ogg", ".opus"}   # ffmpeg can't write cover art here

_MAX_IMAGE_BYTES = 30 * 1024 * 1024


# ---------------------------------------------------------------------------
# --embed-art argument parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtSpec:
   provider: str
   key: str | None = None

   def __repr__(self) -> str:
      # Keep the key out of tracebacks and debug prints.
      return f"ArtSpec(provider={self.provider!r}, key={'***' if self.key else None})"


_PATHLIKE_RX = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/~.])")


def parse_embed_art_arg(value) -> ArtSpec:
   """
   argparse ``type=`` for --embed-art: ``provider`` or ``provider=KEY``.

   Only '=' separates the key, split on the first one, so a key may itself
   contain '=', ':', '/', '+', '$' or spaces. ':' is never a separator here
   (--fetch uses it for provider:template); ``theaudiodb:KEY`` is rejected
   with a hint. Error messages never echo a key.
   """
   if isinstance(value, ArtSpec):
      return value
   raw = (value or "").strip()
   if not raw:
      return ArtSpec(DEFAULT_ART_PROVIDER)

   name, sep, key = raw.partition("=")
   name = name.strip().lower()

   if name not in ART_PROVIDERS:
      choices = ", ".join(ART_PROVIDERS)
      head = raw.split(":", 1)[0].strip().lower()
      if not sep and ":" in raw and head in ART_PROVIDERS:
         raise argparse.ArgumentTypeError(
            f"'{head}:…' is not valid; use {head}=KEY (keys go after '=')"
         )
      if not sep and (_PATHLIKE_RX.match(raw) or Path(raw).exists()):
         raise argparse.ArgumentTypeError(
            f"{raw!r} looks like a path, not an art provider; put the path before "
            f"-g (pyly \"<path>\" -g), or name the provider (-g musicbrainz)"
         )
      shown = name.split(":", 1)[0][:40]
      raise argparse.ArgumentTypeError(f"unknown art provider '{shown}'; choose from {choices}")

   if name in _UNAVAILABLE:
      raise argparse.ArgumentTypeError(f"art provider '{name}' is not available: {_UNAVAILABLE[name]}")

   key = key.strip() or None
   if key and name in _NO_KEY:
      raise argparse.ArgumentTypeError(f"art provider '{name}' takes no key")
   return ArtSpec(name, key)


def resolve_art_key(provider: str, inline: str | None = None) -> str | None:
   """
   The key for ``provider``. Sources, first found wins: the inline
   ``provider=KEY`` value, $PYLY_<PROVIDER>_KEY, ~/.pyly/<provider>-key.
   """
   if inline:
      return inline
   env = os.environ.get(f"PYLY_{provider.upper()}_KEY", "").strip()
   if env:
      return env
   path = Path.home() / ".pyly" / f"{provider}-key"
   if path.is_file():
      text = path.read_text(encoding="utf-8", errors="replace").strip()
      return text or None
   return None


def describe_art_providers() -> list[tuple[str, str]]:
   """(name, description) rows for --list-providers."""
   return [
      ("musicbrainz", "Cover Art Archive, via album.nfo's MBID or a MusicBrainz search. No key. (default)"),
      ("theaudiodb", "TheAudioDB album search. Optional key (public test key otherwise); max 20 requests/min."),
      ("apple", "iTunes Search API, artwork scaled to 1000x1000. No key."),
      ("discogs", "Discogs database search. Needs a personal access token."),
      ("spotify", "Spotify Web API search. Needs client_id:client_secret."),
      ("youtube", "YouTube Data API search thumbnail, cropped square. Needs an API key."),
      ("google", f"Not available: {_UNAVAILABLE['google']}."),
      ("amazon", f"Not available: {_UNAVAILABLE['amazon']}."),
   ]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class SlidingWindowLimiter:
   """
   At most ``max_calls`` in any ``window_s`` seconds (a true sliding window,
   not a fixed bucket). ``clock``/``sleep`` are injectable for tests.
   """

   def __init__(self, max_calls: int, window_s: float, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep):
      self.max_calls = max_calls
      self.window_s = window_s
      self._clock = clock
      self._sleep = sleep
      self._stamps: collections.deque[float] = collections.deque()

   def acquire(self) -> None:
      while True:
         now = self._clock()
         while self._stamps and now - self._stamps[0] >= self.window_s:
            self._stamps.popleft()
         if len(self._stamps) < self.max_calls:
            self._stamps.append(now)
            return
         self._sleep(self._stamps[0] + self.window_s - now)


# TheAudioDB is a free service: stay well under its published 30/min. Every
# request counts, image downloads from its CDN included.
_TADB_LIMIT = SlidingWindowLimiter(20, 60.0)
_ITUNES_LIMIT = SlidingWindowLimiter(20, 60.0)    # Apple documents ~20 calls/min
_DISCOGS_LIMIT = SlidingWindowLimiter(30, 60.0)   # authenticated limit is 60/min


# ---------------------------------------------------------------------------
# HTTP / images
# ---------------------------------------------------------------------------

_UA = _MB_USER_AGENT


def _http(url: str, headers: dict | None = None, data: bytes | None = None, limit: int = 4 * 1024 * 1024) -> bytes:
   req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})}, data=data)
   with urllib.request.urlopen(req, timeout=20) as resp:
      body = resp.read(limit + 1)
   if len(body) > limit:
      raise ValueError("response too large")
   return body


def _http_json(url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
   return json.loads(_http(url, headers, data).decode("utf-8", errors="replace"))


def _describe_error(exc: Exception) -> str:
   """A short reason that never includes the URL (which may carry a key)."""
   if isinstance(exc, urllib.error.HTTPError):
      return f"HTTP {exc.code}"
   if isinstance(exc, urllib.error.URLError):
      return "network error"
   return type(exc).__name__


def _sniff_ext(data: bytes) -> str | None:
   if data[:3] == b"\xff\xd8\xff":
      return "jpg"
   if data[:8] == b"\x89PNG\r\n\x1a\n":
      return "png"
   if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
      return "webp"
   if data[:4] in (b"GIF8",):
      return "gif"
   if data[:2] == b"BM":
      return "bmp"
   return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _album_ok(wanted: str, got: str | None) -> bool:
   """Fuzzy album match that ignores edition suffixes like '(2011 Remaster)'."""
   w = _norm_loose(_strip_trailing_qualifiers(wanted or ""))
   g = _norm_loose(_strip_trailing_qualifiers(got or ""))
   if not w or not g:
      return False
   return w == g or _mxm_sim(w, g) >= 0.85


def _match(q: "AlbumQuery", artist: str | None, album: str | None) -> bool:
   return _mxm_artist_ok(q.artist, artist) and _album_ok(q.album, album)


# ---------------------------------------------------------------------------
# Album identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AlbumQuery:
   artist: str
   album: str
   release_id: str = ""
   group_id: str = ""


def album_query(audio: Path, layout: str | None = None) -> AlbumQuery:
   """
   Who/what the album is. album.nfo is canonical (and carries the MBIDs);
   then the embedded tags (already corrected when -Z ran first); then the
   folder layout.
   """
   album_nfo = find_album_nfo(audio)
   tags = read_embedded_tags(audio)
   actual = actual_metadata(tags)
   expected = expected_metadata(audio, layout, album_nfo, find_artist_nfo(audio))

   artist = (album_nfo.album_artist if album_nfo else "") or tags.get("album_artist", "") \
      or actual["artist"] or expected["artist"]
   album = (album_nfo.album_title if album_nfo else "") or actual["album"] or expected["album"]
   release_id = group_id = ""
   if album_nfo:
      release_id, group_id = read_nfo_mbids(album_nfo.path)
   return AlbumQuery(artist=artist.strip(), album=album.strip(), release_id=release_id, group_id=group_id)


# ---------------------------------------------------------------------------
# Local images
# ---------------------------------------------------------------------------

def find_local_art(folder: Path) -> Path | None:
   """
   The best local cover in ``folder``: name priority album > poster > default
   > cover > folder, then extension jpg/jpeg > png > webp/bmp/gif.
   Case-insensitive.
   """
   try:
      entries = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
   except OSError:
      return None
   for base in LOCAL_ART_NAMES:
      for ext in LOCAL_ART_EXTS:
         hit = entries.get(f"{base}.{ext}")
         if hit is not None:
            return hit
   return None


# ---------------------------------------------------------------------------
# Providers
#
# Each takes (query, key, ctx) and returns a downloaded image path or None.
# They raise on transport errors; the resolver reports those once.
# ---------------------------------------------------------------------------

class _Ctx:
   """Per-run scratch: the temp folder, plus warnings already shown."""

   def __init__(self):
      self.workdir = Path(tempfile.mkdtemp(prefix="pyly_art_"))
      self._n = 0
      self.warned: set[str] = set()
      self.spotify_token: str | None = None

   def download(self, url: str, headers: dict | None = None, limiter: SlidingWindowLimiter | None = None) -> Path | None:
      """Fetch an image into the temp folder. None on 404 or non-image data."""
      if limiter:
         limiter.acquire()
      try:
         data = _http(url, headers, limit=_MAX_IMAGE_BYTES)
      except urllib.error.HTTPError as exc:
         if exc.code == 404:
            return None
         raise
      ext = _sniff_ext(data)
      if not ext:
         return None
      self._n += 1
      path = self.workdir / f"art_{self._n}.{ext}"
      path.write_bytes(data)
      return path

   def warn_once(self, tag: str, message: str) -> None:
      if tag not in self.warned:
         self.warned.add(tag)
         print(warn(message))

   def close(self) -> None:
      shutil.rmtree(self.workdir, ignore_errors=True)


_CAA = "https://coverartarchive.org"


def _art_musicbrainz(q: AlbumQuery, key: str | None, ctx: _Ctx) -> Path | None:
   # No key. An MBID from album.nfo is the album by definition; a searched
   # one is fuzzy-checked against artist + album first.
   targets: list[tuple[str, str]] = []
   if q.release_id:
      targets.append(("release", q.release_id))
   if q.group_id:
      targets.append(("release-group", q.group_id))
   if not targets:
      group = _mb_find_release_group(q)
      if group:
         targets.append(("release-group", group))

   tried_group = bool(q.group_id)
   while targets:
      kind, mbid = targets.pop(0)
      for size in ("front-1200", "front-500", "front"):
         path = ctx.download(f"{_CAA}/{kind}/{mbid}/{size}", {"User-Agent": _MB_USER_AGENT})
         if path:
            return path
      if kind == "release" and not tried_group:
         # This pressing has no front cover; any release in its group will do.
         tried_group = True
         release = fetch_mb_release(mbid)
         if release and release.release_group_id:
            targets.append(("release-group", release.release_group_id))
   return None


def _mb_find_release_group(q: AlbumQuery) -> str | None:
   if not (q.artist and q.album):
      return None
   clean = lambda s: re.sub(r'["\\]', " ", s)
   lucene = f'releasegroup:"{clean(q.album)}" AND artist:"{clean(q.artist)}"'
   data = _mb_get("https://musicbrainz.org/ws/2/release-group?" + urllib.parse.urlencode(
      {"query": lucene, "fmt": "json", "limit": 5}))
   for group in data.get("release-groups") or []:
      credit = "".join(
         (c.get("name") or "") + (c.get("joinphrase") or "") for c in group.get("artist-credit") or []
      )
      if _match(q, credit, group.get("title")):
         return group.get("id")
   return None


# TheAudioDB's public test key, from https://www.theaudiodb.com/free_music_api.
# A personal (premium) key works on the same v1 endpoint, in the same place;
# v2's X-API-KEY header is only needed for v2-only endpoints, which we don't use.
_TADB_FREE_KEY = "123"


def _art_theaudiodb(q: AlbumQuery, key: str | None, ctx: _Ctx) -> Path | None:
   _TADB_LIMIT.acquire()
   api_key = urllib.parse.quote(key or _TADB_FREE_KEY, safe="")
   url = (f"https://www.theaudiodb.com/api/v1/json/{api_key}/searchalbum.php?"
          + urllib.parse.urlencode({"s": q.artist, "a": q.album}))
   data = _http_json(url)
   for item in data.get("album") or []:
      if not _match(q, item.get("strArtist"), item.get("strAlbum")):
         continue
      for field in ("strAlbumThumbHQ", "strAlbumThumb"):
         if item.get(field):
            path = ctx.download(item[field], limiter=_TADB_LIMIT)
            if path:
               return path
   return None


def _art_apple(q: AlbumQuery, key: str | None, ctx: _Ctx) -> Path | None:
   # iTunes Search API: no key. artworkUrl100 ends in /100x100bb.jpg; the
   # same path serves any size, so ask for 1000x1000.
   _ITUNES_LIMIT.acquire()
   url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
      {"term": f"{q.artist} {q.album}", "entity": "album", "limit": 10})
   data = _http_json(url)
   for item in data.get("results") or []:
      if not _match(q, item.get("artistName"), item.get("collectionName")):
         continue
      art = item.get("artworkUrl100") or item.get("artworkUrl60")
      if art:
         big = re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/1000x1000bb.\1", art)
         path = ctx.download(big, limiter=_ITUNES_LIMIT)
         if path:
            return path
   return None


def _art_discogs(q: AlbumQuery, key: str | None, ctx: _Ctx) -> Path | None:
   # Needs a personal access token (Settings → Developers on discogs.com).
   headers = {"Authorization": f"Discogs token={key}"}
   for kind in ("master", "release"):
      _DISCOGS_LIMIT.acquire()
      url = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(
         {"type": kind, "artist": q.artist, "release_title": q.album, "per_page": 10})
      data = _http_json(url, headers)
      for item in data.get("results") or []:
         artist, _, album = (item.get("title") or "").partition(" - ")
         art = item.get("cover_image") or ""
         if not art or art.endswith("spacer.gif") or not _match(q, artist, album):
            continue
         # The image host gets no token.
         path = ctx.download(art, limiter=_DISCOGS_LIMIT)
         if path:
            return path
   return None


def _art_spotify(q: AlbumQuery, key: str | None, ctx: _Ctx) -> Path | None:
   # Needs app credentials as "client_id:client_secret" (client-credentials
   # flow). Search is capped at 10 results for new apps.
   client_id, sep, secret = (key or "").partition(":")
   if not (sep and client_id and secret):
      ctx.warn_once("spotify-key", "spotify: the key must be client_id:client_secret; skipping spotify")
      return None
   if not ctx.spotify_token:
      basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
      token = _http_json(
         "https://accounts.spotify.com/api/token",
         {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
         data=b"grant_type=client_credentials",
      )
      ctx.spotify_token = token.get("access_token")
      if not ctx.spotify_token:
         return None
   url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
      {"q": f"album:{q.album} artist:{q.artist}", "type": "album", "limit": 10})
   data = _http_json(url, {"Authorization": f"Bearer {ctx.spotify_token}"})
   for item in (data.get("albums") or {}).get("items") or []:
      artists = ", ".join(a.get("name") or "" for a in item.get("artists") or [])
      if not _match(q, artists, item.get("name")):
         continue
      images = sorted(item.get("images") or [], key=lambda i: i.get("width") or 0, reverse=True)
      for image in images:
         path = ctx.download(image.get("url") or "")
         if path:
            return path
   return None


def _art_youtube(q: AlbumQuery, key: str | None, ctx: _Ctx) -> Path | None:
   # Needs a YouTube Data API v3 key. Album playlists from the auto-generated
   # "<Artist> - Topic" channels are titled "Album - <name>" and use the
   # cover as thumbnail; thumbnails are 16:9, so they're cropped square later.
   url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(
      {"part": "snippet", "q": f"{q.artist} {q.album} album", "maxResults": 10, "key": key})
   data = _http_json(url)
   items = data.get("items") or []
   # Topic-channel playlists first: their thumbnail is the album cover.
   items.sort(key=lambda i: not (
      (i.get("id") or {}).get("kind") == "youtube#playlist"
      and ((i.get("snippet") or {}).get("channelTitle") or "").endswith(" - Topic")))
   for item in items:
      snip = item.get("snippet") or {}
      title = re.sub(r"^album\s*-\s*", "", snip.get("title") or "", flags=re.IGNORECASE)
      channel = re.sub(r"\s*-\s*topic$", "", snip.get("channelTitle") or "", flags=re.IGNORECASE)
      if not (_album_ok(q.album, title) and _mxm_artist_ok(q.artist, channel)):
         continue
      thumbs = snip.get("thumbnails") or {}
      for size in ("maxres", "standard", "high", "medium", "default"):
         turl = (thumbs.get(size) or {}).get("url")
         if turl:
            path = ctx.download(turl)
            if path:
               return _crop_youtube_square(path, ctx)
   return None


_PROVIDER_FNS: dict[str, Callable] = {
   "musicbrainz": _art_musicbrainz,
   "theaudiodb": _art_theaudiodb,
   "apple": _art_apple,
   "discogs": _art_discogs,
   "spotify": _art_spotify,
   "youtube": _art_youtube,
}


# ---------------------------------------------------------------------------
# Image conversion (ffmpeg)
# ---------------------------------------------------------------------------

def _ffmpeg_image(src: Path, dst: Path, vf: str | None = None) -> bool:
   ffmpeg = _ffmpeg_exe()
   if not ffmpeg:
      return False
   cmd = [ffmpeg, "-v", "error", "-y", "-i", str(src), "-frames:v", "1"]
   if vf:
      cmd += ["-vf", vf]
   cmd += ["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj444p", str(dst)]
   proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", check=False, timeout=60)
   return proc.returncode == 0 and dst.is_file()


def _crop_youtube_square(path: Path, ctx: _Ctx) -> Path:
   # 16:9 thumbnails (maxres/medium) hold the cover in the centre; 4:3 ones
   # (standard/high/default) add letterbox bars, so the content is iw*9/16 tall.
   out = path.with_name(path.stem + "_sq.jpg")
   side = "min(ih\\,iw*9/16)"
   if _ffmpeg_image(path, out, vf=f"crop={side}:{side}"):
      return out
   return path


def embeddable_image(image: Path, ctx: _Ctx) -> Path | None:
   """
   ``image`` if it's already JPEG/PNG, else a JPEG copy in the temp folder
   (webp/bmp/gif can't go into ID3/FLAC/MP4 cover slots).
   """
   ext = image.suffix.lower().lstrip(".")
   if ext in ("jpg", "jpeg", "png"):
      return image
   ctx._n += 1
   out = ctx.workdir / f"conv_{ctx._n}.jpg"
   return out if _ffmpeg_image(image, out) else None


# ---------------------------------------------------------------------------
# Resolution (once per folder / per provider+album)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedArt:
   image: Path          # JPEG/PNG ready to embed
   label: str           # e.g. "local cover.jpg", "theaudiodb"


class ArtResolver:
   """
   Resolves one image per album folder and remembers it, so a 12-track album
   costs at most one provider lookup. Provider hits are also cached by
   provider + artist + album, for albums split across folders (CD1/CD2).
   """

   def __init__(self, provider: str, key: str | None, overwrite: bool = False, layout: str | None = None):
      self.provider = provider
      self.key = key
      self.overwrite = overwrite
      self.layout = layout
      self.ctx = _Ctx()
      self._by_folder: dict[Path, ResolvedArt | None] = {}
      self._by_album: dict[tuple[str, str, str], Path | None] = {}

   def resolve(self, audio: Path) -> ResolvedArt | None:
      folder = audio.parent
      if folder not in self._by_folder:
         self._by_folder[folder] = self._resolve_folder(audio)
      return self._by_folder[folder]

   def _resolve_folder(self, audio: Path) -> ResolvedArt | None:
      local = find_local_art(audio.parent)
      if local and not self.overwrite:
         return self._finish(local, f"local {local.name}")

      fetched = self._from_provider(audio)
      if fetched:
         return self._finish(fetched, self.provider)
      if local:
         return self._finish(local, f"local {local.name} ({self.provider} found nothing)")
      return None

   def _finish(self, image: Path, label: str) -> ResolvedArt | None:
      ready = embeddable_image(image, self.ctx)
      if ready is None:
         self.ctx.warn_once(f"conv:{image}", f"could not convert {image.name} to JPEG (ffmpeg missing or failed)")
         return None
      return ResolvedArt(ready, label)

   def _from_provider(self, audio: Path) -> Path | None:
      if self.provider in _KEY_REQUIRED and not self.key:
         self.ctx.warn_once(
            "nokey",
            f"{self.provider}: no key. Pass -g {self.provider}=KEY, set PYLY_{self.provider.upper()}_KEY, "
            f"or save it to ~/.pyly/{self.provider}-key; skipping {self.provider}",
         )
         return None
      q = album_query(audio, self.layout)
      if not (q.release_id or q.group_id or (q.artist and q.album)):
         return None
      cache_key = (self.provider, _norm_loose(q.artist), _norm_loose(q.album) or q.release_id or q.group_id)
      if cache_key in self._by_album:
         return self._by_album[cache_key]
      try:
         hit = _PROVIDER_FNS[self.provider](q, self.key, self.ctx)
      except Exception as exc:
         self.ctx.warn_once(
            f"err:{cache_key}",
            f"{self.provider}: lookup failed for {q.artist} - {q.album} ({_describe_error(exc)})",
         )
         hit = None
      self._by_album[cache_key] = hit
      return hit

   def close(self) -> None:
      self.ctx.close()


# ---------------------------------------------------------------------------
# Embedded art detection / embedding
# ---------------------------------------------------------------------------

def can_embed(audio: Path) -> bool:
   return audio.suffix.lower() in _EMBEDDABLE


def cannot_hold_art(audio: Path) -> bool:
   """ogg/opus: the container could, but ffmpeg can't write it."""
   return audio.suffix.lower() in _NO_ART_CONTAINERS


def has_embedded_art(audio: Path) -> bool:
   """True when ffprobe sees a stream with disposition attached_pic."""
   ffprobe = _ffprobe_exe()
   if not ffprobe:
      return False
   cmd = [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(audio)]
   try:
      proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False, timeout=15)
      data = json.loads(proc.stdout or "{}")
   except Exception:
      return False
   return any(
      ((s.get("disposition") or {}).get("attached_pic") == 1)
      for s in data.get("streams") or [] if isinstance(s, dict)
   )


def embed_cover(audio: Path, image: Path, mode: str = "backup", dry_run: bool = False) -> tuple[bool, str]:
   """
   Embed ``image`` as the front cover with ffmpeg stream copy. Any existing
   attached picture is dropped (only the audio is mapped from the source),
   which is how --overwrite replaces art. ``mode`` is backup or direct.
   """
   if dry_run:
      return True, "dry run"
   ffmpeg = _ffmpeg_exe()
   if not ffmpeg:
      return False, "ffmpeg not found (PATH or bundled ff/)"

   tmp = audio.with_name(f"{audio.stem}.pyly_art_tmp{audio.suffix}")
   cmd = [
      ffmpeg, "-v", "error", "-y",
      "-i", str(audio), "-i", str(image),
      "-map", "0:a", "-map", "1:v",
      "-map_metadata", "0",
      "-c", "copy",
      "-disposition:v:0", "attached_pic",
      "-metadata:s:v:0", "title=Album cover",
      "-metadata:s:v:0", "comment=Cover (front)",
   ]
   if audio.suffix.lower() == ".mp3":
      cmd += ["-id3v2_version", "3"]
   cmd.append(str(tmp))

   try:
      proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False, timeout=120)
      if proc.returncode != 0 or not tmp.exists():
         if tmp.exists():
            tmp.unlink()
         lines = (proc.stderr or "").strip().splitlines()
         return False, f"ffmpeg failed: {lines[-1] if lines else f'exit {proc.returncode}'}"
      replace_with_backup(audio, tmp, mode)
      return True, "embedded"
   except Exception as exc:
      if tmp.exists():
         tmp.unlink()
      return False, f"write error: {exc}"
