"""
Identify songs by how they sound, using Chromaprint and AcoustID.

Chromaprint's fpcalc tool listens to the first two minutes of a file and
turns it into a short "fingerprint" string. AcoustID (acoustid.org) is a
free web service that matches a fingerprint against millions of songs and
answers with the MusicBrainz recordings it sounds like. That lets PyLy spot a
file holding the wrong song even when its tags and filename look fine.

fpcalc is looked for on PATH, then in the bundled ff/ folder (like ffmpeg).
The AcoustID key comes from get_key("acoustid"): the [keys] section of the
settings file, $PYLY_ACOUSTID_KEY, or ~/.pyly/acoustid-key. It is sent in the
request body, never in a URL, so it can't leak into an error message or log.

Answers are cached in ~/.pyly/acoustid-cache.json per file (path, size and
modification time), so nightly runs only ask about new or changed files.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from pyly.file_metadata import _bundled, _titles_agree
from pyly.lyrics_fetch import _MB_USER_AGENT

_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
# AcoustID allows three requests a second; stay just under it.
_RATE_LIMIT_S = 0.34
# Below this score a result is a guess, not an identification.
MIN_SCORE = 0.5
# --fix-tags only takes a title from AcoustID when it is this sure.
FIX_SCORE = 0.9

_last_call = 0.0
_lock = threading.Lock()


class AcoustIDError(Exception):
   """fpcalc or the AcoustID service could not answer (missing tool, bad key, network)."""


@dataclass(frozen=True)
class SoundMatch:
   """One recording a file sounds like, with AcoustID's confidence (0-1)."""
   score: float
   recording_id: str
   title: str
   artist: str


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def fpcalc_path() -> str | None:
   """Where fpcalc is: PATH first, then ff/fpcalc.exe. None when it's not installed."""
   return _bundled("fpcalc")


def fingerprint(audio: Path) -> tuple[int, str]:
   """
   Run fpcalc on one file and return (length in whole seconds, fingerprint).
   Raises AcoustIDError when fpcalc is missing or can't read the file.
   """
   exe = fpcalc_path()
   if not exe:
      raise AcoustIDError("fpcalc not found (put fpcalc.exe in ff/ or on PATH)")
   try:
      proc = subprocess.run([exe, "-json", str(audio)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, timeout=60)
      data = json.loads(proc.stdout or "{}")
   except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
      raise AcoustIDError(f"fpcalc failed: {e}") from e
   if not data.get("fingerprint"):
      raise AcoustIDError(f"fpcalc could not read the audio: {(proc.stderr or '').strip()[:200]}")
   return int(round(float(data.get("duration") or 0))), data["fingerprint"]


# ---------------------------------------------------------------------------
# AcoustID lookup
# ---------------------------------------------------------------------------

def lookup(duration: int, fp: str, key: str) -> list[SoundMatch]:
   """
   Ask AcoustID which recordings sound like this fingerprint. Best first;
   one entry per recording. Raises AcoustIDError on a bad key or network trouble.
   """
   global _last_call
   body = urllib.parse.urlencode({
      "client": key, "meta": "recordings", "duration": duration, "fingerprint": fp,
   }).encode("ascii")
   req = urllib.request.Request(_LOOKUP_URL, data=body,
                                headers={"User-Agent": _MB_USER_AGENT,
                                         "Content-Type": "application/x-www-form-urlencoded"})
   with _lock:
      wait = _RATE_LIMIT_S - (time.monotonic() - _last_call)
      if wait > 0:
         time.sleep(wait)
      try:
         with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
      except urllib.error.HTTPError as e:
         # AcoustID explains errors in a JSON body even on HTTP 400.
         try:
            data = json.loads(e.read().decode("utf-8"))
         except Exception:
            raise AcoustIDError(f"AcoustID HTTP {e.code}") from e
      except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
         raise AcoustIDError(f"AcoustID lookup failed: {e}") from e
      finally:
         _last_call = time.monotonic()
   if data.get("status") != "ok":
      message = (data.get("error") or {}).get("message", "unknown error")
      raise AcoustIDError(f"AcoustID refused the lookup: {message}")
   return _parse_results(data.get("results") or [])


def _parse_results(results: list) -> list[SoundMatch]:
   """Flatten AcoustID results into one SoundMatch per recording, best score first."""
   best: dict[str, SoundMatch] = {}
   for result in results:
      score = float(result.get("score") or 0)
      for rec in result.get("recordings") or []:
         rid = rec.get("id")
         if not rid or (rid in best and best[rid].score >= score):
            continue
         artist = " & ".join(a.get("name", "") for a in rec.get("artists") or [] if a.get("name"))
         best[rid] = SoundMatch(score, rid, rec.get("title", ""), artist)
   return sorted(best.values(), key=lambda m: -m.score)


# ---------------------------------------------------------------------------
# Cached identification
# ---------------------------------------------------------------------------

class Identifier:
   """
   Identify files by sound, reusing cached answers. One per run: the cache is
   read once and saved by save(). An unusable setup (no fpcalc, no key) is
   reported once through `problem`, and identify() then answers from the
   cache only.
   """

   def __init__(self, key: str | None):
      self.key = key
      self.cache_path = Path.home() / ".pyly" / "acoustid-cache.json"
      self.dirty = False
      self.problem = ""
      if not key:
         self.problem = "no AcoustID key (add 'acoustid = ...' under [keys] in ~/.pyly/pyly.ini)"
      elif not fpcalc_path():
         self.problem = "fpcalc not found (put fpcalc.exe in ff/ or on PATH)"
      try:
         self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
      except (OSError, json.JSONDecodeError):
         self.cache = {}

   def identify(self, audio: Path) -> list[SoundMatch] | None:
      """
      What the file sounds like (matches at or above MIN_SCORE; [] = not in
      AcoustID). None when the setup is unusable and nothing is cached.
      Raises AcoustIDError when this one file can't be checked.
      """
      stat = audio.stat()
      where = str(audio.resolve())
      entry = self.cache.get(where)
      if entry and entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime:
         return [SoundMatch(**m) for m in entry.get("matches", [])]
      if self.problem:
         return None
      matches = [m for m in lookup(*fingerprint(audio), self.key) if m.score >= MIN_SCORE]
      self.cache[where] = {"size": stat.st_size, "mtime": stat.st_mtime,
                           "matches": [asdict(m) for m in matches]}
      self.dirty = True
      return matches

   def save(self) -> None:
      """Write the cache back when anything new was learned. Never stores the key."""
      if not self.dirty:
         return
      self.cache_path.parent.mkdir(parents=True, exist_ok=True)
      tmp = self.cache_path.with_suffix(".tmp")
      tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
      tmp.replace(self.cache_path)
      self.dirty = False


def sound_flags(matches: list[SoundMatch], expected_title: str, recording_id: str = "") -> list[str]:
   """
   Plain-English warnings when a file doesn't sound like the song it should be.
   With album.nfo's recording id the check is exact; otherwise titles are compared.
   """
   if not matches:
      return []
   if recording_id and any(m.recording_id == recording_id for m in matches):
      return []
   if not recording_id and any(_titles_agree(m.title, expected_title) for m in matches):
      return []
   top = matches[0]
   by = f" by {top.artist}" if top.artist else ""
   return [f"SOUND: sounds like {top.title!r}{by} ({top.score:.0%} sure), not {expected_title!r}"]


def fix_title(matches: list[SoundMatch] | None) -> str:
   """The title --fix-tags may use for a file album.nfo doesn't list: the top match when sure, else ''."""
   if matches and matches[0].score >= FIX_SCORE:
      return matches[0].title
   return ""
