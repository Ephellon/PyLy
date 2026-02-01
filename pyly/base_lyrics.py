from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


_FILLER_WORDS = {
   "uh", "uhh", "uhhh",
   "oh", "ooh", "woo", "woah", "whoa",
   "yeah", "yea", "yep", "nope",
   "okay", "ok", "damn",
   "og",
   "ayy", "ay", "hey",
}

_PUNCT_RX = re.compile(r"[^a-z0-9\s]+", re.I)
_SPACE_RX = re.compile(r"\s+")


@dataclass
class BaseMatchStats:
   replaced: int = 0
   kept: int = 0
   dropped: int = 0

   merged_spans: int = 0
   max_span: int = 1
   garbage_removed: int = 0

   base_lines_used: int = 0
   whisper_lines_consumed_for_base: int = 0

   global_diff: float = 0.0
   rescue_replaced: int = 0

   def avg_whisper_per_base(self) -> float:
      if self.base_lines_used <= 0:
         return 0.0
      return self.whisper_lines_consumed_for_base / float(self.base_lines_used)


def _norm(text: str) -> str:
   s = text.strip().lower()
   if not s:
      return ""

   s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
   s = s.replace("—", "-").replace("–", "-")

   # Light contraction normalization
   s = s.replace(" 'cause", " cause")
   s = s.replace("'cause", "cause")

   # Targeted phrase folding (small, explicit, deterministic)
   replacements = {
      "did not seem": "deny see",
      "didnt seem": "deny see",
      "denied seeing": "deny see",
      "denied see": "deny see",
      "seeing me": "see me",

      "its cuz": "cause",
      "it's cuz": "cause",
      "is cuz": "cause",

      "im": "i am",

      # Common phonetic failures for this track class
      "episode of grips": "episode of cribs",
      "grips": "cribs",
      "global load": "blow below",
      "dont an": "on an",
   }

   for k, v in replacements.items():
      s = s.replace(k, v)

   s = _PUNCT_RX.sub(" ", s)
   s = _SPACE_RX.sub(" ", s).strip()
   return s


def _is_garbage_line(text: str) -> bool:
   n = _norm(text)
   if not n:
      return True

   # Single very short token => likely ad-lib/noise
   if len(n) <= 2:
      return True

   if n in _FILLER_WORDS:
      return True

   parts = n.split()
   if len(parts) == 1 and parts[0] in _FILLER_WORDS:
      return True

   if len(parts) == 2 and parts[0] == parts[1] and parts[0] in _FILLER_WORDS:
      return True

   return False


def _sim(a: str, b: str) -> float:
   if not a or not b:
      return 0.0
   return SequenceMatcher(None, a, b).ratio()


def _global_diff_score(whisper_lines: list[str], base_lines: list[str]) -> float:
   w = " ".join(_norm(x) for x in whisper_lines if _norm(x))
   b = " ".join(_norm(x) for x in base_lines if _norm(x))
   if not w or not b:
      return 0.0
   return SequenceMatcher(None, w, b).ratio()


def _max_sim_to_base_window(w_line_norm: str, b_norm: list[str], b_start: int, window: int) -> float:
   if not w_line_norm:
      return 0.0
   best = 0.0
   stop = min(len(b_norm), b_start + max(1, window))
   for j in range(b_start, stop):
      if not b_norm[j]:
         continue
      s = _sim(w_line_norm, b_norm[j])
      if s > best:
         best = s
   return best


def load_base_lyrics_lines(txt_path: Path) -> list[str]:
   raw = txt_path.read_text(encoding="utf-8", errors="replace").splitlines()
   lines: list[str] = []
   for ln in raw:
      s = ln.strip()
      if s:
         lines.append(s)
   return lines


def apply_base_lyrics(
   whisper_lines: list[str],
   base_lines: list[str],
   threshold: float = 0.82,
   window: int = 12,
   strict: bool = False,
   max_merge: int = 5,

   # diff/rescue
   diff_threshold: float = 0.75,
   enable_rescue: bool = True,
   rescue_drop_below: float = 0.35,
   rescue_threshold_delta: float = 0.10,
) -> tuple[list[str], BaseMatchStats]:
   """
   Pass 1: span/merge matching (many-to-one) replaces Whisper spans with base lines.
   Pass 2: diff-driven rescue optionally cleans remaining junk and fills missed base lines.
   """
   stats = BaseMatchStats()
   patched = list(whisper_lines)

   if not whisper_lines or not base_lines:
      return patched, stats

   w_norm = [_norm(x) for x in whisper_lines]
   b_norm = [_norm(x) for x in base_lines]

   stats.global_diff = _global_diff_score(whisper_lines, base_lines)

   i = 0
   b = 0
   max_merge = max(1, int(max_merge))
   window = max(1, int(window))

   used_base = [False] * len(base_lines)

   # -------- PASS 1 --------
   while i < len(whisper_lines) and b < len(base_lines):
      if _is_garbage_line(whisper_lines[i]):
         patched[i] = ""
         stats.dropped += 1
         stats.garbage_removed += 1
         i += 1
         continue

      best_score = 0.0
      best_b = b
      best_k = 1

      b_stop = min(len(base_lines), b + window)

      for bj in range(b, b_stop):
         target = b_norm[bj]
         if not target:
            continue

         for k in range(1, max_merge + 1):
            if i + k > len(whisper_lines):
               break

            parts: list[str] = []
            for t in range(i, i + k):
               if _is_garbage_line(whisper_lines[t]):
                  continue
               if w_norm[t]:
                  parts.append(w_norm[t])

            merged = " ".join(parts).strip()
            if not merged:
               continue

            score = _sim(merged, target)

            # Coverage bonus prevents short partials winning too often
            if k > 1:
               score += min(0.05, 0.01 * (k - 1))

            if score > best_score:
               best_score = score
               best_b = bj
               best_k = k

      effective = float(threshold)
      if best_k > 1:
         effective = max(0.0, effective - 0.05)

      if best_score >= effective:
         patched[i] = base_lines[best_b]
         for t in range(i + 1, min(len(patched), i + best_k)):
            patched[t] = ""

         used_base[best_b] = True

         stats.replaced += 1
         stats.base_lines_used += 1
         stats.whisper_lines_consumed_for_base += best_k

         if best_k > 1:
            stats.merged_spans += 1
            stats.max_span = max(stats.max_span, best_k)

         i += best_k
         b = best_b + 1
         continue

      if strict:
         patched[i] = ""
         stats.dropped += 1
      else:
         stats.kept += 1

      i += 1

   while i < len(whisper_lines):
      if _is_garbage_line(whisper_lines[i]):
         patched[i] = ""
         stats.dropped += 1
         stats.garbage_removed += 1
      elif strict:
         patched[i] = ""
         stats.dropped += 1
      else:
         stats.kept += 1
      i += 1

   # -------- PASS 2: RESCUE --------
   if not enable_rescue:
      return patched, stats

   if stats.global_diff < float(diff_threshold):
      return patched, stats

   remaining_base = [idx for idx, used in enumerate(used_base) if (not used) and b_norm[idx]]
   if not remaining_base:
      return patched, stats

   rescue_threshold = max(0.40, float(threshold) - float(rescue_threshold_delta))

   rb = 0
   j = 0

   while j < len(patched) and rb < len(remaining_base):
      line = patched[j]

      # If this line is clearly garbage (or blank), we can try to inject the next base line
      if (not line) or _is_garbage_line(line):
         bj = remaining_base[rb]

         # Build a small local candidate (up to 3 lines) for scoring
         cand_parts: list[str] = []
         for t in range(j, min(len(patched), j + 3)):
            if patched[t] and not _is_garbage_line(patched[t]):
               cand_parts.append(_norm(patched[t]))
         cand = " ".join(cand_parts).strip()

         if not cand:
            patched[j] = base_lines[bj]
            used_base[bj] = True
            stats.rescue_replaced += 1
            rb += 1
            j += 1
            continue

         score = _sim(cand, b_norm[bj])
         if score >= rescue_threshold:
            patched[j] = base_lines[bj]
            used_base[bj] = True
            stats.rescue_replaced += 1
            rb += 1
            j += 1
            continue

         # Drop lines that don't resemble any nearby base at all
         local_best = _max_sim_to_base_window(_norm(line), b_norm, bj, window=6)
         if local_best < float(rescue_drop_below):
            patched[j] = ""
            stats.dropped += 1
            stats.garbage_removed += 1

      j += 1

   # Final sweep: if base exists, aggressively remove obvious junk that survived
   for k in range(len(patched)):
      if patched[k] and _is_garbage_line(patched[k]):
         patched[k] = ""
         stats.dropped += 1
         stats.garbage_removed += 1

   return patched, stats
