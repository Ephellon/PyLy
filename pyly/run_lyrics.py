from __future__ import annotations

import time
from pathlib import Path

import sys

from .inputs import AUDIO_EXTS, collect_audio_files
from .options import RunOptions
from .pipeline import run_pipeline
from . import lyrics_fetch
from .lrc_writer import write_lrc
from .console_ui import (
   banner, ok, warn, err, info, format_duration, LiveStatus, RollingETA,
)


def run_lyrics(opts: RunOptions) -> int:
   """
   Handle make-lyrics, get-lyrics, refresh-lyrics, upgrade-lyrics commands.
   Returns exit code: 0=ok, 1=some fail, 2=no input files.
   """
   # ---- Collect input files based on mode ----
   if opts.refresh_lyrics:
      # Port of old _run_redownload: collect .lrc files and audio files with sibling .lrc
      return _run_refresh_lyrics(opts)

   # All other modes: collect audio files
   audio_files = collect_audio_files([str(p) for p in opts.paths], opts.include_subfolders)

   if not audio_files:
      print("[!] No audio files found.", file=sys.stderr)
      return 2

   # ---- Determine pipeline parameters for the active mode ----
   # Default values (for make-lyrics)
   fetch_keep_mode = "primary"
   fetch_only = False
   overwrite = opts.replace

   if opts.upgrade_lyrics:
      # upgrade-lyrics: filter to audio with existing .lrc that has NO [PyLy:url] tag
      filtered = []
      for audio in audio_files:
         lrc_path = audio.with_suffix(".lrc")
         if lrc_path.exists():
            url = lyrics_fetch.read_pyly_url_from_lrc(lrc_path)
            if url is None:
               filtered.append(audio)
      audio_files = filtered
      if not audio_files:
         print(info("No Whisper-made lyrics to upgrade."))
         return 0
      fetch_keep_mode = "primary"
      fetch_only = True
      overwrite = True  # always overwrite for upgrade
   elif opts.get_lyrics is not None and not opts.make_lyrics:
      # get-lyrics without make-lyrics: fetch only, never Whisper
      fetch_keep_mode = "primary"
      fetch_only = True
   elif opts.make_lyrics or opts.get_lyrics is not None:
      # make-lyrics (default) or get-lyrics with make-lyrics
      fetch_keep_mode = "alternate" if opts.keep_both else "primary"
      fetch_only = False
   else:
      # Should not happen (default action is make-lyrics), but fall back
      fetch_keep_mode = "primary"
      fetch_only = False

   # Parse fetch config
   fetch_config = lyrics_fetch.parse_fetch_arg(opts.lyric_sources, allow_scraping=opts.allow_scraping)

   # Base lyrics path
   base_lyrics_path = Path(opts.use_lyrics) if opts.use_lyrics else None

   # ---- Run pipeline for each file ----
   total = len(audio_files)
   banner(f"PyLy — {total} file(s) queued")

   ok_count = 0
   fail_count = 0
   skipped_count = 0

   eta = RollingETA(total=total, window=5)
   live = LiveStatus(enabled=True)

   for idx, audio in enumerate(audio_files, start=1):
      completed = ok_count + skipped_count + fail_count
      live.update(f"[{idx}/{total}] {audio.name}  (ETA ~ {eta.eta_string(completed)})")

      t0 = time.time()
      try:
         result = run_pipeline(
            audio_path=audio,
            overwrite=overwrite,
            clean=opts.clean_up,
            dry_run=opts.preview,
            write_log=opts.write_log,
            whisper_model=opts.model,
            language=opts.language,
            device=opts.device,

            base_lyrics_path=base_lyrics_path,
            base_strict=opts.drop_extras,
            base_threshold=opts.match_score,
            base_window=opts.match_window,
            base_max_merge=opts.merge_limit,

            base_diff_threshold=opts.rescue_score,
            base_rescue=not opts.skip_rescue,
            truth_mode=opts.trust_lyrics,

            lrc_header=not opts.skip_header,
            fetch_config=fetch_config,
            fetch_keep_mode=fetch_keep_mode,
            layout=opts.layout,
            fetch_only=fetch_only,
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
            if result.get("reason"):
               out_name += f", {result['reason']}"
            live.commit(ok(f"OK ({out_name})  [{format_duration(dt)}]"))

      except Exception as e:
         dt = time.time() - t0
         fail_count += 1
         eta.add(dt)
         live.commit(err(f"{e}  [{format_duration(dt)}]"))

      completed = ok_count + skipped_count + fail_count
      overall_eta = eta.eta_string(completed)
      live.update(f"Progress: {completed}/{total}  |  Overall ETA ~ {overall_eta}")

   live.clear()
   banner(f"{ok_count} - OK / {skipped_count} - SKIPPED / {fail_count} - FAIL")
   return 0 if fail_count == 0 else 1


def _run_refresh_lyrics(opts: RunOptions) -> int:
   """
   Port of old _run_redownload: refresh lyrics from embedded URLs.
   Saves by default; with --preview only reports.
   """
   # Collect .lrc files from paths (audio files and directories)
   lrc_files: list[Path] = []

   for raw in opts.paths:
      p = Path(raw).expanduser()
      if not p.exists():
         print(f"[X] Path not found: {p}", file=sys.stderr)
         return 2

      if p.is_file():
         if p.suffix.lower() == ".lrc":
            lrc_files.append(p.resolve())
         elif p.suffix.lower() in AUDIO_EXTS:
            sibling = p.with_suffix(".lrc")
            if sibling.exists():
               lrc_files.append(sibling.resolve())
            else:
               print(warn(f"No .lrc found for {p.name}"))
         else:
            print(f"[X] Not a supported file type for --refresh-lyrics: {p}", file=sys.stderr)
            return 2
      elif p.is_dir():
         pattern = p.rglob("*.lrc") if opts.include_subfolders else p.glob("*.lrc")
         lrc_files.extend(f.resolve() for f in pattern)

   lrc_files = sorted(set(lrc_files))

   if not lrc_files:
      print("[!] No .lrc files found.", file=sys.stderr)
      return 1

   total = len(lrc_files)
   banner(f"PyLy --refresh-lyrics — {total} file(s)")

   ok_count = 0
   skip_count = 0
   fail_count = 0
   no_url_count = 0

   live = LiveStatus(enabled=True)
   eta = RollingETA(total=total, window=5)

   for idx, lrc_path in enumerate(lrc_files, start=1):
      completed = ok_count + skip_count + fail_count + no_url_count
      live.update(f"[{idx}/{total}] {lrc_path.name}  (ETA ~ {eta.eta_string(completed)})")
      t0 = time.time()

      try:
         embedded_url = lyrics_fetch.read_pyly_url_from_lrc(lrc_path)
         if not embedded_url:
            no_url_count += 1
            dt = time.time() - t0
            eta.add(min(dt, 0.1))
            live.commit(ok(f"No URL tag  ({lrc_path.name})  [{format_duration(dt)}]"))
            continue

         fetched = lyrics_fetch.fetch_by_url(embedded_url)
         if not fetched or not fetched.synced_lrc_text:
            fail_count += 1
            dt = time.time() - t0
            eta.add(dt)
            live.commit(err(f"Fetch failed ({lrc_path.name})  [{format_duration(dt)}]"))
            continue

         if opts.preview:
            # Preview mode: report only, don't save
            skip_count += 1
            dt = time.time() - t0
            eta.add(min(dt, 0.5))
            live.commit(ok(f"Would update ({lrc_path.name})  [{format_duration(dt)}]"))
            continue

         # Find sibling audio file for duration measurement
         audio_sibling: Path | None = None
         for ext in sorted(AUDIO_EXTS):
            candidate = lrc_path.with_suffix(ext)
            if candidate.exists():
               audio_sibling = candidate
               break

         headers = lyrics_fetch.build_lrc_metadata_headers(
            fetched=fetched,
            audio_path=audio_sibling,
            include_standard_tags=not opts.skip_header,
         ) if not opts.skip_header else []

         synced_lines = fetched.synced_lrc_text.splitlines()
         write_lrc(lrc_path, synced_lines, overwrite=True, headers=headers)

         ok_count += 1
         dt = time.time() - t0
         eta.add(dt)
         live.commit(ok(f"Updated ({lrc_path.name})  [{format_duration(dt)}]"))

      except Exception as e:
         fail_count += 1
         dt = time.time() - t0
         eta.add(dt)
         live.commit(err(f"{e}  ({lrc_path.name})  [{format_duration(dt)}]"))

   live.clear()
   if no_url_count:
      banner(f"{ok_count} - UPDATED / {skip_count} - SKIPPED / {no_url_count} - NO URL / {fail_count} - FAIL")
   else:
      banner(f"{ok_count} - UPDATED / {skip_count} - SKIPPED / {fail_count} - FAIL")
   return 0 if fail_count == 0 else 1
