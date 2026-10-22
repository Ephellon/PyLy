"""
Tests for pyly.settings module.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from pyly.settings import (
   settings_path, SettingsError, load_settings, build_options, get_key,
   current_settings, set_current_settings, SettingsFile,
)
from pyly.options import RunOptions, FLAGS, FLAG_BY_NAME


class TestSettingsPath(unittest.TestCase):
   def test_env_var(self):
      with patch.dict(os.environ, {"PYLY_CONFIG": "/custom/path/pyly.ini"}):
         self.assertEqual(settings_path(), Path("/custom/path/pyly.ini"))

   def test_default(self):
      with patch.dict(os.environ, {}, clear=True):
         with patch("pathlib.Path.home", return_value=Path("/home/user")):
            self.assertEqual(settings_path(), Path("/home/user/.pyly/pyly.ini"))


class TestLoadSettings(unittest.TestCase):
   def test_missing_file_returns_empty(self):
      with tempfile.TemporaryDirectory() as tmp:
         missing = Path(tmp) / "nonexistent.ini"
         s = load_settings(missing)
         self.assertIsInstance(s, SettingsFile)
         self.assertEqual(s.sections(), [])

   def test_empty_file(self):
      with tempfile.TemporaryDirectory() as tmp:
         f = Path(tmp) / "empty.ini"
         f.write_text("")
         s = load_settings(f)
         self.assertEqual(s.sections(), [])

   def test_sections_and_keys_case_sensitive(self):
      with tempfile.TemporaryDirectory() as tmp:
         f = Path(tmp) / "case.ini"
         f.write_text("[Defaults]\nModel=small\nmodel=tiny\n")
         s = load_settings(f)
         self.assertIn("Defaults", s.sections())
         opts = s.options("Defaults")
         self.assertIn("Model", opts)
         self.assertIn("model", opts)


class TestBuildOptions(unittest.TestCase):
   def setUp(self):
      # Clear module cache
      set_current_settings(None)

   def _make_settings(self, content: str) -> SettingsFile:
      with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
         f.write(content)
         f.flush()
         return load_settings(Path(f.name))

   def test_priority_cli_over_profile_over_defaults(self):
      # CLI: model=large, Profile: model=base, Defaults: model=small, Flag.default=medium
      settings = self._make_settings("""
[defaults]
model = small
[myprofile]
model = base
""")
      cli = {"model": "large"}
      opts = build_options(cli, [], settings, "myprofile")
      self.assertEqual(opts.model, "large")

   def test_profile_over_defaults(self):
      settings = self._make_settings("""
[defaults]
model = small
[myprofile]
model = base
""")
      cli = {}
      opts = build_options(cli, [], settings, "myprofile")
      self.assertEqual(opts.model, "base")

   def test_defaults_over_flag_default(self):
      settings = self._make_settings("""
[defaults]
model = small
""")
      cli = {}
      opts = build_options(cli, [], settings, None)
      self.assertEqual(opts.model, "small")  # Flag.default is "medium"

   def test_flag_default_when_nothing_set(self):
      settings = self._make_settings("")
      cli = {}
      opts = build_options(cli, [], settings, None)
      self.assertEqual(opts.model, "medium")

   def test_bool_conversion_variants(self):
      for val in ("yes", "true", "on", "1", "YES", "True", "ON"):
         settings = self._make_settings(f"""
[defaults]
replace = {val}
""")
         opts = build_options({}, [], settings, None)
         self.assertTrue(opts.replace, f"Failed for {val}")

      for val in ("no", "false", "off", "0", "NO", "False", "OFF"):
         settings = self._make_settings(f"""
[defaults]
replace = {val}
""")
         opts = build_options({}, [], settings, None)
         self.assertFalse(opts.replace, f"Failed for {val}")

   def test_bool_conversion_error(self):
      settings = self._make_settings("""
[defaults]
replace = maybe
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, None)
      self.assertIn("expected yes/no/true/false/on/off/1/0", str(cm.exception))

   def test_int_conversion(self):
      settings = self._make_settings("""
[defaults]
match-window = 20
""")
      opts = build_options({}, [], settings, None)
      self.assertEqual(opts.match_window, 20)

   def test_int_conversion_error(self):
      settings = self._make_settings("""
[defaults]
match-window = notanumber
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, None)
      self.assertIn("expected integer", str(cm.exception))

   def test_float_conversion(self):
      settings = self._make_settings("""
[defaults]
match-score = 0.9
""")
      opts = build_options({}, [], settings, None)
      self.assertEqual(opts.match_score, 0.9)

   def test_choice_conversion(self):
      settings = self._make_settings("""
[defaults]
model = large
""")
      opts = build_options({}, [], settings, None)
      self.assertEqual(opts.model, "large")

   def test_choice_conversion_error(self):
      settings = self._make_settings("""
[defaults]
model = huge
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, None)
      self.assertIn("expected one of", str(cm.exception))

   def test_optional_value_yes_means_const(self):
      settings = self._make_settings("""
[defaults]
get-lyrics = yes
""")
      opts = build_options({}, [], settings, None)
      # const for get-lyrics is "" (empty string = default sources)
      self.assertEqual(opts.get_lyrics, "")

   def test_optional_value_no_means_off(self):
      settings = self._make_settings("""
[defaults]
get-lyrics = no
""")
      opts = build_options({}, [], settings, None)
      self.assertIsNone(opts.get_lyrics)

   def test_optional_value_text(self):
      settings = self._make_settings("""
[defaults]
get-lyrics = musicbrainz,musixmatch
""")
      opts = build_options({}, [], settings, None)
      self.assertEqual(opts.get_lyrics, "musicbrainz,musixmatch")

   def test_unknown_key_error(self):
      settings = self._make_settings("""
[defaults]
unknown-flag = value
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, None)
      self.assertIn("unknown key", str(cm.exception))

   def test_unknown_profile_error(self):
      settings = self._make_settings("""
[defaults]
model = small
[other]
model = base
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, "missing")
      self.assertIn("Profile 'missing' not found", str(cm.exception))

   def test_profile_key_not_allowed_in_file(self):
      settings = self._make_settings("""
[defaults]
profile = something
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, None)
      self.assertIn("'profile' key is not allowed", str(cm.exception))

   def test_implied_make_lyrics_from_switches(self):
      settings = self._make_settings("""
[defaults]
keep-both = yes
""")
      opts = build_options({}, [], settings, None)
      self.assertTrue(opts.keep_both)
      self.assertTrue(opts.make_lyrics)

   def test_refresh_and_upgrade_mutually_exclusive(self):
      settings = self._make_settings("""
[defaults]
refresh-lyrics = yes
upgrade-lyrics = yes
""")
      with self.assertRaises(SettingsError) as cm:
         build_options({}, [], settings, None)
      self.assertIn("Cannot combine", str(cm.exception))

   def test_use_lyrics_implies_make_lyrics(self):
      settings = self._make_settings("""
[defaults]
use-lyrics = some.txt
""")
      opts = build_options({}, [], settings, None)
      self.assertEqual(opts.use_lyrics, "some.txt")
      self.assertTrue(opts.make_lyrics)


class TestGetKey(unittest.TestCase):
   def setUp(self):
      self._home = tempfile.TemporaryDirectory()
      self.addCleanup(self._home.cleanup)
      home_patch = patch("pathlib.Path.home", return_value=Path(self._home.name))
      home_patch.start()
      self.addCleanup(home_patch.stop)
      set_current_settings(None)

   def _make_settings(self, tmp_path: Path, content: str) -> SettingsFile:
      f = tmp_path / "settings.ini"
      f.write_text(content)
      return load_settings(f)

   def test_keys_section_wins(self):
      with tempfile.TemporaryDirectory() as tmp:
         tmp_path = Path(tmp)
         settings = self._make_settings(tmp_path, """
[keys]
musixmatch = from-keys-section
""")
         set_current_settings(settings)
         with patch.dict(os.environ, {"PYLY_MUSIXMATCH_KEY": "from-env"}):
            key_file = Path.home() / ".pyly" / "musixmatch-key"
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text("from-file")
            self.assertEqual(get_key("musixmatch"), "from-keys-section")

   def test_env_var_second(self):
      with tempfile.TemporaryDirectory() as tmp:
         tmp_path = Path(tmp)
         settings = self._make_settings(tmp_path, """
[defaults]
model = medium
""")
         set_current_settings(settings)
         with patch.dict(os.environ, {"PYLY_MUSIXMATCH_KEY": "from-env"}):
            key_file = Path.home() / ".pyly" / "musixmatch-key"
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text("from-file")
            self.assertEqual(get_key("musixmatch"), "from-env")

   def test_key_file_third(self):
      with tempfile.TemporaryDirectory() as tmp:
         tmp_path = Path(tmp)
         settings = self._make_settings(tmp_path, """
[defaults]
model = medium
""")
         set_current_settings(settings)
         with patch.dict(os.environ, {}, clear=True):
            key_file = Path.home() / ".pyly" / "musixmatch-key"
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text("from-file")
            self.assertEqual(get_key("musixmatch"), "from-file")

   def test_none_when_missing(self):
      with tempfile.TemporaryDirectory() as tmp:
         tmp_path = Path(tmp)
         settings = self._make_settings(tmp_path, """
[defaults]
model = medium
""")
         set_current_settings(settings)
         with patch.dict(os.environ, {}, clear=True):
            key_file = Path.home() / ".pyly" / "musixmatch-key"
            if key_file.exists():
               key_file.unlink()
            self.assertIsNone(get_key("musixmatch"))

   def test_dashes_to_underscores_in_env(self):
      with tempfile.TemporaryDirectory() as tmp:
         tmp_path = Path(tmp)
         settings = self._make_settings(tmp_path, "")
         set_current_settings(settings)
         with patch.dict(os.environ, {"PYLY_MY_SOURCE_KEY": "env-value"}):
            self.assertEqual(get_key("my-source"), "env-value")


class TestCurrentSettingsCache(unittest.TestCase):
   def setUp(self):
      set_current_settings(None)

   def test_caches_on_first_call(self):
      with tempfile.TemporaryDirectory() as tmp:
         f = Path(tmp) / "cache.ini"
         f.write_text("[defaults]\nmodel=tiny\n")
         with patch("pyly.settings.settings_path", return_value=f):
            s1 = current_settings()
            s2 = current_settings()
            self.assertIs(s1, s2)

   def test_set_current_settings_replaces_cache(self):
      with tempfile.TemporaryDirectory() as tmp:
         f1 = Path(tmp) / "a.ini"
         f1.write_text("[defaults]\nmodel=tiny\n")
         f2 = Path(tmp) / "b.ini"
         f2.write_text("[defaults]\nmodel=base\n")
         s1 = load_settings(f1)
         s2 = load_settings(f2)
         set_current_settings(s1)
         self.assertIs(current_settings(), s1)
         set_current_settings(s2)
         self.assertIs(current_settings(), s2)


if __name__ == "__main__":
   unittest.main()
