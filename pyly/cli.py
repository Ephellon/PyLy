import argparse
import sys
import time
from pathlib import Path

from .pipeline import run_pipeline
from .console_ui import LiveStatus, banner, ok, err, warn, info, RollingETA, format_duration, set_color_enabled


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


def _collect_lrc_inputs(
   inputs: list[str | Path],
   recursive: bool = False,
) -> list[Path]:
   """Collect .lrc files from the given paths."""
   results: set[Path] = set()

   for raw in inputs:
      p = Path(raw).expanduser()

      if not p.exists():
         raise FileNotFoundError(f"Input not found: {p}")

      if p.is_file():
         if p.suffix.lower() == ".lrc":
            results.add(p.resolve())
         else:
            raise ValueError(f"Not an .lrc file: {p}")
         continue

      if p.is_dir():
         for f in (p.rglob("*.lrc") if recursive else p.glob("*.lrc")):
            results.add(f.resolve())

   return sorted(results)


def _configure_stdio() -> None:
   """
   Force stdout/stderr to UTF-8 so PyLy's output (the ✓ glyphs, em dashes, and
   Unicode characters in filenames) survives being redirected to a file or pipe.

   When stdout is a real console this is already fine, but ``pyly ... > out.log``
   makes Python encode with the locale codepage (cp1252 on Windows), which
   raises UnicodeEncodeError on any non-cp1252 character. ``errors="replace"``
   keeps us crash-proof even on genuinely unencodable bytes.
   """
   for stream in (sys.stdout, sys.stderr):
      try:
         stream.reconfigure(encoding="utf-8", errors="replace")
      except Exception:
         pass


def main(argv: list[str] | None = None) -> int:
   _configure_stdio()
   ap = argparse.ArgumentParser(prog="pyly", add_help=True)
   ap.add_argument("path", nargs="?", default=None, help="Audio file, .lrc file (for --redownload), or folder")
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

   # Fetch
   ap.add_argument("--fetch", "-f", nargs="?", const="", default=None,
                   help="Fetch base lyrics online (optional provider/template).")
   ap.add_argument("--keep-as-primary", "-k", action="store_true",
                   help="Prefer fetched synced LRC when available (implies --fetch).")
   ap.add_argument("--keep-as-alternate", "-K", action="store_true",
                   help="Keep fetched synced LRC as <basename>.fetched.lrc (implies --fetch).")
   ap.add_argument(
      "--layout", "-y", default=None,
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
                   help="Write metadata + PyLy tags into the LRC header. Default: on.")
   ap.add_argument("--no-lrc-header", "-A", dest="lrc_header", action="store_false",
                   help="Do not write header tags.")
   ap.set_defaults(lrc_header=True)

   # Re-download
   ap.add_argument(
      "--redownload", "-R", action="store_true",
      help=(
         "Re-fetch lyrics for existing .lrc files using the [PyLy:<url>] tag embedded in them. "
         "Accepts audio files, .lrc files, or directories. Requires --overwrite to actually write."
      ),
   )

   # Provider scraping opt-in
   ap.add_argument(
      "--allow-provider-site-scraping", action="store_true", default=False,
      help=(
         "Allow lyric providers that work by scraping websites rather than using a documented API. "
         "Disabled by default. Only enable if you accept the provider's terms of use."
      ),
   )

   # Find-better: only reprocess Whisper-transcribed .lrc files
   ap.add_argument(
      "--find-better", "-F", action="store_true", default=False,
      help=(
         "Only process audio files whose existing .lrc was transcribed by Whisper "
         "(i.e. has no external [PyLy:<url>] tag). Files with externally fetched lyrics are skipped."
      ),
   )

   # Provider listing (early-exit, no path required)
   ap.add_argument(
      "--list-providers", "-p", action="store_true", default=False,
      help="Print all available lyric providers and exit.",
   )

   # File metadata check / match (standalone modes; no Whisper)
   ap.add_argument(
      "--check-file-metadata", "-z", nargs="?", const="strict", default=None,
      choices=["strict", "loose"], metavar="MODE",
      help=(
         "Check that each audio file's embedded tags (title, artist, album, year, track) "
         "match what the file should contain — derived from album.nfo/artist.nfo, then the "
         "folder layout, then the filename. Reports mismatches; writes nothing. "
         "MODE: strict (default — must match the .nfo field) or loose (may match the .nfo "
         "or the folder layout, and ignores trailing '(...)' qualifiers like '(Digital Media 01)')."
      ),
   )
   ap.add_argument(
      "--match-file-metadata", "-Z", nargs="?", const="backup", default=None,
      metavar="MODE",
      help=(
         "Fix mismatched tags by writing the expected values (sourced from album.nfo, then "
         "layout, then filename) into the audio file via ffmpeg. MODE is one of: "
         "backup (default — copy original to <file>.bak first), direct (in place, no backup), "
         "copy (leave original, write <stem>.tagged.<ext>). Comparison strictness is strict "
         "by default; combine with '-z loose' to fix under loose matching."
      ),
   )

   # Enrich album.nfo from MusicBrainz using its embedded MBID
   ap.add_argument(
      "--update-nfo", "-n", action="store_true", default=False,
      help=(
         "Fill in missing album.nfo data (album artist, year, barcode, and the full track "
         "list with titles/durations/MBIDs) by looking up the embedded musicbrainzalbumid "
         "(or musicbrainzreleasegroupid) on MusicBrainz. Non-destructive: existing fields are "
         "kept unless --overwrite is given. Backs up album.nfo.bak first; honours --dry-run."
      ),
   )

   ns = ap.parse_args(argv)
   set_color_enabled(ns.color)

   # --list-providers: print registry and exit (no path needed)
   if ns.list_providers:
      from .lyrics_fetch import list_providers
      providers = list_providers()
      print()
      print("  Available lyric providers:")
      print()
      for p in providers:
         scraping_note = "  [requires --allow-provider-site-scraping]" if p.requires_scraping else ""
         print(f"    {p.name:<18} {p.description}{scraping_note}")
      print()
      return 0

   if not ns.path:
      ap.error("the following arguments are required: path")

   if ns.online:
      print("[X] --online is not implemented. Offline Whisper is the default.", file=sys.stderr)
      return 2

   if ns.keep_as_primary and ns.keep_as_alternate:
      print("[X] --keep-as-primary and --keep-as-alternate are mutually exclusive.", file=sys.stderr)
      return 2

   if (ns.keep_as_primary or ns.keep_as_alternate) and ns.fetch is None:
      ns.fetch = ""

   # ------------------------------------------------------------------
   # --check-file-metadata / --match-file-metadata: standalone tag modes
   # ------------------------------------------------------------------
   if ns.match_file_metadata is not None:
      mode = ns.match_file_metadata.strip().lower()
      if mode not in {"backup", "direct", "copy"}:
         print(f"[X] --match-file-metadata MODE must be backup, direct, or copy (got {mode!r}).", file=sys.stderr)
         return 2
      strictness = ns.check_file_metadata or "strict"
      return _run_file_metadata(ns, write_mode=mode, strictness=strictness)
   if ns.check_file_metadata:
      return _run_file_metadata(ns, write_mode=None, strictness=ns.check_file_metadata)

   if ns.update_nfo:
      return _run_update_nfo(ns)

   # ------------------------------------------------------------------
   # --redownload mode: work directly from .lrc files (or audio files)
   # ------------------------------------------------------------------
   if ns.redownload:
      return _run_redownload(ns)

   # ------------------------------------------------------------------
   # Normal pipeline mode
   # ------------------------------------------------------------------
   try:
      inputs = _collect_inputs([ns.path], ns.recursive)
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not inputs:
      print("[!] No supported audio files found.", file=sys.stderr)
      return 1

   if ns.find_better:
      from .lyrics_fetch import read_pyly_url_from_lrc as _read_url
      filtered = []
      for audio in inputs:
         lrc = audio.with_suffix(".lrc")
         if lrc.exists() and _read_url(lrc) is not None:
            continue  # externally fetched — skip
         filtered.append(audio)
      inputs = filtered
      if not inputs:
         print("[!] No Whisper-transcribed files found (all existing .lrc files have an external URL).", file=sys.stderr)
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
   fetch_config = parse_fetch_arg(ns.fetch, allow_scraping=ns.allow_provider_site_scraping)

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
            fetch_keep_mode="primary" if ns.keep_as_primary else ("alternate" if ns.keep_as_alternate else None),
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


def _run_redownload(ns) -> int:
   """
   Handle --redownload mode.

   Accepts:
   - Audio files: looks for sibling .lrc, reads [PyLy:url], re-fetches
   - .lrc files: reads [PyLy:url] directly, re-fetches
   - Directories: collects .lrc files (and audio files with sibling .lrc)
   """
   from pathlib import Path as _Path
   from .lyrics_fetch import read_pyly_url_from_lrc, fetch_by_url, build_lrc_metadata_headers
   from .lrc_writer import write_lrc
   from .console_ui import info as _info

   p = _Path(ns.path).expanduser()

   # Collect .lrc files
   lrc_files: list[_Path] = []

   if p.is_file():
      if p.suffix.lower() == ".lrc":
         lrc_files.append(p.resolve())
      elif p.suffix.lower() in {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".alac", ".wma", ".aiff"}:
         # audio file — find sibling .lrc
         sibling = p.with_suffix(".lrc")
         if sibling.exists():
            lrc_files.append(sibling.resolve())
         else:
            print(f"[!] No .lrc found for {p.name}", file=sys.stderr)
            return 1
      else:
         print(f"[X] Not a supported file type for --redownload: {p}", file=sys.stderr)
         return 2
   elif p.is_dir():
      pattern = p.rglob("*.lrc") if ns.recursive else p.glob("*.lrc")
      lrc_files = sorted(f.resolve() for f in pattern)
   else:
      print(f"[X] Path not found: {p}", file=sys.stderr)
      return 2

   if not lrc_files:
      print("[!] No .lrc files found.", file=sys.stderr)
      return 1

   total = len(lrc_files)
   banner(f"PyLy --redownload — {total} file(s)")

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
         embedded_url = read_pyly_url_from_lrc(lrc_path)
         if not embedded_url:
            no_url_count += 1
            dt = time.time() - t0
            eta.add(min(dt, 0.1))
            live.commit(ok(f"No URL tag  ({lrc_path.name})  [{format_duration(dt)}]"))
            continue

         fetched = fetch_by_url(embedded_url)
         if not fetched or not fetched.synced_lrc_text:
            fail_count += 1
            dt = time.time() - t0
            eta.add(dt)
            live.commit(err(f"Fetch failed ({lrc_path.name})  [{format_duration(dt)}]"))
            continue

         if not ns.overwrite:
            skip_count += 1
            dt = time.time() - t0
            eta.add(min(dt, 0.5))
            live.commit(ok(f"Skipped (use --overwrite to update) ({lrc_path.name})  [{format_duration(dt)}]"))
            continue

         # Find sibling audio file for duration measurement
         audio_sibling: _Path | None = None
         for ext in (".flac", ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".alac", ".wma", ".aiff"):
            candidate = lrc_path.with_suffix(ext)
            if candidate.exists():
               audio_sibling = candidate
               break

         headers = build_lrc_metadata_headers(
            fetched=fetched,
            audio_path=audio_sibling,
            include_standard_tags=ns.lrc_header,
         ) if ns.lrc_header else []

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


def _run_file_metadata(ns, write_mode: str | None, strictness: str = "strict") -> int:
   """
   Handle --check-file-metadata (write_mode=None) and
   --match-file-metadata (write_mode in {backup, direct, copy}).

   Reads each audio file's embedded tags, derives the expected values from
   album.nfo/artist.nfo -> folder layout -> filename, and either reports
   mismatches (check) or writes corrections via ffmpeg (match). ``strictness``
   is strict or loose (see compare_metadata).
   """
   from .file_metadata import (
      read_embedded_tags,
      actual_metadata,
      expected_metadata,
      layout_metadata,
      compare_metadata,
      find_album_nfo,
      find_artist_nfo,
      has_problems,
      write_tags,
   )

   try:
      inputs = _collect_inputs([ns.path], ns.recursive)
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not inputs:
      print("[!] No supported audio files found.", file=sys.stderr)
      return 1

   action = f"--match-file-metadata ({write_mode})" if write_mode else "--check-file-metadata"
   banner(f"PyLy {action} [{strictness}] — {len(inputs)} file(s)")

   clean_count = 0      # files whose tags already match
   problem_count = 0    # files with at least one mismatch/missing
   fixed_count = 0      # files actually written (match mode)
   fail_count = 0

   for audio in inputs:
      try:
         album_nfo = find_album_nfo(audio)
         artist_nfo = find_artist_nfo(audio)
         tags = read_embedded_tags(audio)
         expected = expected_metadata(audio, ns.layout, album_nfo, artist_nfo)
         actual = actual_metadata(tags)
         layout_exp = layout_metadata(audio, ns.layout)
         diffs = compare_metadata(expected, actual, layout_exp, mode=strictness)

         if not has_problems(diffs):
            clean_count += 1
            print(ok(f"{audio.name}  (tags match)"))
            continue

         problem_count += 1
         sources = []
         if album_nfo:
            sources.append("album.nfo")
         if artist_nfo:
            sources.append("artist.nfo")
         nfo_note = f"  [{', '.join(sources)}]" if sources else "  [no .nfo]"
         print(warn(f"{audio.name}{nfo_note}"))
         for d in diffs:
            if d.status == "mismatch":
               print(info(f"  {d.field}: tag={d.actual!r}  ->  expected {d.expected!r}"))
            elif d.status == "missing":
               print(info(f"  {d.field}: tag missing  ->  expected {d.expected!r}"))

         if write_mode:
            changed, message = write_tags(
               audio, expected, diffs,
               mode=write_mode, dry_run=ns.dry_run,
            )
            if changed:
               fixed_count += 1
               print(ok(f"  {message}"))
            else:
               print(info(f"  (skipped write: {message})"))

      except Exception as e:
         fail_count += 1
         print(err(f"{audio.name}: {e}"))

   if write_mode:
      verb = "WOULD FIX" if ns.dry_run else "FIXED"
      banner(
         f"{clean_count} - OK / {problem_count} - MISMATCH / "
         f"{fixed_count} - {verb} / {fail_count} - FAIL"
      )
   else:
      banner(f"{clean_count} - OK / {problem_count} - MISMATCH / {fail_count} - FAIL")

   # Non-zero exit when problems remain or anything failed, so this is usable
   # as a check in scripts. In match mode, fixed files no longer count as bad.
   unresolved = (problem_count - fixed_count) if write_mode else problem_count
   return 0 if (unresolved == 0 and fail_count == 0) else 1


def _run_update_nfo(ns) -> int:
   """
   Handle --update-nfo: enrich each album.nfo from its embedded MusicBrainz id.

   Collects album.nfo files under the path, looks up the release on MusicBrainz
   (release id first, then release-group id), and writes back any missing
   fields plus the track list. --overwrite replaces conflicting fields;
   --dry-run previews; a .bak copy is kept before any write.
   """
   import shutil
   from pathlib import Path as _Path

   from .file_metadata import read_nfo_mbids, enrich_album_nfo, _read_text
   from .lyrics_fetch import fetch_mb_release, resolve_release_from_group

   root = _Path(ns.path).expanduser()
   if not root.exists():
      print(f"[X] Path not found: {root}", file=sys.stderr)
      return 2

   if root.is_file() and root.name.lower() == "album.nfo":
      nfo_files = [root.resolve()]
   elif root.is_dir():
      pattern = root.rglob("album.nfo") if ns.recursive else root.glob("album.nfo")
      nfo_files = sorted(f.resolve() for f in pattern)
   else:
      print(f"[X] --update-nfo expects an album.nfo file or a folder (got {root.name}).", file=sys.stderr)
      return 2

   if not nfo_files:
      print("[!] No album.nfo files found.", file=sys.stderr)
      return 1

   banner(f"PyLy --update-nfo — {len(nfo_files)} album.nfo file(s)")

   updated = skipped = no_id = fail = 0

   for nfo_path in nfo_files:
      label = nfo_path.parent.name or nfo_path.name
      try:
         release_id, group_id = read_nfo_mbids(nfo_path)
         if not release_id and group_id:
            release_id = resolve_release_from_group(group_id) or ""
         if not release_id:
            no_id += 1
            print(warn(f"{label}  (no musicbrainz id in album.nfo)"))
            continue

         release = fetch_mb_release(release_id)
         if not release:
            fail += 1
            print(err(f"{label}  (MusicBrainz lookup failed)"))
            continue

         changes, new_text = enrich_album_nfo(nfo_path, release, overwrite=ns.overwrite)
         if not changes:
            skipped += 1
            print(ok(f"{label}  (already complete)"))
            continue

         summary = ", ".join(changes)
         if ns.dry_run:
            print(ok(f"{label}  would update: {summary}"))
            updated += 1
            continue

         if new_text != (_read_text(nfo_path) or ""):
            bak = nfo_path.with_name(nfo_path.name + ".bak")
            if not bak.exists():
               shutil.copy2(nfo_path, bak)
            nfo_path.write_text(new_text, encoding="utf-8", newline="\n")
         updated += 1
         print(ok(f"{label}  updated: {summary}"))

      except Exception as e:
         fail += 1
         print(err(f"{label}: {e}"))

   verb = "WOULD UPDATE" if ns.dry_run else "UPDATED"
   banner(f"{updated} - {verb} / {skipped} - COMPLETE / {no_id} - NO ID / {fail} - FAIL")
   return 0 if fail == 0 else 1


if __name__ == "__main__":
   raise SystemExit(main())
