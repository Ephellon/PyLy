from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .timecode import parse_srt_timestamp, format_srt_timestamp


@dataclass
class SrtBlock:
   start_ms: int
   end_ms: int
   lines: list[str]


_TIME_RX = re.compile(r"^\s*(\d\d:\d\d:\d\d,\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d,\d\d\d)\s*$")


def read_srt(path: Path) -> list[SrtBlock]:
   text = path.read_text(encoding="utf-8", errors="replace")
   # Normalize newlines
   text = text.replace("\r\n", "\n").replace("\r", "\n")
   chunks = [c.strip("\n") for c in text.split("\n\n") if c.strip()]

   blocks: list[SrtBlock] = []
   for chunk in chunks:
      lines = chunk.split("\n")
      if len(lines) < 2:
         continue

      # Optional numeric index line at [0]
      i = 0
      if lines[0].strip().isdigit():
         i = 1
      if i >= len(lines):
         continue

      m = _TIME_RX.match(lines[i])
      if not m:
         continue

      start_ms = parse_srt_timestamp(m.group(1))
      end_ms = parse_srt_timestamp(m.group(2))
      payload = [ln.strip() for ln in lines[i + 1:] if ln.strip() != ""]
      blocks.append(SrtBlock(start_ms=start_ms, end_ms=end_ms, lines=payload))

   return blocks


def write_srt(path: Path, blocks: list[SrtBlock]) -> None:
   out_lines: list[str] = []
   for idx, b in enumerate(blocks, start=1):
      out_lines.append(str(idx))
      out_lines.append(f"{format_srt_timestamp(b.start_ms)} --> {format_srt_timestamp(b.end_ms)}")
      out_lines.extend(b.lines if b.lines else [""])
      out_lines.append("")
   path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8", newline="\n")
