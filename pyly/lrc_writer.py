from __future__ import annotations

from pathlib import Path
from .srt_io import SrtBlock
from .timecode import format_lrc_timestamp


def srt_blocks_to_lrc_lines(blocks: list[SrtBlock]) -> list[str]:
   lines: list[str] = []
   for b in blocks:
      ts = format_lrc_timestamp(b.start_ms)
      # If multiple lines exist in one SRT block, emit each with same timestamp.
      for ln in b.lines:
         lines.append(f"[{ts}] {ln}")
   return lines


def write_lrc(path: Path, lines: list[str], overwrite: bool, headers: list[str] | None = None) -> None:
   if path.exists() and not overwrite:
      raise FileExistsError(str(path))

   out: list[str] = []
   if headers:
      for h in headers:
         h = (h or "").strip()
         if not h:
            continue
         if not (h.startswith("[") and h.endswith("]")):
            continue
         out.append(h)

   for ln in lines:
      out.append((ln or "").rstrip("\n"))

   text = "\n".join(out).rstrip() + "\n"
   path.write_text(text, encoding="utf-8", newline="\n")
