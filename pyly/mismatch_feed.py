"""
Read FAFO's mismatch list and turn its records into plain-English flags.

FAFO (a separate program, github.com/Ephellon/FfAFO) compares each song in
the library with a streaming service and MusicBrainz, and writes one JSON
record per line to a file such as R:\\FAFO\\mismatches.jsonl for every song or
album that looks wrong. PyLy only reads that file: a FAFO record never
changes tags, album.nfo or .lrc files. --check-tags prints the flags beside
its own tag problems so a person can decide what to do.

A track record looks like:
   {"file": "R:\\Local\\Artist\\Album\\01 Song.mp3", "checked": "2026-10-29T05:00:38+00:00",
    "reference": {"service": "apple", "url": "..."},
    "mismatches": [{"code": "DURATION", "library": 308.7, "service": 306.6, "delta": -2.1}]}
An album record has "folder" instead of "file".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# A length difference above this many seconds can push synced lyrics out of
# time, so the flag says so.
LYRICS_DRIFT_SECONDS = 5.0


@dataclass
class MismatchFeed:
   """FAFO records keyed by file path (tracks) and folder path (albums)."""
   tracks: dict[str, dict] = field(default_factory=dict)
   albums: dict[str, dict] = field(default_factory=dict)

   def __len__(self) -> int:
      return len(self.tracks) + len(self.albums)


def _key(path) -> str:
   """Compare paths the way Windows does: case and slash direction don't matter."""
   return os.path.normcase(os.path.normpath(str(path)))


def load_mismatch_feed(path) -> MismatchFeed:
   """
   Read a FAFO mismatches.jsonl file. Lines that aren't records (FAFO's
   warnings, blank lines, a half-written last line) are skipped. When one
   path appears more than once, the last record wins, because FAFO appends
   newer checks after older ones. Raises OSError when the file can't be read.
   """
   feed = MismatchFeed()
   with open(path, encoding="utf-8-sig") as fh:
      for line in fh:
         line = line.strip()
         if not line:
            continue
         try:
            record = json.loads(line)
         except ValueError:
            continue
         if not isinstance(record, dict) or not record.get("mismatches"):
            continue
         if record.get("file"):
            feed.tracks[_key(record["file"])] = record
         elif record.get("folder"):
            feed.albums[_key(record["folder"])] = record
   return feed


def _checked_time(record: dict) -> float | None:
   """The record's "checked" time as a timestamp, or None when missing or unreadable."""
   text = record.get("checked")
   if not text:
      return None
   try:
      when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
   except ValueError:
      return None
   if when.tzinfo is None:
      when = when.replace(tzinfo=timezone.utc)
   return when.timestamp()


def is_stale(record: dict, path: Path) -> bool:
   """
   True when the file changed after FAFO checked it, so the record may no
   longer apply. Uses the size and mtime FAFO saw when the record has them,
   else compares the file's mtime with the "checked" time. A record with
   neither is trusted.
   """
   try:
      st = path.stat()
   except OSError:
      return True
   if "size" in record and "mtime" in record:
      try:
         return int(record["size"]) != st.st_size or abs(float(record["mtime"]) - st.st_mtime) > 1.0
      except (TypeError, ValueError):
         pass
   checked = _checked_time(record)
   return checked is not None and st.st_mtime > checked + 1.0


def _seconds(value) -> str:
   """Format a number of seconds like 3.7 s."""
   try:
      return f"{float(value):.1f} s"
   except (TypeError, ValueError):
      return "?"


def describe(mismatch: dict, has_downloaded_lyrics: bool = False) -> str:
   """One plain-English line for a FAFO mismatch."""
   code = str(mismatch.get("code", "?"))
   lyrics_note = "; downloaded .lrc may not fit this file" if has_downloaded_lyrics else ""

   if code == "AUDIO":
      service = str(mismatch.get("service", "service")).title()
      return f"AUDIO: sounds unlike the {service} preview (BER {mismatch.get('ber')}), maybe the wrong recording{lyrics_note}"
   if code == "DURATION":
      try:
         drift = abs(float(mismatch.get("delta", 0))) > LYRICS_DRIFT_SECONDS
      except (TypeError, ValueError):
         drift = False
      return (f"DURATION: {_seconds(mismatch.get('library'))} here, {_seconds(mismatch.get('service'))} "
              f"on the service ({_seconds(mismatch.get('delta'))} off){lyrics_note if drift else ''}")
   if code == "TITLE_VERSION":
      here = ", ".join(mismatch.get("library") or []) or "none"
      there = ", ".join(mismatch.get("service") or []) or "none"
      return f"TITLE_VERSION: version words here: {here}; on the service: {there}"
   if code == "RELEASE_WITHDRAWN":
      hint = mismatch.get("replacement") or mismatch.get("official") or mismatch.get("id")
      tail = f"; official edition {hint}" if hint else ""
      return f"RELEASE_WITHDRAWN: album.nfo points at a withdrawn release{tail}; run --get-info --replace"
   if code == "TRACKLIST":
      parts = [f"{name} {len(mismatch[name])}" for name in ("missing", "extra", "duplicates", "moved")
               if mismatch.get(name)]
      # FAFO names missing tracks "NN Title", so show the first few; the rest stay counted.
      missing = [str(m) for m in mismatch.get("missing") or []]
      if missing:
         shown = ", ".join(missing[:3]) + (f", +{len(missing) - 3} more" if len(missing) > 3 else "")
         parts[0] = f"missing {len(missing)}: {shown}"
      return f"TRACKLIST: differs from MusicBrainz ({', '.join(parts) or 'details in feed'})"
   if code in ("ISRC", "EXPLICIT"):
      return f"{code}: tag {mismatch.get('library')!r}, service {mismatch.get('service')!r}"
   return f"{code}: {json.dumps({k: v for k, v in mismatch.items() if k != 'code'}, ensure_ascii=False)}"


def track_flags(feed: MismatchFeed, audio: Path) -> list[str]:
   """Flag lines for one audio file. A stale record gives nothing."""
   from pyly.lyrics_fetch import read_pyly_url_from_lrc   # late import: lyrics_fetch is large

   record = feed.tracks.get(_key(audio))
   if not record or is_stale(record, audio):
      return []
   lrc = audio.with_suffix(".lrc")
   downloaded = lrc.exists() and read_pyly_url_from_lrc(lrc) is not None
   return [describe(m, downloaded) for m in record["mismatches"]]


def album_flags(feed: MismatchFeed, folder: Path) -> list[str]:
   """Flag lines for one album folder (shown once, not per track)."""
   record = feed.albums.get(_key(folder))
   return [describe(m) for m in record["mismatches"]] if record else []
