from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .srt_io import SrtBlock


@dataclass(frozen=True)
class PipelineOutputs:
   srt: Path
   red_srt: Path
   lrc: Path
   log: Path | None


def _outputs_for(audio: Path) -> PipelineOutputs:
   base = audio.with_suffix("")
   return PipelineOutputs(
      srt=base.with_suffix(".srt"),
      red_srt=Path(str(base) + ".red.srt"),
      lrc=base.with_suffix(".lrc"),
      log=Path(str(base) + ".pyly.log"),
   )


def _extract_whisper_lines(blocks: list[SrtBlock]) -> list[str]:
   lines: list[str] = []
   for b in blocks:
      for ln in b.lines:
         s = ln.strip()
         if s:
            lines.append(s)
   return lines


def _apply_lines_back_to_blocks(blocks: list[SrtBlock], new_lines: list[str]) -> list[SrtBlock]:
   i = 0
   out: list[SrtBlock] = []
   for b in blocks:
      replaced_lines: list[str] = []
      for _ in b.lines:
         if i < len(new_lines):
            replaced_lines.append(new_lines[i])
            i += 1

      replaced_lines = [x for x in replaced_lines if x.strip()]
      if replaced_lines:
         out.append(SrtBlock(start_ms=b.start_ms, end_ms=b.end_ms, lines=replaced_lines))

   return out


def _resolve_base_lyrics(base_arg: Path | None, audio_path: Path) -> Path | None:
   """
   Resolve base lyrics path per audio file.

   Rules:
   - Absolute paths are used as-is
   - Relative paths resolve against the audio file's directory
   - If the filename contains '*', it is replaced with audio stem
     (e.g. '*.txt' -> '<audio_stem>.txt')
   """
   if not base_arg:
      return None

   base_arg = Path(base_arg)
   name = base_arg.name
   if "*" in name:
      name = name.replace("*", audio_path.stem)

   if base_arg.is_absolute():
      return (base_arg.parent / name).resolve()

   return (audio_path.parent / base_arg.parent / name).resolve()


def run_pipeline(
   audio_path: Path,
   overwrite: bool = False,
   clean: bool = False,
   dry_run: bool = False,
   write_log: bool = False,
   whisper_model: str = "small",
   language: str | None = None,
   device: str | None = None,

   # base lyrics
   base_lyrics_path: Path | None = None,
   base_strict: bool = False,
   base_threshold: float = 0.82,
   base_window: int = 12,
   base_max_merge: int = 5,

   # diff/rescue (optional; only used if base_lyrics supports it)
   base_diff_threshold: float = 0.75,
   base_rescue: bool = True,

   # output niceties
   lrc_header: bool = True,
   fetch_config=None,
   layout: str | None = None,
) -> dict[str, str]:
   """
   Process a single audio file:
   audio -> Whisper .srt -> reduced .red.srt -> .lrc

   Returns:
      {"status": "ok"|"skipped"|"dry_run", "lrc": "<path>", "reason": "..."}
   """

   import inspect
   import traceback

   # Local imports to match your repo layout
   from .whisper_offline import transcribe_to_srt
   from .reduce_text import reduce_srt_file
   from .srt_io import read_srt
   from .base_lyrics import load_base_lyrics_lines, apply_base_lyrics
   from .lrc_writer import srt_blocks_to_lrc_lines, write_lrc
   from .lyrics_fetch import fetch_base_lyrics_lines

   audio_path = Path(audio_path)

   if not audio_path.exists():
      raise FileNotFoundError(str(audio_path))

   out_srt = audio_path.with_suffix(".srt")
   out_red = audio_path.with_suffix(".red.srt")
   out_lrc = audio_path.with_suffix(".lrc")
   log_path = audio_path.with_suffix(".pyly.log") if write_log else None

   def _log(msg: str) -> None:
      if not log_path:
         return
      try:
         with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(msg.rstrip() + "\n")
      except Exception:
         pass

   def _read_lrc_text_lines(lrc_lines: list[str]) -> list[str]:
      # For matching: strip timestamps, keep text only
      out: list[str] = []
      for ln in lrc_lines:
         s = (ln or "").strip()
         if not s:
            continue
         if s.startswith("[") and "]" in s:
            out.append(s.split("]", 1)[1].lstrip())
         else:
            out.append(s)
      return out

   def _apply_text_back_into_lrc(lrc_lines: list[str], new_text_lines: list[str]) -> list[str]:
      # Preserve timestamps; drop blank new_text_lines entries
      if len(lrc_lines) != len(new_text_lines):
         return lrc_lines

      out: list[str] = []
      for old, txt in zip(lrc_lines, new_text_lines):
         txt = (txt or "").strip()
         if not txt:
            continue

         s = (old or "").strip()
         if s.startswith("[") and "]" in s:
            ts = s.split("]", 1)[0]  # "[mm:ss.xx"
            out.append(ts + "] " + txt)
         else:
            out.append(txt)
      return out

   def _call_apply_base_lyrics(apply_fn, **kwargs):
      # Forward/backward compatible parameter passing
      sig = inspect.signature(apply_fn)
      filtered = {k: v for (k, v) in kwargs.items() if k in sig.parameters}
      return apply_fn(**filtered)

   # Skip existing output unless overwrite
   if out_lrc.exists() and not overwrite:
      return {"status": "skipped", "lrc": str(out_lrc), "reason": "existing .lrc"}

   if dry_run:
      return {"status": "dry_run", "lrc": str(out_lrc)}

   # Reset log
   if log_path:
      log_path.write_text("", encoding="utf-8", newline="\n")

   try:
      # ---- Whisper -> .srt ----
      # transcribe_to_srt should write an SRT file and return its path (or None; then we assume out_srt)
      srt_path = transcribe_to_srt(
         audio_path=audio_path,
         output_dir=audio_path.parent,
         model=whisper_model,
         language=language,
         device=device,
         log_path=log_path,
      ) or out_srt

      if not Path(srt_path).exists():
         raise RuntimeError(f"Whisper did not produce SRT: {srt_path}")

      # ---- Reduce SRT (text-only) -> .red.srt ----
      red_path = reduce_srt_file(
         srt_path=Path(srt_path),
         out_path=out_red,
         log_path=log_path,
      ) or out_red

      if not Path(red_path).exists():
         raise RuntimeError(f"Reducer did not produce red SRT: {red_path}")

      # ---- Read red SRT blocks ----
      blocks = read_srt(Path(red_path))
      if not blocks:
         raise RuntimeError("No subtitle blocks found after reduction.")

      # ---- Blocks -> LRC lines ----
      lrc_lines = srt_blocks_to_lrc_lines(blocks)

      # ---- Optional base lyrics alignment ----
      headers: list[str] = []

      resolved_base = _resolve_base_lyrics(base_lyrics_path, audio_path) if base_lyrics_path else None
      base_lines = None
      base_source = None
      base_found = False

      if resolved_base and resolved_base.is_file():
         base_lines = load_base_lyrics_lines(resolved_base)
         base_source = f"file:{resolved_base}"
         base_found = True
      elif base_lyrics_path:
         expected = str(resolved_base) if resolved_base else str(base_lyrics_path)
         _log(f"BASE: missing (expected: {expected})")
         if fetch_config and getattr(fetch_config, "enabled", False):
            base_lines = fetch_base_lyrics_lines(fetch_config, audio_path, log_fn=_log, layout=layout)
            if base_lines:
               base_source = f"fetch:{getattr(fetch_config, 'provider', '')}"
      elif fetch_config and getattr(fetch_config, "enabled", False):
         base_lines = fetch_base_lyrics_lines(fetch_config, audio_path, log_fn=_log, layout=layout)
         if base_lines:
            base_source = f"fetch:{getattr(fetch_config, 'provider', '')}"

      if base_lines:
         whisper_text_lines = _read_lrc_text_lines(lrc_lines)

         new_text_lines, stats = _call_apply_base_lyrics(
            apply_base_lyrics,
            whisper_lines=whisper_text_lines,
            base_lines=base_lines,
            threshold=base_threshold,
            window=base_window,
            strict=base_strict,
            max_merge=base_max_merge,

            # optional new knobs (only used if apply_base_lyrics supports them)
            diff_threshold=base_diff_threshold,
            enable_rescue=base_rescue,
         )

         lrc_lines = _apply_text_back_into_lrc(lrc_lines, new_text_lines)

         # Logging + optional header tags
         if base_source:
            _log(f"BASE: {base_source}")
         _log(f"BASE: replaced={getattr(stats, 'replaced', 0)} kept={getattr(stats, 'kept', 0)} dropped={getattr(stats, 'dropped', 0)}")
         _log(f"BASE: merged_spans={getattr(stats, 'merged_spans', 0)} max_span={getattr(stats, 'max_span', 1)} garbage_removed={getattr(stats, 'garbage_removed', 0)}")

         gsim = getattr(stats, "global_similarity", None)
         rescue_rep = getattr(stats, "rescue_replaced", None)
         rescue_triggered = getattr(stats, "rescue_triggered", None)
         rescue_applied = getattr(stats, "rescue_applied", None)
         rescue_skip_reason = getattr(stats, "rescue_skip_reason", None)

         if gsim is not None:
            _log(
               "BASE: global_similarity="
               f"{float(gsim):0.3f} base_diff_threshold={float(base_diff_threshold):0.2f} "
               f"rescue_enabled={bool(base_rescue)} rescue_triggered={bool(rescue_triggered)} "
               f"rescue_applied={bool(rescue_applied)}"
            )
         if rescue_rep is not None:
            _log(f"BASE: rescue_replaced={int(rescue_rep)}")
         if rescue_triggered and not rescue_applied and rescue_skip_reason:
            _log(f"BASE: rescue_no_changes={rescue_skip_reason}")

         if lrc_header:
            headers.append("[re:PyLy]")
            headers.append("[by:PyLy]")
            if gsim is not None:
               headers.append(f"[pyly_base_similarity:{float(gsim):0.3f}]")
               if rescue_triggered:
                  headers.append("[pyly_base_mode:rescue]")

      elif base_lyrics_path and not base_lines and not base_found:
         _log("BASE: none (missing and fetch disabled or failed)")

      # ---- Write LRC ----
      write_lrc(out_lrc, lrc_lines, overwrite=overwrite, headers=headers if headers else None)

      # ---- Cleanup ----
      if clean:
         for p in [out_srt, out_red]:
            try:
               if p.exists():
                  p.unlink()
            except Exception:
               pass

      return {"status": "ok", "lrc": str(out_lrc)}

   except Exception as e:
      _log("EXCEPTION: " + repr(e))
      _log(traceback.format_exc())

      # Leave intermediates/logs in place for debugging
      raise
