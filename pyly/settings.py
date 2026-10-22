"""
Settings file handling for PyLy 2.

Reads ~/.pyly/pyly.ini (or $PYLY_CONFIG), merges with CLI values and defaults,
and provides API keys from [keys] section, environment, or per-source key files.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any

from .options import FLAGS, FLAG_BY_NAME, RunOptions, DEFAULT_LYRIC_SOURCES


# ---------------------------------------------------------------------------
# Settings file location
# ---------------------------------------------------------------------------

def settings_path() -> Path:
   """
   Return the path to the settings file.

   Priority: $PYLY_CONFIG > ~/.pyly/pyly.ini
   """
   env = os.environ.get("PYLY_CONFIG")
   if env:
      return Path(env).expanduser()
   return Path.home() / ".pyly" / "pyly.ini"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SettingsError(Exception):
   """
   Error while reading or interpreting the settings file.

   The message should name the file and, when known, the section and line.
   """
   pass


# ---------------------------------------------------------------------------
# Settings file type (raw ConfigParser with case-sensitive keys)
# ---------------------------------------------------------------------------

class SettingsFile:
   """
   Thin wrapper around configparser.ConfigParser with interpolation disabled
   and case-sensitive option names.
   """
   def __init__(self, path: Path):
      self.path = path
      self.parser = configparser.ConfigParser(interpolation=None)
      self.parser.optionxform = str  # preserve case
      if path.is_file():
         try:
            self.parser.read(path, encoding="utf-8")
         except configparser.Error as e:
            # Include line number if available
            msg = str(e)
            if hasattr(e, 'lineno') and e.lineno:
               msg = f"{path}:{e.lineno}: {e}"
            else:
               msg = f"{path}: {e}"
            raise SettingsError(msg) from e

   def sections(self) -> list[str]:
      """Return list of section names in the settings file."""
      return self.parser.sections()

   def has_section(self, name: str) -> bool:
      """Check if a section exists in the settings file."""
      return self.parser.has_section(name)

   def options(self, section: str) -> list[str]:
      """Return list of option names in a section."""
      return self.parser.options(section)

   def get(self, section: str, option: str, fallback: Any = None) -> str | None:
      """
      Get an option value from a section.

      Returns fallback if section or option doesn't exist.
      """
      try:
         return self.parser.get(section, option)
      except (configparser.NoSectionError, configparser.NoOptionError):
         return fallback

   def get_raw(self, section: str, option: str) -> str | None:
      """Get raw value without interpolation (same as get since interpolation=None)."""
      return self.get(section, option)


def load_settings(path: Path | None = None) -> SettingsFile:
   """
   Load the settings file. Missing file returns an empty SettingsFile (not an error).
   """
   if path is None:
      path = settings_path()
   return SettingsFile(path)


# ---------------------------------------------------------------------------
# Value conversion helpers
# ---------------------------------------------------------------------------

_BOOL_TRUE = {"yes", "true", "on", "1"}
_BOOL_FALSE = {"no", "false", "off", "0"}

def _convert_bool(value: str, section: str, key: str) -> bool:
   """Convert a string to boolean. Accepts yes/no, true/false, on/off, 1/0."""
   v = value.strip().lower()
   if v in _BOOL_TRUE:
      return True
   if v in _BOOL_FALSE:
      return False
   raise SettingsError(f"{section}:{key}: expected yes/no/true/false/on/off/1/0, got {value!r}")

def _convert_int(value: str, section: str, key: str) -> int:
   """Convert a string to integer. Raises SettingsError on failure."""
   try:
      return int(value.strip())
   except ValueError:
      raise SettingsError(f"{section}:{key}: expected integer, got {value!r}")

def _convert_float(value: str, section: str, key: str) -> float:
   """Convert a string to float. Raises SettingsError on failure."""
   try:
      return float(value.strip())
   except ValueError:
      raise SettingsError(f"{section}:{key}: expected number, got {value!r}")

def _convert_choice(value: str, choices: tuple[str, ...], section: str, key: str) -> str:
   """Convert a string to one of the allowed choices. Raises SettingsError if not in choices."""
   v = value.strip()
   if v not in choices:
      raise SettingsError(f"{section}:{key}: expected one of {', '.join(choices)}, got {value!r}")
   return v


def _convert_flag_value(flag: Flag, value: str, section: str, key: str) -> Any:
   """
   Convert a string from the settings file to the flag's type.
   For optional-value flags, handle yes/no FIRST, then choices.
   """
   v = value.strip()
   v_lower = v.lower()

   # Optional-value flags: yes/no handled before type conversion
   if flag.optional and v_lower in _BOOL_TRUE | _BOOL_FALSE:
      if v_lower in _BOOL_TRUE:
         return flag.const
      else:
         return None

   # Normal conversion
   if flag.value is None:  # switch
      return _convert_bool(value, section, key)
   if flag.value == "int":
      return _convert_int(value, section, key)
   if flag.value == "float":
      return _convert_float(value, section, key)
   if isinstance(flag.value, tuple):  # choice
      return _convert_choice(value, flag.value, section, key)
   # text (including optional-value flags with non-boolean text)
   return v


# ---------------------------------------------------------------------------
# Build RunOptions from CLI, settings file, and defaults
# ---------------------------------------------------------------------------

def build_options(
   cli_values: dict[str, object],
   paths: list[Path],
   settings: SettingsFile,
   profile: str | None,
) -> RunOptions:
   """
   Merge CLI values, profile section, [defaults] section, Flag defaults, and RunOptions defaults.

   Priority (highest first):
     1. cli_values (only flags actually typed on command line)
     2. [profile] section (if profile is set)
     3. [defaults] section
     4. Flag.default
     5. RunOptions default (dataclass field default)

   Raises SettingsError for unknown keys or unknown profile.
   """
   opts = RunOptions()
   opts.paths = paths

   # Determine which sections to read, in priority order
   sections_to_read: list[str] = []
   if profile:
      if not settings.has_section(profile):
         known = [s for s in settings.sections() if s not in ("defaults", "keys")]
         raise SettingsError(
            f"Profile {profile!r} not found in {settings.path}. "
            f"Available profiles: {', '.join(known) if known else '(none)'}"
         )
      sections_to_read.append(profile)
   if settings.has_section("defaults"):
      sections_to_read.append("defaults")

   # Collect all flag values from settings file (profile then defaults)
   file_values: dict[str, tuple[str, Any]] = {}  # key -> (section, converted_value)

   for section in sections_to_read:
      for key in settings.options(section):
         # Skip 'profile' key inside the file (not allowed)
         if key == "profile":
            raise SettingsError(f"[{section}]: 'profile' key is not allowed in the settings file")
         # Compare lowercased
         flag = FLAG_BY_NAME.get(key.lower())
         if flag is None:
            raise SettingsError(f"[{section}]: unknown key {key!r}")
         raw = settings.get(section, key)
         if raw is None:
            continue
         try:
            converted = _convert_flag_value(flag, raw, section, key)
         except SettingsError:
            raise
         except Exception as e:
            raise SettingsError(f"[{section}]:{key}: {e}") from e
         # Only set if not already set by a higher-priority section
         if key.lower() not in file_values:
            file_values[key.lower()] = (section, converted)

   # Now apply to RunOptions: CLI > file_values > Flag.default > RunOptions default
   for flag in FLAGS:
      dest = flag.dest
      value = None
      source = "default"

      # 1. CLI values (only flags actually typed)
      if dest in cli_values:
         value = cli_values[dest]
         source = "cli"
      # 2. Settings file (compare lowercased)
      elif flag.name.lower() in file_values:
         value = file_values[flag.name.lower()][1]
         source = f"file[{file_values[flag.name.lower()][0]}]"
      # 3. Flag.default
      elif flag.default is not None:
         value = flag.default
         source = "flag_default"
      # 4. RunOptions default (already set in opts)

      if value is not None:
         # Special handling for optional-value flags in settings file already done in _convert_flag_value
         # Assign to RunOptions
         setattr(opts, dest, value)

   # Handle implied make_lyrics from switches and use_lyrics
   if any(getattr(opts, f.dest) for f in FLAGS if f.kind == "switch" and f.group == "lyrics"):
      opts.make_lyrics = True
   if opts.use_lyrics is not None:
      opts.make_lyrics = True

   # Validate mutually exclusive refresh/upgrade
   if opts.refresh_lyrics and opts.upgrade_lyrics:
      raise SettingsError("Cannot combine --refresh-lyrics and --upgrade-lyrics; pick one")

   return opts


# ---------------------------------------------------------------------------
# API key lookup
# ---------------------------------------------------------------------------

def get_key(source: str, settings: SettingsFile | None = None) -> str | None:
   """
   Look up an API key for a source.

   Order:
     1. [keys] section entry `source`
     2. Environment variable PYLY_<SOURCE>_KEY (uppercase, dashes to underscores)
     3. File ~/.pyly/<source>-key (stripped text)

   Returns None when none found. Never prints or logs the key.
   """
   if settings is None:
      settings = current_settings()

   # 1. [keys] section
   if settings.has_section("keys"):
      val = settings.get("keys", source)
      if val is not None:
         return val.strip()

   # 2. Environment variable
   env_name = f"PYLY_{source.upper().replace('-', '_')}_KEY"
   env_val = os.environ.get(env_name)
   if env_val is not None:
      return env_val.strip()

   # 3. Per-source key file
   key_file = Path.home() / ".pyly" / f"{source}-key"
   if key_file.is_file():
      try:
         return key_file.read_text(encoding="utf-8").strip()
      except OSError:
         pass

   return None


# ---------------------------------------------------------------------------
# Module-level cache for current settings
# ---------------------------------------------------------------------------

_current_settings: SettingsFile | None = None

def current_settings() -> SettingsFile:
   """Return the cached settings file, loading on first call."""
   global _current_settings
   if _current_settings is None:
      _current_settings = load_settings()
   return _current_settings

def set_current_settings(settings: SettingsFile) -> None:
   """Replace the cached settings (used by tests)."""
   global _current_settings
   _current_settings = settings
