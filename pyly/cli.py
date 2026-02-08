import argparse
import sys
import time
from pathlib import Path

from .pipeline import run_pipeline
from .console_ui import LiveStatus, banner, ok, err, RollingETA, format_duration, set_color_enabled


AUDIO_EXTS = {
   ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".alac", ".wma", ".aiff"
}


def _is_audio_file(path: Path) -> bool:
   return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def _collect_inputs(
   inputs: list[str | Path],
   recursive: bool = False,
) -> list[Path]:
   """
   Resolve CLI inputs into a sorted list of audio files.

   Rules:
   - Files are accepted only if they are known audio types
   - Directories yield audio files inside them
   - --recursive controls deep traversal
   - Duplicates are removed
   - Output is deterministic
   """

   results: set[Path] = set()

   for raw in inputs:
      p = Path(raw).expanduser()

      # Allow globbing explicitly (Windows cmd doesn't expand *)
      if "*" in str(p) or "?" in str(p):
         for gp in p.parent.glob(p.name):
            if gp.is_dir():
               if recursive:
                  for f in gp.rglob("*"):
                     if _is_audio_file(f):
                        results.add(f.resolve())
               else:
                  for f in gp.iterdir():
                     if _is_audio_file(f):
                        results.add(f.resolve())
            elif _is_audio_file(gp):
               results.add(gp.resolve())
         continue

      if not p.exists():
         raise FileNotFoundError(f"Input not found: {p}")

      if p.is_file():
         if _is_audio_file(p):
            results.add(p.resolve())
         else:
            raise ValueError(f"Not an audio file: {p}")
         continue

      if p.is_dir():
         if recursive:
            for f in p.rglob("*"):
               if _is_audio_file(f):
                  results.add(f.resolve())
         else:
            for f in p.iterdir():
               if _is_audio_file(f):
                  results.add(f.resolve())
         continue

   return sorted(results)


def main(argv: list[str] | None = None) -> int:
   ap = argparse.ArgumentParser(prog="pyly", add_help=True)
   ap.add_argument("path", help="Audio file or folder")
   ap.add_argument("--recursive", "-r", action="store_true", help="Recurse when path is a folder")
   ap.add_argument("--overwrite", "-o", action="store_true", help="Overwrite existing .lrc")
   ap.add_argument("--clean", "-c", action="store_true", help="Delete intermediates after success")
   ap.add_argument("--dry-run", "-q", action="store_true", help="Print actions without running")
   ap.add_argument("--log", "-v", action="store_true", help="Write per-file .pyly.log")
   ap.add_argument("--model", "-m", default="small", help="Whisper model (tiny/base/small/medium/large)")
   ap.add_argument("--language", "-l", default=None, help="Language code (e.g., en). Optional.")
   ap.add_argument("--device", "-d", default=None, help="Device (cpu/cuda). Optional pass-through.")
   ap.add_argument("--online", action="store_true", help="Opt-in online mode (currently unimplemented)")
   color_group = ap.add_mutually_exclusive_group()
   color_group.add_argument("--color", dest="color", action="store_const", const=True,
                            help="Force color output")
   color_group.add_argument("--no-color", dest="color", action="store_const", const=False,
                            help="Disable color output")
   ap.set_defaults(color=None)

   # Base lyrics
   ap.add_argument("--base", "-b", dest="base_lyrics", default=None, help="Text-only lyrics file (no timing)")
   ap.add_argument("--base-lyrics", dest="base_lyrics", default=None, help="Alias of --base")
   ap.add_argument("--lyrics", dest="base_lyrics", default=None, help="Alias of --base")
   ap.add_argument("--truth", "--base-truth", "-u", dest="truth_mode", action="store_true",
                   help="Treat base lyrics as ground truth for patching (guarded by similarity).")
   ap.add_argument("--base-strict", "-s", action="store_true",
                   help="Drop unmatched Whisper lines when base lyrics are provided")
   ap.add_argument("--base-threshold", "-t", type=float, default=0.82,
                   help="Similarity threshold (0..1) to replace with base. Default: 0.82")
   ap.add_argument("--base-window", "-w", type=int, default=12,
                   help="Lookahead window in base lines while matching. Default: 12")
   ap.add_argument("--base-max-merge", "-x", type=int, default=5,
                   help="Max Whisper lines to merge into one base match. Default: 5")
   ap.add_argument("--fetch", "-f", nargs="?", const="", default=None,
                   help="Fetch base lyrics online (optional provider/template).")
   ap.add_argument(
      "--layout",
      "-y",
      default=None,
      help=(
         "Optional layout hint: lidarr/plex/flat preset or a custom template string. "
         "Templates use token braces (e.g. {Artist Name}) and are used only when tags are missing."
      ),
   )

   # Diff / rescue
   ap.add_argument("--base-diff-threshold", "-i", type=float, default=0.75,
                   help="Enable rescue pass if global similarity >= this. Default: 0.75")
   ap.add_argument("--base-rescue", "-e", dest="base_rescue", action="store_true",
                   help="Enable diff-driven rescue pass (default when base is used).")
   ap.add_argument("--no-base-rescue", "-E", dest="base_rescue", action="store_false",
                   help="Disable diff-driven rescue pass.")
   ap.set_defaults(base_rescue=True)

   # LRC header tags
   ap.add_argument("--lrc-header", "-a", dest="lrc_header", action="store_true",
                   help="Write PyLy tags into the LRC header. Default: on.")
   ap.add_argument("--no-lrc-header", "-A", dest="lrc_header", action="store_false",
                   help="Do not write header tags.")
   ap.set_defaults(lrc_header=True)

   ns = ap.parse_args(argv)
   set_color_enabled(ns.color)

   if ns.online:
      print("[X] --online is not implemented. Offline Whisper is the default.", file=sys.stderr)
      return 2

   try:
      inputs = _collect_inputs([ns.path], ns.recursive)
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not inputs:
      print("[!] No supported audio files found.", file=sys.stderr)
      return 1

   base_arg = Path(ns.base_lyrics) if ns.base_lyrics else None
   if base_arg and base_arg.is_absolute():
      has_wildcard = "*" in base_arg.name or "?" in base_arg.name
      if not has_wildcard and not base_arg.is_file():
         print(f"[X] Base lyrics file not found: {base_arg}", file=sys.stderr)
         return 2

   total = len(inputs)
   banner(f"PyLy — {total} file(s) queued")

   ok_count = 0
   fail_count = 0
   skipped_count = 0

   eta = RollingETA(total=total, window=5)
   live = LiveStatus(enabled=True)

   from .lyrics_fetch import parse_fetch_arg
   fetch_config = parse_fetch_arg(ns.fetch)

   for idx, audio in enumerate(inputs, start=1):
      completed = (ok_count + skipped_count + fail_count)
      eta_str = eta.eta_string(completed)
      live.update(f"[{idx}/{total}] {audio.name}  (ETA ~ {eta_str})")

      t0 = time.time()
      try:
         result = run_pipeline(
            audio_path=audio,
            overwrite=ns.overwrite,
            clean=ns.clean,
            dry_run=ns.dry_run,
            write_log=ns.log,
            whisper_model=ns.model,
            language=ns.language,
            device=ns.device,

            base_lyrics_path=base_arg,
            base_strict=ns.base_strict,
            base_threshold=ns.base_threshold,
            base_window=ns.base_window,
            base_max_merge=ns.base_max_merge,

            base_diff_threshold=ns.base_diff_threshold,
            base_rescue=ns.base_rescue,
            truth_mode=ns.truth_mode,

            lrc_header=ns.lrc_header,
            fetch_config=fetch_config,
            layout=ns.layout,
         )
         dt = time.time() - t0

         status = result.get("status", "ok")
         if status == "skipped":
            skipped_count += 1
            eta.add(min(dt, 2.0))
            live.commit(ok(f"Skipped ({Path(result.get('lrc', '')).name})  [{format_duration(dt)}]"))
         elif status == "dry_run":
            ok_count += 1
            eta.add(min(dt, 2.0))
            live.commit(ok(f"Dry run  [{format_duration(dt)}]"))
         else:
            ok_count += 1
            eta.add(dt)
            out_name = Path(result.get("lrc", "")).name
            live.commit(ok(f"OK ({out_name})  [{format_duration(dt)}]"))

      except Exception as e:
         dt = time.time() - t0
         fail_count += 1
         eta.add(dt)
         live.commit(err(f"{e}  [{format_duration(dt)}]"))

      completed = (ok_count + skipped_count + fail_count)
      overall_eta = eta.eta_string(completed)
      live.update(f"Progress: {completed}/{total}  |  Overall ETA ~ {overall_eta}")

   live.clear()
   banner(f"{ok_count} - OK / {skipped_count} - SKIPPED / {fail_count} - FAIL")
   return 0 if fail_count == 0 else 1


if __name__ == "__main__":
   raise SystemExit(main())
