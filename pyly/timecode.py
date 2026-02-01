from __future__ import annotations

import re


_SRT_TS = re.compile(r"^(\d\d):(\d\d):(\d\d),(\d\d\d)$")


def parse_srt_timestamp(ts: str) -> int:
   m = _SRT_TS.match(ts.strip())
   if not m:
      raise ValueError(f"Bad SRT timestamp: {ts!r}")
   hh = int(m.group(1))
   mm = int(m.group(2))
   ss = int(m.group(3))
   ms = int(m.group(4))
   total = (((hh * 60) + mm) * 60 + ss) * 1000 + ms
   return total


def format_srt_timestamp(ms: int) -> str:
   if ms < 0:
      ms = 0
   s = ms // 1000
   rem = ms % 1000
   hh = s // 3600
   s %= 3600
   mm = s // 60
   ss = s % 60
   return f"{hh:02d}:{mm:02d}:{ss:02d},{rem:03d}"


def format_lrc_timestamp(ms: int) -> str:
   if ms < 0:
      ms = 0

   # LRC wants mm:ss.xx (centiseconds)
   total_cs = ms // 10
   mm = total_cs // (60 * 100)
   ss = (total_cs // 100) % 60
   xx = total_cs % 100
   return f"{mm:02d}:{ss:02d}.{xx:02d}"
