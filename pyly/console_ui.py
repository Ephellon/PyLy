from __future__ import annotations

import itertools
import sys
import time


_ANSI_CLEAR_EOL = "\x1b[0K"

def _is_tty() -> bool:
   try:
      return sys.stdout.isatty()
   except Exception:
      return False


def format_duration(seconds: float) -> str:
   if seconds < 0:
      seconds = 0.0
   total = int(round(seconds))
   hh = total // 3600
   mm = (total % 3600) // 60
   ss = total % 60
   if hh > 0:
      return f"{hh:d}:{mm:02d}:{ss:02d}"
   return f"{mm:d}:{ss:02d}"


class LiveStatus:
   """
   One-line status that updates in-place until you commit a message.
   """

   def __init__(self, enabled: bool = True):
      self.enabled = bool(enabled) and _is_tty()
      self._last_len = 0
      self._active = False

   def update(self, text: str) -> None:
      if not self.enabled:
         # Fallback: print as normal line
         print(text)
         return

      s = (text or "").rstrip("\n")
      self._active = True

      # \r returns to start of line; clear EOL to remove leftovers
      sys.stdout.write("\r" + s + _ANSI_CLEAR_EOL)
      sys.stdout.flush()
      self._last_len = len(s)

   def clear(self) -> None:
      if not self.enabled or not self._active:
         return
      sys.stdout.write("\r" + _ANSI_CLEAR_EOL)
      sys.stdout.flush()
      self._active = False
      self._last_len = 0

   def commit(self, text: str) -> None:
      """
      Clears the live line then prints a normal line.
      """
      if self.enabled and self._active:
         self.clear()
      print(text)

   def newline(self) -> None:
      """
      If you need to end the live line cleanly.
      """
      if not self.enabled:
         return
      if self._active:
         sys.stdout.write("\n")
         sys.stdout.flush()
         self._active = False
         self._last_len = 0


class RollingETA:
   """
   Rolling ETA estimator based on per-file completion times.

   Uses a bounded window average so early slow/fast outliers don't dominate forever.
   Deterministic, no external deps.
   """

   def __init__(self, total: int, window: int = 5) -> None:
      self.total = max(0, int(total))
      self.window = max(1, int(window))
      self._durations: list[float] = []
      self._t0 = time.time()

   def add(self, duration_s: float) -> None:
      if duration_s <= 0:
         return
      self._durations.append(float(duration_s))
      if len(self._durations) > self.window:
         self._durations.pop(0)

   def done_count(self) -> int:
      # Only counts successful additions, not “attempts”
      # (caller can decide whether to add failures too)
      return 0 if self.total == 0 else None  # unused placeholder


   def avg(self) -> float:
      if not self._durations:
         return 0.0
      return sum(self._durations) / float(len(self._durations))

   def remaining_seconds(self, completed: int) -> float:
      remaining = max(0, self.total - int(completed))
      a = self.avg()
      if a <= 0:
         return 0.0
      return remaining * a

   def elapsed_seconds(self) -> float:
      return time.time() - self._t0

   def eta_string(self, completed: int) -> str:
      rem = self.remaining_seconds(completed)
      if rem <= 0:
         return "0:00"
      return format_duration(rem)


class Spinner:
   """
   Minimal spinner that stays on one line and looks decent in Windows terminals.
   Automatically disables itself if stdout isn't a TTY (e.g., piping output).
   """

   def __init__(self, label: str = "", enabled: bool = True) -> None:
      self._label = label.strip()
      self._enabled = bool(enabled) and _is_tty()
      self._frames = itertools.cycle(["|", "/", "-", "\\"])
      self._start = 0.0
      self._last_len = 0
      self._active = False

   def start(self, extra: str = "") -> None:
      if not self._enabled:
         return
      self._start = time.time()
      self._active = True
      self.tick(extra)

   def tick(self, extra: str = "") -> None:
      if not self._enabled or not self._active:
         return

      elapsed = time.time() - self._start
      frame = next(self._frames)

      label = self._label
      if label and extra:
         msg = f"{frame} {label} — {extra}  ({elapsed:0.1f}s)"
      elif label:
         msg = f"{frame} {label}  ({elapsed:0.1f}s)"
      else:
         msg = f"{frame} {extra}  ({elapsed:0.1f}s)"

      pad = max(0, self._last_len - len(msg))
      sys.stdout.write("\r" + msg + (" " * pad))
      sys.stdout.flush()
      self._last_len = len(msg)

   def stop(self, final: str = "done") -> None:
      if not self._enabled or not self._active:
         return

      elapsed = time.time() - self._start
      label = self._label

      if label and final:
         msg = f"✓ {label} — {final}  ({elapsed:0.1f}s)"
      elif label:
         msg = f"✓ {label}  ({elapsed:0.1f}s)"
      else:
         msg = f"✓ {final}  ({elapsed:0.1f}s)"

      pad = max(0, self._last_len - len(msg))
      sys.stdout.write("\r" + msg + (" " * pad) + "\n")
      sys.stdout.flush()

      self._active = False
      self._last_len = 0


def banner(text: str) -> None:
   print(f"\n=== {text} ===")


def step(msg: str) -> None:
   print(f" - {msg}")


def ok(msg: str) -> None:
   print(f" ✓ {msg}")


def warn(msg: str) -> None:
   print(f" ! {msg}")


def err(msg: str) -> None:
   print(f" X {msg}")
