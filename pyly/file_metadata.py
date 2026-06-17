"""
File-metadata checking and matching.

Two responsibilities, both standalone modes (no Whisper involved):

* check  (--check-file-metadata / -z)
    Read the audio file's embedded tags and compare them against what the
    file *should* contain, deriving the expected values from (in priority):
        1. an accompanying ``album.nfo`` / ``artist.nfo`` (Kodi/Lidarr style)
        2. the folder layout / ``--layout`` hint
        3. the filename (used when the layout is ``flat`` or yields nothing)
    Mismatches are reported; nothing is written.

NFO files are parsed with a tolerant regex extractor rather than a strict XML
parser: real-world Kodi/Lidarr NFOs are frequently not well-formed XML (e.g.
``<rating: max=10>8</rating>``), the ``<?xml?>`` header is optional, and the
schemas are flat enough that regex extraction is reliable.

* match  (--match-file-metadata / -Z)
    Same comparison, but the expected values are written back into the audio
    file's tags via ffmpeg.  Three write modes:
        backup  write in place, but copy the original to <file>.<ext>.bak first
        direct  write in place, no backup
        copy    leave the original untouched, write <stem>.tagged.<ext>

ffprobe reads tags; ffmpeg writes them.  Both fall back to the bundled
binaries under ``ff/`` when not found on PATH, mirroring the rest of PyLy.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .console_ui import info, warn
from .template_tokens import infer_path_guess, _clean_title


# Fields we check / fix, in display order.
CHECK_FIELDS = ("title", "artist", "album", "year", "track")

_YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")
_LEADING_INT_RX = re.compile(r"\s*(\d+)")


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg discovery (PATH first, bundled ff/ as fallback)
# ---------------------------------------------------------------------------

def _bundled(name: str) -> str | None:
   exe = shutil.which(name)
   if exe:
      return exe
   candidate = Path(__file__).resolve().parents[1] / "ff" / f"{name}.exe"
   return str(candidate) if candidate.exists() else None


def _ffprobe_exe() -> str | None:
   return _bundled("ffprobe")


def _ffmpeg_exe() -> str | None:
   return _bundled("ffmpeg")


# ---------------------------------------------------------------------------
# Embedded tag reading
# ---------------------------------------------------------------------------

def read_embedded_tags(audio_path: Path) -> dict[str, str]:
   """Return the audio file's format+stream tags, keyed lowercase. {} on failure."""
   ffprobe = _ffprobe_exe()
   if not ffprobe:
      return {}
   cmd = [
      ffprobe, "-v", "quiet",
      "-print_format", "json",
      "-show_format", "-show_streams",
      str(audio_path),
   ]
   try:
      proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
      data = json.loads(proc.stdout or "{}")
   except Exception:
      return {}

   tags: dict[str, str] = {}
   if not isinstance(data, dict):
      return tags
   fmt_tags = (data.get("format") or {}).get("tags") or {}
   for key, value in fmt_tags.items():
      if isinstance(value, (str, int, float)):
         tags[str(key).lower()] = str(value).strip()
   for stream in data.get("streams", []) or []:
      if not isinstance(stream, dict):
         continue
      for key, value in (stream.get("tags") or {}).items():
         if isinstance(value, (str, int, float)):
            tags.setdefault(str(key).lower(), str(value).strip())
   return tags


def actual_metadata(tags: dict[str, str]) -> dict[str, str]:
   """Project raw embedded tags onto the fields PyLy checks."""
   return {
      "title": tags.get("title", "") or "",
      "artist": tags.get("artist", "") or tags.get("albumartist", "") or "",
      "album": tags.get("album", "") or "",
      "year": _extract_year(tags.get("date", "") or tags.get("year", "") or tags.get("originaldate", "")),
      "track": _extract_track(tags.get("track", "") or tags.get("tracknumber", "")),
   }


# ---------------------------------------------------------------------------
# album.nfo parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NfoTrack:
   position: int | None
   title: str


@dataclass(frozen=True)
class NfoAlbumRef:
   """An <album> entry as listed inside an artist.nfo (title + year only)."""
   title: str
   year: str


@dataclass
class AlbumNfo:
   path: Path
   album_title: str = ""
   album_artist: str = ""
   year: str = ""
   tracks: list[NfoTrack] = field(default_factory=list)

   def track_for(self, position: int | None, title_hint: str = "") -> NfoTrack | None:
      """Resolve a track entry by position first, then by a fuzzy title hint."""
      if position is not None:
         for t in self.tracks:
            if t.position == position:
               return t
      if title_hint:
         hint = _norm(title_hint)
         for t in self.tracks:
            if t.title and _norm(t.title) == hint:
               return t
      return None


@dataclass
class ArtistNfo:
   path: Path
   name: str = ""
   albums: list[NfoAlbumRef] = field(default_factory=list)

   def year_for(self, album_title: str) -> str:
      """Year of the listed album whose title matches ``album_title``."""
      if not album_title:
         return ""
      target = _norm(album_title)
      for ref in self.albums:
         if ref.title and _norm(ref.title) == target:
            return ref.year
      return ""


def find_album_nfo(audio_path: Path) -> AlbumNfo | None:
   """Look for an ``album.nfo`` in the file's folder, then its parent folder."""
   for folder in _ancestor_dirs(audio_path, depth=2):
      candidate = folder / "album.nfo"
      if candidate.is_file():
         parsed = parse_album_nfo(candidate)
         if parsed:
            return parsed
   return None


def find_artist_nfo(audio_path: Path) -> ArtistNfo | None:
   """
   Look for an ``artist.nfo``, which Lidarr writes in the artist folder
   (above the album folders). Walk a few levels up from the track.
   """
   for folder in _ancestor_dirs(audio_path, depth=3):
      candidate = folder / "artist.nfo"
      if candidate.is_file():
         parsed = parse_artist_nfo(candidate)
         if parsed:
            return parsed
   return None


def _ancestor_dirs(audio_path: Path, depth: int) -> list[Path]:
   """The file's folder and up to ``depth`` ancestors, de-duplicated, in order."""
   dirs: list[Path] = []
   folder = audio_path.parent
   for _ in range(depth + 1):
      if folder and folder not in dirs:
         dirs.append(folder)
      parent = folder.parent
      if parent == folder:
         break
      folder = parent
   return dirs


def parse_album_nfo(nfo_path: Path) -> AlbumNfo | None:
   """
   Parse a Kodi/Lidarr ``album.nfo`` with a tolerant regex extractor.

   Handles invalid XML, an optional ``<?xml?>`` header, XML entities, and
   nested ``<albumArtistCredits><artist>``.  Returns None only when the file
   cannot be read.
   """
   text = _read_text(nfo_path)
   if text is None:
      return None

   # Pull out the <track> blocks first, then strip them so album-level field
   # lookups (title/artist/year) never pick up a track's <title>.
   tracks: list[NfoTrack] = []
   for block in _nfo_blocks("track", text):
      pos_raw = _nfo_first("position", block) or _nfo_first("track", block)
      title = _nfo_first("title", block)
      if title or pos_raw:
         tracks.append(NfoTrack(position=_safe_int(pos_raw), title=title))

   album_level = re.sub(r"<track\b[^>]*>.*?</track>", "", text, flags=re.IGNORECASE | re.DOTALL)

   album_title = _nfo_first("title", album_level)
   # The \b after "artist" means <artistdesc> (a biography) is never matched,
   # while top-level <artist> and the one in <albumArtistCredits> both are.
   album_artist = _nfo_first("albumartist", album_level) or _nfo_first("artist", album_level)
   year = _nfo_year(album_level)

   return AlbumNfo(
      path=nfo_path,
      album_title=album_title,
      album_artist=album_artist,
      year=year,
      tracks=tracks,
   )


def parse_artist_nfo(nfo_path: Path) -> ArtistNfo | None:
   """Parse a Lidarr ``artist.nfo``: the <name> and its listed <album> entries."""
   text = _read_text(nfo_path)
   if text is None:
      return None

   name = _nfo_first("name", text)
   albums: list[NfoAlbumRef] = []
   for block in _nfo_blocks("album", text):
      title = _nfo_first("title", block)
      year = _nfo_year(block)
      if title:
         albums.append(NfoAlbumRef(title=title, year=year))

   return ArtistNfo(path=nfo_path, name=name, albums=albums)


def _read_text(path: Path) -> str | None:
   try:
      return path.read_text(encoding="utf-8", errors="replace")
   except Exception:
      return None


def _nfo_first(tag: str, text: str) -> str:
   """First ``<tag>...</tag>`` inner value, entity-decoded and trimmed."""
   m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, flags=re.IGNORECASE | re.DOTALL)
   return _nfo_clean(m.group(1)) if m else ""


def _nfo_blocks(tag: str, text: str) -> list[str]:
   """Inner contents of every ``<tag>...</tag>`` occurrence."""
   return re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, flags=re.IGNORECASE | re.DOTALL)


def _nfo_clean(value: str) -> str:
   if not value:
      return ""
   return html.unescape(unicodedata.normalize("NFC", value)).strip()


def _nfo_year(scope: str) -> str:
   for tag in ("year", "releasedate", "originalreleasedate", "date"):
      raw = _nfo_first(tag, scope)
      if raw:
         m = _YEAR_RX.search(raw)
         if m:
            return m.group(0)
   return ""


# ---------------------------------------------------------------------------
# Expected metadata + comparison
# ---------------------------------------------------------------------------

def expected_metadata(
   audio_path: Path,
   layout: str | None,
   album_nfo: AlbumNfo | None,
   artist_nfo: ArtistNfo | None = None,
) -> dict[str, str]:
   """
   Derive the values the file *should* carry.

   Priority per field: the .nfo files, then folder/layout, then filename.  The
   folder->filename fallback is already baked into ``infer_path_guess`` (title
   from the filename, artist/album from the folder unless the layout is flat).

   The .nfo files also pin down the folder *structure* even when they omit a
   field: the folder holding album.nfo is the album folder and the one above
   it is the artist; artist.nfo lives in the artist folder.  That is far more
   reliable than the generic path guess, which can't tell an Artist/Album/Track
   tree from an Artist/Track one without a --layout hint.  An explicit --layout
   still wins over this folder inference.
   """
   guess = infer_path_guess(audio_path, layout)
   nfo_track = album_nfo.track_for(guess.track_number, guess.title) if album_nfo else None

   album_dir, artist_dir = _structure_dirs(audio_path, album_nfo, artist_nfo)
   struct_album = album_dir.name if album_dir else ""
   struct_artist = artist_dir.name if artist_dir else ""

   layout_given = bool((layout or "").strip())

   # TITLE: album.nfo track title -> layout/folder guess -> filename
   title = (nfo_track.title if nfo_track else "") or guess.title or _clean_title(audio_path.stem)

   # ALBUM: album.nfo title -> explicit layout -> album folder name
   if album_nfo and album_nfo.album_title:
      album = album_nfo.album_title
   elif layout_given and guess.album:
      album = guess.album
   else:
      album = struct_album or guess.album

   # ARTIST: album.nfo artist -> artist.nfo name -> explicit layout -> artist folder
   if album_nfo and album_nfo.album_artist:
      artist = album_nfo.album_artist
   elif artist_nfo and artist_nfo.name:
      artist = artist_nfo.name
   elif layout_given and guess.artist:
      artist = guess.artist
   else:
      artist = struct_artist or guess.artist

   # YEAR: album.nfo year -> artist.nfo's listing for this album -> guess
   year = album_nfo.year if (album_nfo and album_nfo.year) else ""
   if not year and artist_nfo:
      year = artist_nfo.year_for(album) or artist_nfo.year_for(struct_album)
   if not year:
      year = guess.year

   track_num = guess.track_number
   if track_num is None and nfo_track is not None:
      track_num = nfo_track.position

   return {
      "title": title or "",
      "artist": artist or "",
      "album": album or "",
      "year": year or "",
      "track": str(track_num) if track_num is not None else "",
   }


def _structure_dirs(
   audio_path: Path,
   album_nfo: AlbumNfo | None,
   artist_nfo: ArtistNfo | None,
) -> tuple[Path | None, Path | None]:
   """
   Resolve the album and artist folders from whichever .nfo we have.

   With album.nfo: its folder is the album, the folder above is the artist.
   With only artist.nfo: its folder is the artist, and the album folder is the
   child of it that lies on the path down to the track.
   """
   if album_nfo is not None:
      album_dir = album_nfo.path.parent
      parent = album_dir.parent
      return album_dir, (parent if parent != album_dir else None)

   if artist_nfo is not None:
      artist_dir = artist_nfo.path.parent
      for ancestor in audio_path.parents:
         if ancestor.parent == artist_dir:
            return ancestor, artist_dir
      return None, artist_dir

   return None, None


@dataclass(frozen=True)
class FieldDiff:
   field: str
   expected: str
   actual: str
   status: str  # "ok" | "mismatch" | "missing" | "unknown"


def compare_metadata(expected: dict[str, str], actual: dict[str, str]) -> list[FieldDiff]:
   diffs: list[FieldDiff] = []
   for f in CHECK_FIELDS:
      exp = (expected.get(f) or "").strip()
      act = (actual.get(f) or "").strip()
      if not exp:
         status = "unknown"          # nothing reliable to compare against
      elif not act:
         status = "missing"          # file has no value for a field we know
      elif _field_eq(f, exp, act):
         status = "ok"
      else:
         status = "mismatch"
      diffs.append(FieldDiff(field=f, expected=exp, actual=act, status=status))
   return diffs


def has_problems(diffs: list[FieldDiff]) -> bool:
   return any(d.status in ("mismatch", "missing") for d in diffs)


# ---------------------------------------------------------------------------
# Tag writing (ffmpeg)
# ---------------------------------------------------------------------------

def write_tags(
   audio_path: Path,
   expected: dict[str, str],
   diffs: list[FieldDiff],
   mode: str = "backup",
   dry_run: bool = False,
   log_fn=None,
) -> tuple[bool, str]:
   """
   Write the expected values into the audio file's tags.

   Only fields that are actually wrong (mismatch/missing) are written.
   Returns (changed, message).  ``changed`` is False when there is nothing
   to fix, ffmpeg is unavailable, or the write fails.
   """
   to_fix = [d for d in diffs if d.status in ("mismatch", "missing")]
   if not to_fix:
      return False, "already correct"

   meta_args: list[str] = []
   for d in to_fix:
      value = expected.get(d.field, "")
      if not value:
         continue
      if d.field == "title":
         meta_args += ["-metadata", f"title={value}"]
      elif d.field == "artist":
         meta_args += ["-metadata", f"artist={value}", "-metadata", f"album_artist={value}"]
      elif d.field == "album":
         meta_args += ["-metadata", f"album={value}"]
      elif d.field == "year":
         meta_args += ["-metadata", f"date={value}"]
      elif d.field == "track":
         meta_args += ["-metadata", f"track={value}"]

   if not meta_args:
      return False, "nothing writable"

   summary = ", ".join(f"{d.field}={expected.get(d.field, '')!r}" for d in to_fix)

   if dry_run:
      return True, f"would set {summary} (mode={mode})"

   ffmpeg = _ffmpeg_exe()
   if not ffmpeg:
      return False, "ffmpeg not found (PATH or bundled ff/)"

   if mode == "copy":
      final = audio_path.with_name(f"{audio_path.stem}.tagged{audio_path.suffix}")
      tmp = audio_path.with_name(f"{audio_path.stem}.pyly_tag_tmp{audio_path.suffix}")
   else:
      final = audio_path
      tmp = audio_path.with_name(f"{audio_path.stem}.pyly_tag_tmp{audio_path.suffix}")

   cmd = [
      ffmpeg, "-v", "error", "-y",
      "-i", str(audio_path),
      "-map_metadata", "0",
      *meta_args,
      "-c", "copy",
      str(tmp),
   ]

   try:
      proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
      if proc.returncode != 0 or not tmp.exists():
         _cleanup(tmp)
         err = (proc.stderr or "").strip().splitlines()
         detail = err[-1] if err else f"exit {proc.returncode}"
         return False, f"ffmpeg failed: {detail}"

      if mode == "copy":
         os.replace(tmp, final)
         return True, f"wrote {final.name}: {summary}"

      if mode == "backup":
         bak = audio_path.with_name(audio_path.name + ".bak")
         if not bak.exists():
            shutil.copy2(audio_path, bak)
      os.replace(tmp, final)
      return True, f"set {summary} (mode={mode})"

   except Exception as exc:
      _cleanup(tmp)
      return False, f"write error: {exc}"


def _cleanup(path: Path) -> None:
   try:
      if path.exists():
         path.unlink()
   except Exception:
      pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _norm(value: str) -> str:
   if not value:
      return ""
   s = unicodedata.normalize("NFC", str(value))
   s = (
      s.replace("’", "'").replace("‘", "'")
       .replace("“", '"').replace("”", '"')
       .replace("–", "-").replace("—", "-")
   )
   s = " ".join(s.split())
   return s.casefold()


def _field_eq(field_name: str, expected: str, actual: str) -> bool:
   if field_name == "year":
      return _extract_year(expected) == _extract_year(actual)
   if field_name == "track":
      return _extract_track(expected) == _extract_track(actual)
   return _norm(expected) == _norm(actual)


def _extract_year(value: str) -> str:
   if not value:
      return ""
   m = _YEAR_RX.search(str(value))
   return m.group(0) if m else ""


def _extract_track(value: str) -> str:
   """Normalize a track value to its leading integer (e.g. '02/14' -> '2')."""
   if value is None:
      return ""
   m = _LEADING_INT_RX.match(str(value))
   return str(int(m.group(1))) if m else ""


def _safe_int(value: str | None) -> int | None:
   if not value:
      return None
   m = _LEADING_INT_RX.match(str(value))
   try:
      return int(m.group(1)) if m else int(str(value).strip())
   except Exception:
      return None
