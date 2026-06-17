"""
File-metadata checking and matching.

Two responsibilities, both standalone modes (no Whisper involved):

* check  (--check-file-metadata / -z)
    Read the audio file's embedded tags and compare them against what the
    file *should* contain, deriving the expected values from (in priority):
        1. an accompanying ``album.nfo`` (Kodi/Plex style)
        2. the folder layout / ``--layout`` hint
        3. the filename (used when the layout is ``flat`` or yields nothing)
    Mismatches are reported; nothing is written.

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

import json
import os
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
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


def find_album_nfo(audio_path: Path) -> AlbumNfo | None:
   """Look for an ``album.nfo`` in the file's folder, then its parent folder."""
   seen: set[Path] = set()
   for folder in (audio_path.parent, audio_path.parent.parent):
      if not folder or folder in seen:
         continue
      seen.add(folder)
      candidate = folder / "album.nfo"
      if candidate.is_file():
         parsed = parse_album_nfo(candidate)
         if parsed:
            return parsed
   return None


def parse_album_nfo(nfo_path: Path) -> AlbumNfo | None:
   """Parse a Kodi/Plex-style ``album.nfo``. Tolerant of missing fields."""
   try:
      text = nfo_path.read_text(encoding="utf-8", errors="replace")
      root = ET.fromstring(text)
   except Exception:
      return None

   # The <album> element may be the root or nested somewhere inside.
   album_el = root if root.tag.lower() == "album" else root.find(".//album")
   if album_el is None:
      album_el = root

   album_title = _nfo_text(album_el, "title")
   # NB: <artistdesc> is a free-text biography, not an artist name — never use it.
   album_artist = (
      _nfo_artist(album_el, "albumartist")
      or _nfo_artist(album_el, "artist")
   )
   year = _nfo_year(album_el)

   tracks: list[NfoTrack] = []
   for track_el in album_el.findall("track"):
      pos_raw = _nfo_text(track_el, "position") or _nfo_text(track_el, "track")
      title = _nfo_text(track_el, "title")
      tracks.append(NfoTrack(position=_safe_int(pos_raw), title=title))

   return AlbumNfo(
      path=nfo_path,
      album_title=album_title,
      album_artist=album_artist,
      year=year,
      tracks=tracks,
   )


def _nfo_text(parent: ET.Element, tag: str) -> str:
   el = parent.find(tag)
   if el is None or el.text is None:
      return ""
   return unicodedata.normalize("NFC", el.text).strip()


def _nfo_artist(parent: ET.Element, tag: str) -> str:
   """Read an artist field that may be plain text or a nested <name> element."""
   el = parent.find(tag)
   if el is None:
      return ""
   if el.text and el.text.strip():
      return unicodedata.normalize("NFC", el.text).strip()
   name = el.find("name")
   if name is not None and name.text:
      return unicodedata.normalize("NFC", name.text).strip()
   return ""


def _nfo_year(parent: ET.Element) -> str:
   for tag in ("year", "releasedate", "originalreleasedate", "date"):
      raw = _nfo_text(parent, tag)
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
   nfo: AlbumNfo | None,
) -> dict[str, str]:
   """
   Derive the values the file *should* carry.

   Priority per field: album.nfo, then folder/layout, then filename.  The
   folder->filename fallback is already baked into ``infer_path_guess``
   (the title comes from the filename, artist/album from the folder unless
   the layout is ``flat``).

   When an album.nfo is present it pins down the structure even if it omits
   some fields: the folder holding album.nfo *is* the album folder, so its
   name is the album and the folder above it is the artist.  That is far more
   reliable than the generic path guess, which can't tell an Artist/Album/Track
   tree from an Artist/Track one without a --layout hint.  An explicit --layout
   still wins over this folder inference.
   """
   guess = infer_path_guess(audio_path, layout)
   nfo_track = nfo.track_for(guess.track_number, guess.title) if nfo else None

   # Folder-structure values implied by the album.nfo location.
   nfo_album = ""
   nfo_dir_artist = ""
   if nfo is not None:
      album_dir = nfo.path.parent
      nfo_album = album_dir.name
      if album_dir.parent and album_dir.parent != album_dir:
         nfo_dir_artist = album_dir.parent.name

   layout_given = bool((layout or "").strip())

   title = (nfo_track.title if nfo_track else "") or guess.title or _clean_title(audio_path.stem)

   # album.nfo's own field -> explicit layout -> the album.nfo folder name
   if nfo is not None and nfo.album_title:
      album = nfo.album_title
   elif layout_given and guess.album:
      album = guess.album
   else:
      album = nfo_album or guess.album

   # album.nfo's own field -> explicit layout -> the folder above album.nfo
   if nfo is not None and nfo.album_artist:
      artist = nfo.album_artist
   elif layout_given and guess.artist:
      artist = guess.artist
   else:
      artist = nfo_dir_artist or guess.artist

   year = (nfo.year if nfo else "") or guess.year

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
