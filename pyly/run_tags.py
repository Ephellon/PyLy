"""
Tag and cover art actions for PyLy 2.

This module implements the four tag-related actions:
- run_get_info: enrich album.nfo from MusicBrainz (old --update-nfo)
- run_fix_tags: rewrite audio tags to match album.nfo (old --match-file-metadata)
- run_check_tags: report tag mismatches without writing (old --check-file-metadata)
- run_cover: embed cover art from local files or online sources (old --embed-art)

Each function takes a finished RunOptions object and returns an exit code.
"""

from __future__ import annotations

import sys
import re
import shutil
import urllib.parse
from pathlib import Path

from pyly.options import RunOptions
from pyly.inputs import collect_audio_files
from pyly.file_metadata import (
   read_embedded_tags,
   actual_metadata,
   expected_metadata,
   layout_metadata,
   compare_metadata,
   find_album_nfo,
   find_artist_nfo,
   read_nfo_mbids,
   album_nfo_missing_fields,
   enrich_album_nfo,
   parse_album_nfo,
   read_nfo_artist_mbid,
   has_problems,
   write_tags,
   _read_text,
)
from pyly.cover_art import (
   ArtResolver,
   split_source_key,
   resolve_art_key,
   find_local_art,
   can_embed,
   cannot_hold_art,
   has_embedded_art,
   embed_cover,
)
from pyly.lyrics_fetch import (
   MBNotFoundError,
   fetch_mb_release,
   resolve_release_from_group,
   search_mb_release,
)
from pyly.console_ui import banner, ok, err, warn, info


# ---------------------------------------------------------------------------
# run_get_info: enrich album.nfo from MusicBrainz
# ---------------------------------------------------------------------------

def _album_track_count(folder: Path, nfo) -> int | None:
   """
   How many tracks the album has: the album.nfo track list if it has one, else
   the larger of the number of audio files and the highest track number in a
   filename ("12 Song.mp3" means at least 12, even when tracks are missing).
   """
   if nfo and nfo.tracks:
      return len(nfo.tracks)
   files = collect_audio_files([folder], recursive=False)
   numbers = [int(m.group(1)) for f in files if (m := re.match(r"(\d{1,3})\D", f.stem))]
   return max([len(files), *numbers]) or None


def _suggest_release(nfo_path: Path) -> str:
   """
   For an album.nfo with a stale MusicBrainz id: search MusicBrainz by artist,
   album and track count, and return a line naming the best match for a person
   to check and paste in. Nothing is written.
   """
   folder = nfo_path.parent
   nfo = parse_album_nfo(nfo_path)
   # album.nfo first, then the folder names (Artist\Album).
   artist = (nfo.album_artist if nfo else "") or folder.parent.name
   album = (nfo.album_title if nfo else "") or folder.name
   track_count = _album_track_count(folder, nfo)

   match = search_mb_release(artist, album, track_count, artist_id=read_nfo_artist_mbid(nfo_path))
   if not match:
      search = urllib.parse.urlencode({"query": f"{artist} {album}", "type": "release"})
      return info(f"   no close match on MusicBrainz; search by hand: https://musicbrainz.org/search?{search}")

   tracks = f"{match.track_count} tracks" if match.track_count is not None else "track count unknown"
   kind = "exact match" if match.exact else "closest match"
   details = ", ".join(x for x in (tracks, match.date) if x)
   others = (f"\n   {match.others} other edition(s) fit just as well (different country or format); "
             "they usually share the same track list") if match.others else ""
   return info(
      f"   {kind}: {match.title!r} by {match.artist} ({details}) {match.url}{others}\n"
      f"   not applied; to use it, put {match.release_id} in <musicbrainzalbumid> in {nfo_path.name}"
   )


def run_get_info(opts: RunOptions) -> int:
   """
   Fill in missing album.nfo details from MusicBrainz.
   With --replace, overwrite details already there.
   Returns 0 on success, 1 if any file failed, 2 on bad input.
   """
   try:
      # Collect album.nfo files from the given paths
      nfo_files: list[Path] = []
      for raw in opts.paths:
         p = Path(raw).expanduser()
         if not p.exists():
            print(f"[X] Path not found: {p}", file=sys.stderr)
            return 2
         if p.is_file() and p.name.lower() == "album.nfo":
            nfo_files.append(p.resolve())
         elif p.is_dir():
            pattern = p.rglob("album.nfo") if opts.include_subfolders else p.glob("album.nfo")
            nfo_files.extend(sorted(f.resolve() for f in pattern))
         else:
            print(f"[X] --get-info expects an album.nfo file or a folder (got {p.name}).", file=sys.stderr)
            return 2
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not nfo_files:
      # Nothing to enrich is not a failure
      print("(i) No album.nfo files found; nothing to update.")
      return 0

   banner(f"PyLy --get-info — {len(nfo_files)} album.nfo file(s)")

   updated = skipped = no_id = fail = 0
   overwrite = opts.replace
   dry_run = opts.preview

   for nfo_path in nfo_files:
      label = nfo_path.parent.name or nfo_path.name
      try:
         release_id, group_id = read_nfo_mbids(nfo_path)
         if not (release_id or group_id):
            no_id += 1
            print(warn(f"{label}  (no musicbrainz id in album.nfo)"))
            continue

         # Check locally before touching the network
         missing = album_nfo_missing_fields(nfo_path)
         if not overwrite and not missing:
            skipped += 1
            print(ok(f"{label}  (already complete — skipped MusicBrainz lookup)"))
            continue

         if missing:
            print(info(f"{label}  (missing: {', '.join(missing)}) — looking up MusicBrainz"))

         if not release_id:
            release_id = resolve_release_from_group(group_id) or ""
         if not release_id:
            fail += 1
            print(err(f"{label}  (could not resolve a release from the release-group id)"))
            continue

         try:
            release = fetch_mb_release(release_id, raise_not_found=True)
         except MBNotFoundError:
            # The id in album.nfo no longer exists on MusicBrainz (the release was
            # deleted upstream). Search for the album and suggest the best match,
            # but never write it: runs are often unattended, and a wrong release
            # would write a wrong track list. Still a FAIL until the id is fixed.
            fail += 1
            print(err(f"{label}  (stale MusicBrainz id in album.nfo: {release_id} no longer exists)"))
            print(_suggest_release(nfo_path))
            continue
         if not release:
            fail += 1
            print(err(f"{label}  (MusicBrainz lookup failed)"))
            continue

         changes, new_text = enrich_album_nfo(nfo_path, release, overwrite=overwrite)
         if not changes:
            skipped += 1
            print(ok(f"{label}  (already complete)"))
            continue

         summary = ", ".join(changes)
         if dry_run:
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

   verb = "WOULD UPDATE" if dry_run else "UPDATED"
   banner(f"{updated} - {verb} / {skipped} - COMPLETE / {no_id} - NO ID / {fail} - FAIL")
   return 0 if fail == 0 else 1


# ---------------------------------------------------------------------------
# run_fix_tags: rewrite audio tags to match album.nfo
# ---------------------------------------------------------------------------

def run_fix_tags(opts: RunOptions) -> int:
   """
   Rewrite audio tags that don't match album.nfo.
   Write mode comes from opts.write_mode ("backup" or "direct").
   Comparison strictness follows --check-tags when it's also given (so a strict
   check after the fix finds nothing left to report); otherwise "loose".
   Returns 0 on success, 1 if any file failed or mismatches remain, 2 on bad input.
   """
   try:
      inputs = collect_audio_files(opts.paths, opts.include_subfolders)
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not inputs:
      print("[!] No supported audio files found.", file=sys.stderr)
      return 1

   write_mode = opts.write_mode  # "backup" or "direct"
   strictness = opts.check_tags or "loose"
   layout = opts.layout
   dry_run = opts.preview

   banner(f"PyLy --fix-tags ({write_mode}) [{strictness}] — {len(inputs)} file(s)")

   clean_count = 0
   problem_count = 0
   fixed_count = 0
   fail_count = 0

   for audio in inputs:
      try:
         album_nfo = find_album_nfo(audio)
         artist_nfo = find_artist_nfo(audio)
         tags = read_embedded_tags(audio)
         expected = expected_metadata(audio, layout, album_nfo, artist_nfo)
         actual = actual_metadata(tags)
         layout_exp = layout_metadata(audio, layout)
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

         changed, message = write_tags(
            audio, expected, diffs,
            mode=write_mode, dry_run=dry_run,
         )
         if changed:
            fixed_count += 1
            print(ok(f"  {message}"))
         else:
            print(info(f"  (skipped write: {message})"))

      except Exception as e:
         fail_count += 1
         print(err(f"{audio.name}: {e}"))

   verb = "WOULD FIX" if dry_run else "FIXED"
   banner(
      f"{clean_count} - OK / {problem_count} - MISMATCH / "
      f"{fixed_count} - {verb} / {fail_count} - FAIL"
   )

   # Non-zero exit when problems remain or anything failed
   unresolved = (problem_count - fixed_count) if write_mode else problem_count
   return 0 if (unresolved == 0 and fail_count == 0) else 1


# ---------------------------------------------------------------------------
# run_check_tags: report tag mismatches without writing
# ---------------------------------------------------------------------------

def run_check_tags(opts: RunOptions) -> int:
   """
   Report tags that don't match album.nfo; writes nothing.
   Strictness comes from opts.check_tags ("strict" or "loose").
   Returns 1 when any mismatch, else 0. Returns 2 on bad input.
   """
   try:
      inputs = collect_audio_files(opts.paths, opts.include_subfolders)
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not inputs:
      print("[!] No supported audio files found.", file=sys.stderr)
      return 1

   strictness = opts.check_tags or "strict"
   layout = opts.layout

   banner(f"PyLy --check-tags [{strictness}] — {len(inputs)} file(s)")

   clean_count = 0
   problem_count = 0
   fail_count = 0

   for audio in inputs:
      try:
         album_nfo = find_album_nfo(audio)
         artist_nfo = find_artist_nfo(audio)
         tags = read_embedded_tags(audio)
         expected = expected_metadata(audio, layout, album_nfo, artist_nfo)
         actual = actual_metadata(tags)
         layout_exp = layout_metadata(audio, layout)
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

      except Exception as e:
         fail_count += 1
         print(err(f"{audio.name}: {e}"))

   banner(f"{clean_count} - OK / {problem_count} - MISMATCH / {fail_count} - FAIL")

   # Non-zero exit when any mismatch or failure
   return 0 if (problem_count == 0 and fail_count == 0) else 1


# ---------------------------------------------------------------------------
# run_cover: embed cover art from local files or online sources
# ---------------------------------------------------------------------------

def run_cover(opts: RunOptions) -> int:
   """
   Embed cover art into audio files.
   - --use-cover only: use local folder image, never downloads.
   - --get-cover: download from source, fallback to folder image.
   - Both set: same as --get-cover.
   - --replace: replace existing embedded art.
   Returns 0 on success, 1 if any file failed, 2 on bad input.
   """
   try:
      inputs = collect_audio_files(opts.paths, opts.include_subfolders)
   except Exception as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   if not inputs:
      print("[!] No supported audio files found.", file=sys.stderr)
      return 1

   # Determine resolver configuration
   if opts.get_cover:
      # --get-cover takes precedence when both are set
      try:
         spec = split_source_key(opts.get_cover)
      except ValueError as e:
         print(f"[X] {e}", file=sys.stderr)
         return 2
      key = resolve_art_key(spec.provider, spec.key)
      resolver = ArtResolver(spec.provider, key, prefer="online", layout=opts.layout)
      source_desc = f"{spec.provider}" + (f"={spec.key}" if spec.key else "")
   elif opts.use_cover:
      # --use-cover only: local only, never downloads
      resolver = ArtResolver(None, None, prefer="local", layout=opts.layout)
      source_desc = "local"
   else:
      # Should not happen: caller ensures at least one is set
      print("[X] No cover source specified", file=sys.stderr)
      return 2

   replacing = " — replacing existing art" if opts.replace else ""
   banner(f"PyLy --cover ({source_desc}){replacing} — {len(inputs)} file(s)")

   embedded = has_art = no_art = unsupported = fail = 0
   warned_ogg = False
   write_mode = opts.write_mode
   dry_run = opts.preview

   try:
      for audio in inputs:
         try:
            if not can_embed(audio):
               unsupported += 1
               if cannot_hold_art(audio):
                  if not warned_ogg:
                     warned_ogg = True
                     print(warn("ffmpeg can't write cover art into .ogg/.opus files; skipping them"))
               else:
                  print(warn(f"{audio.name}  (cover art not supported for {audio.suffix} — skipped)"))
               continue

            # Skip files that already have art unless --replace
            if not opts.replace and has_embedded_art(audio):
               has_art += 1
               print(ok(f"{audio.name}  (has art — skipped)"))
               continue

            art = resolver.resolve(audio)
            if art is None:
               no_art += 1
               print(warn(f"{audio.name}  no art found"))
               continue

            changed, message = embed_cover(audio, art.image, mode=write_mode, dry_run=dry_run)
            if changed:
               embedded += 1
               verb = "would embed" if dry_run else "embedded"
               print(ok(f"{audio.name}  {verb} (source: {art.label})"))
            else:
               fail += 1
               print(err(f"{audio.name}: {message}"))

         except Exception as e:
            fail += 1
            print(err(f"{audio.name}: {e}"))
   finally:
      resolver.close()

   verb = "WOULD EMBED" if dry_run else "EMBEDDED"
   banner(
      f"{embedded} - {verb} / {has_art} - HAS ART / {no_art} - NO ART / "
      f"{unsupported} - UNSUPPORTED / {fail} - FAIL"
   )
   return 0 if fail == 0 else 1
