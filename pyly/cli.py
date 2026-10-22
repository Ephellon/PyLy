"""
PyLy 2 command-line interface.

Built entirely from the FLAGS table in options.py. No legacy flag names remain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .options import (
   FLAGS, FLAG_BY_NAME, RunOptions, ACTION_ORDER, DEFAULT_ACTION,
   DEFAULT_LYRIC_SOURCES,
)
from .settings import (
   load_settings, build_options, get_key, SettingsError, set_current_settings,
)
from .console_ui import set_color_enabled, banner, ok, err, warn, info
from .inputs import collect_audio_files


def _configure_stdio() -> None:
   """
   Force stdout/stderr to UTF-8 so PyLy's output survives being redirected
   to a file or pipe.
   """
   for stream in (sys.stdout, sys.stderr):
      try:
         stream.reconfigure(encoding="utf-8", errors="replace")
      except Exception:
         pass


def _build_parser() -> argparse.ArgumentParser:
   """Construct the ArgumentParser from FLAGS."""
   ap = argparse.ArgumentParser(
      prog="pyly",
      add_help=True,
      allow_abbrev=False,
   )

   # Positional paths
   ap.add_argument(
      "paths",
      nargs="*",
      type=Path,
      help="Audio files, directories, or patterns to process",
   )

   # Group flags by group for help output
   groups: dict[str, argparse._ArgumentGroup] = {}
   for flag in FLAGS:
      if flag.group and flag.group not in groups:
         groups[flag.group] = ap.add_argument_group(flag.group.title())

   for flag in FLAGS:
      group = groups.get(flag.group, ap)
      kwargs: dict[str, Any] = {
         "help": flag.help,
         "default": argparse.SUPPRESS,  # only typed flags appear in namespace
      }

      # Build option strings
      opt_strings = [f"--{flag.name}"]
      if flag.letter:
         opt_strings.append(f"-{flag.letter}")
      for alias in flag.aliases:
         opt_strings.append(f"--{alias}")

      if flag.value is None:  # switch
         kwargs["action"] = "store_true"
      elif flag.optional:
         kwargs["nargs"] = "?"
         kwargs["const"] = flag.const
         if flag.metavar:
            kwargs["metavar"] = flag.metavar
         # Type conversion for optional-value flags
         if flag.value == "int":
            kwargs["type"] = int
         elif flag.value == "float":
            kwargs["type"] = float
         elif isinstance(flag.value, tuple):
            kwargs["choices"] = flag.value
         else:
            kwargs["type"] = str
      else:  # required value
         if flag.value == "int":
            kwargs["type"] = int
         elif flag.value == "float":
            kwargs["type"] = float
         elif isinstance(flag.value, tuple):
            kwargs["choices"] = flag.value
         else:
            kwargs["type"] = str
         if flag.metavar:
            kwargs["metavar"] = flag.metavar

      group.add_argument(*opt_strings, dest=flag.dest, **kwargs)

   return ap


def _cli_values_from_namespace(ns: argparse.Namespace) -> dict[str, object]:
   """
   Extract only the flags that were actually provided on the command line.
   Because we used default=argparse.SUPPRESS, only typed flags are present.
   """
   return {k: v for k, v in vars(ns).items() if k != "paths"}


def _path_swallow_guard(ns: argparse.Namespace, ap: argparse.ArgumentParser) -> None:
   """
   After parsing, check if any optional-value flag got a value that looks like a path.
   If so, error with a helpful message.
   """
   for flag in FLAGS:
      if not flag.optional:
         continue
      dest = flag.dest
      if not hasattr(ns, dest):
         continue
      value = getattr(ns, dest)
      if value is None or value is flag.const:
         continue
      # Value was provided; check if it looks like a path
      v_str = str(value)
      # Exception: --get-cover value with '=' is a key, allowed
      if flag.name == "get-cover" and "=" in v_str:
         continue
      # Looks like a path if it exists, or contains path separators / drive letter
      p = Path(v_str)
      if p.exists() or "\\" in v_str or "/" in v_str or (len(v_str) >= 2 and v_str[1] == ":"):
         ap.error(
            f"'{v_str}' looks like a path, not a value for --{flag.name}; "
            f"put the path first: pyly \"{v_str}\" --{flag.name}"
         )


def _wire_options(opts: RunOptions) -> None:
   """Apply options that affect global state before running actions."""
   # Color
   color_val = opts.color
   if color_val == "on":
      set_color_enabled(True)
   elif color_val == "off":
      set_color_enabled(False)
   else:  # auto
      set_color_enabled(None)

   # Whisper Python
   if opts.whisper_python:
      from .whisper_offline import set_whisper_python
      set_whisper_python(opts.whisper_python)

   # Musixmatch token
   token = get_key("musixmatch")
   if token:
      from .lyrics_fetch import set_musixmatch_token
      set_musixmatch_token(token)


def _dispatch_actions(opts: RunOptions) -> int:
   """
   Run the requested actions in order. Returns the max exit code.
   Each action module is imported lazily.
   """
   exit_codes: list[int] = []

   actions = opts.actions()
   tags_done = False
   cover_done = False
   lyrics_done = False

   for action_name in actions:
      if action_name in ("get-info", "fix-tags", "check-tags"):
         if not tags_done:
            from .run_tags import run_get_info, run_fix_tags, run_check_tags
            tags_done = True
         if action_name == "get-info":
            exit_codes.append(run_get_info(opts))
         elif action_name == "fix-tags":
            exit_codes.append(run_fix_tags(opts))
         elif action_name == "check-tags":
            exit_codes.append(run_check_tags(opts))

      elif action_name in ("get-cover", "use-cover"):
         if not cover_done:
            from .run_tags import run_cover
            cover_done = True
            exit_codes.append(run_cover(opts))

      elif action_name in ("make-lyrics", "get-lyrics", "refresh-lyrics", "upgrade-lyrics"):
         if not lyrics_done:
            from .run_lyrics import run_lyrics
            lyrics_done = True
            exit_codes.append(run_lyrics(opts))

   return max(exit_codes) if exit_codes else 0


def main(argv: list[str] | None = None) -> int:
   _configure_stdio()

   ap = _build_parser()
   # Intermixed parsing lets paths sit on either side of flags:
   # pyly "<album 1>" --replace "<album 2>"
   ns = ap.parse_intermixed_args(argv)

   # --list-sources: print sources and exit (no path required)
   if getattr(ns, "list_sources", False):
      from .lyrics_fetch import list_providers
      from .cover_art import describe_art_providers

      print()
      print("  Available lyric providers:")
      print()
      for p in list_providers():
         note = "  [needs --allow-scraping]" if p.requires_scraping else ""
         print(f"    {p.name:<18} {p.description}{note}")
      print()
      print("  Cover-art providers (--get-cover):")
      print()
      for name, desc in describe_art_providers():
         print(f"    {name:<18} {desc}")
      print()
      return 0

   # Check this before "path required": when a flag swallowed the path, the
   # helpful message is "put the path first", not "no path given".
   _path_swallow_guard(ns, ap)

   # Paths required unless --list-sources
   paths = getattr(ns, "paths", [])
   if not paths:
      ap.error("the following arguments are required: paths")

   # Combine CLI values with settings
   cli_values = _cli_values_from_namespace(ns)
   profile = cli_values.get("profile")

   try:
      settings = load_settings()
      set_current_settings(settings)
      opts = build_options(cli_values, paths, settings, profile)
   except SettingsError as e:
      print(f"[X] {e}", file=sys.stderr)
      return 2

   # No action asked for means make-lyrics; record it so every module sees
   # the same choice the dispatcher makes.
   if opts.actions() == [DEFAULT_ACTION] and not opts.make_lyrics:
      opts.make_lyrics = True

   # Validate mutually exclusive refresh/upgrade (also checked in build_options)
   if opts.refresh_lyrics and opts.upgrade_lyrics:
      ap.error("Cannot combine --refresh-lyrics and --upgrade-lyrics; pick one")

   # Wire global state
   _wire_options(opts)

   # Dispatch actions
   return _dispatch_actions(opts)


if __name__ == "__main__":
   raise SystemExit(main())
