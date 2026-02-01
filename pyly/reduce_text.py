from __future__ import annotations

import re
from pathlib import Path


# Common subtitle noise tokens / stage directions
_NOISE_PATTERNS = [
   r"^\[ *(music|applause|cheering|laughter|laughs|crowd|intro|outro|silence) *\]$",
   r"^\( *(music|instrumental|applause|cheering|laughter|laughs|crowd|intro|outro|silence) *\)$",
   r"^\[ *(inaudible|unintelligible) *\]$",
   r"^\( *(inaudible|unintelligible) *\)$",
   r"^(inaudible|unintelligible)\.?$",
]

_NOISE_RX = re.compile("|".join(_NOISE_PATTERNS), re.I)

# Musical notes / glyphs often found in subs
_MUSIC_NOTE_RX = re.compile(r"[♪♫♩♬]+", re.I)

# "Speaker:" labels (very conservative)
_SPEAKER_LABEL_RX = re.compile(r"^[A-Z][A-Z0-9 _\-]{1,24}:\s+")

# Generic "caption-ish" bracketed line (only if it contains a known keyword)
_BRACKETED_RX = re.compile(r"^\s*[\[\(].*[\]\)]\s*$")


def _log_append(log_path: Path | None, msg: str) -> None:
   if not log_path:
      return
   try:
      with open(log_path, "a", encoding="utf-8", newline="\n") as f:
         f.write(msg.rstrip("\n") + "\n")
   except Exception:
      pass


def _clean_line(line: str) -> str:
   s = (line or "").strip()
   if not s:
      return ""

   # Remove musical note glyphs anywhere
   s = _MUSIC_NOTE_RX.sub("", s).strip()

   # Normalize some unicode punctuation lightly (helps base matching later)
   s = s.replace("’", "'").replace("‘", "'")
   s = s.replace("“", '"').replace("”", '"')
   s = s.replace("—", "-").replace("–", "-")

   # Drop pure noise captions
   if _NOISE_RX.match(s):
      return ""

   # If it's fully bracketed, drop it only if it looks like a stage direction keyword
   if _BRACKETED_RX.match(s):
      inner = re.sub(r"^[\[\(]\s*|\s*[\]\)]$", "", s).strip().lower()
      if any(k in inner for k in ["music", "applause", "cheer", "laughter", "inaudible", "unintelligible", "instrumental"]):
         return ""

   # Strip conservative speaker labels (e.g. "EMINEM: ...") but keep text
   # Only if label is short-ish and ALL CAPS-ish.
   s = _SPEAKER_LABEL_RX.sub("", s).strip()

   # Collapse whitespace
   s = re.sub(r"\s+", " ", s).strip()

   return s


def reduce_srt_blocks(blocks: list["SrtBlock"]) -> list["SrtBlock"]:
   from .srt_io import SrtBlock

   out: list[SrtBlock] = []
   for b in blocks:
      new_lines: list[str] = []
      for ln in b.lines:
         cleaned = _clean_line(ln)
         if cleaned:
            new_lines.append(cleaned)

      if new_lines:
         out.append(SrtBlock(start_ms=b.start_ms, end_ms=b.end_ms, lines=new_lines))
   return out


def reduce_srt_file(
   srt_path: Path,
   out_path: Path,
   log_path: Path | None = None,
) -> Path:
   """
   Read SRT, remove non-lyrical/noise lines at the text level, and write a reduced SRT.

   Requires srt_io.py to provide:
      - read_srt(path) -> list[SrtBlock]
      - write_srt(path, blocks) -> None
   Where SrtBlock has:
      - start_ms: int
      - end_ms: int
      - lines: list[str]
   """
   from .srt_io import read_srt, write_srt

   srt_path = Path(srt_path)
   out_path = Path(out_path)

   if not srt_path.exists():
      raise FileNotFoundError(str(srt_path))

   blocks = read_srt(srt_path)

   kept_blocks = 0
   dropped_blocks = 0
   kept_lines = 0
   dropped_lines = 0

   new_blocks = []

   for b in blocks:
      new_lines: list[str] = []
      for ln in b.lines:
         cleaned = _clean_line(ln)
         if cleaned:
            new_lines.append(cleaned)
            kept_lines += 1
         else:
            dropped_lines += 1

      # Drop entire block if no lines survived
      if not new_lines:
         dropped_blocks += 1
         continue

      # Rebuild block preserving timestamps
      # Assume SrtBlock is a dataclass or similar; create a new one conservatively:
      try:
         nb = type(b)(start_ms=b.start_ms, end_ms=b.end_ms, lines=new_lines)
      except Exception:
         # Fallback: mutate a shallow copy if constructor signature differs
         b.lines = new_lines
         nb = b

      new_blocks.append(nb)
      kept_blocks += 1

   out_path.parent.mkdir(parents=True, exist_ok=True)
   write_srt(out_path, new_blocks)

   _log_append(log_path, f"REDUCE: input={srt_path.name} output={out_path.name}")
   _log_append(log_path, f"REDUCE: blocks_kept={kept_blocks} blocks_dropped={dropped_blocks}")
   _log_append(log_path, f"REDUCE: lines_kept={kept_lines} lines_dropped={dropped_lines}")
   _log_append(log_path, "REDUCE: rules=noise_brackets, music_notes, inaudible/unintelligible, optional_speaker_labels")

   return out_path
