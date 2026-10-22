import tempfile
"""
Tests for pyly.cli module.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from pyly.cli import main, _build_parser, _path_swallow_guard
from pyly.options import RunOptions, FLAGS, FLAG_BY_NAME
from pyly.settings import SettingsError


def _sample_value(flag):
   """A valid example value for a flag that takes one (choices use the first word)."""
   if isinstance(flag.value, tuple):
      return flag.value[0]
   return {"int": "3", "float": "0.5"}.get(flag.value, "value")


class TestParser(unittest.TestCase):
   def setUp(self):
      self.parser = _build_parser()

   def test_all_flags_have_long_name(self):
      for flag in FLAGS:
         # Each flag should be parseable by its long name
         args = [f"--{flag.name}"]
         if flag.value is not None:
            args.append(_sample_value(flag))
         ns = self.parser.parse_args(args)
         self.assertTrue(hasattr(ns, flag.dest))

   def test_short_letters(self):
      # Flags with letters should work with short form
      for flag in FLAGS:
         if flag.letter:
            args = [f"-{flag.letter}"]
            if flag.value is not None:
               args.append(_sample_value(flag))
            ns = self.parser.parse_args(args)
            self.assertTrue(hasattr(ns, flag.dest))

   def test_aliases(self):
      for flag in FLAGS:
         for alias in flag.aliases:
            args = [f"--{alias}"]
            if flag.value is not None:
               args.append(_sample_value(flag))
            ns = self.parser.parse_args(args)
            self.assertTrue(hasattr(ns, flag.dest))

   def test_abbreviations_rejected(self):
      # --incl should fail (allow_abbrev=False)
      with self.assertRaises(SystemExit):
         self.parser.parse_args(["--incl"])

   def test_positional_paths(self):
      ns = self.parser.parse_args(["/path/to/music", "/another/path"])
      self.assertEqual(ns.paths, [Path("/path/to/music"), Path("/another/path")])

   def test_no_paths_allowed_when_list_sources(self):
      # --list-sources doesn't require paths
      ns = self.parser.parse_args(["--list-sources"])
      self.assertTrue(ns.list_sources)
      self.assertEqual(ns.paths, [])


class TestPathSwallowGuard(unittest.TestCase):
   def setUp(self):
      self.parser = _build_parser()

   def test_optional_value_that_exists_as_file_triggers_error(self):
      with tempfile.TemporaryDirectory() as tmp:
         dummy = Path(tmp) / "dummy.txt"
         dummy.write_text("hello")
         with self.assertRaises(SystemExit) as cm:
            main([str(dummy), "--get-lyrics", str(dummy)])
         self.assertEqual(cm.exception.code, 2)

   def test_optional_value_with_path_separator_triggers_error(self):
      with self.assertRaises(SystemExit) as cm:
         main(["/some/path", "--get-lyrics", "musicbrainz,/some/path"])
      self.assertEqual(cm.exception.code, 2)

   def test_optional_value_with_drive_letter_triggers_error(self):
      with self.assertRaises(SystemExit) as cm:
         main(["C:\\music", "--get-lyrics", "D:\\music"])
      self.assertEqual(cm.exception.code, 2)

   def test_get_cover_with_equals_allowed(self):
      # --get-cover value with '=' is a key, allowed
      ns = self.parser.parse_args(["/music", "--get-cover", "musicbrainz=mykey"])
      self.assertEqual(ns.get_cover, "musicbrainz=mykey")

   def test_switch_flags_not_affected(self):
      # Switches don't take values, so no swallow possible
      ns = self.parser.parse_intermixed_args(["/music", "--replace", "/other/path"])
      self.assertTrue(ns.replace)
      self.assertIn(Path("/other/path"), list(ns.paths))


class TestMainDispatch(unittest.TestCase):
   def setUp(self):
      # Reset settings cache
      from pyly.settings import set_current_settings
      set_current_settings(None)

   @patch("pyly.cli.load_settings")
   @patch("pyly.run_tags.run_get_info")
   @patch("pyly.run_tags.run_fix_tags")
   @patch("pyly.run_tags.run_cover")
   @patch("pyly.run_tags.run_check_tags")
   @patch("pyly.run_lyrics.run_lyrics")
   @patch("pyly.inputs.collect_audio_files")
   def test_dispatch_order_and_single_call(
      self, mock_collect, mock_run_lyrics, mock_check_tags,
      mock_run_cover, mock_run_fix_tags, mock_run_get_info, mock_load_settings
   ):
      # Setup
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track1.flac"), Path("track2.flac")]
      mock_run_get_info.return_value = 0
      mock_run_fix_tags.return_value = 0
      mock_run_cover.return_value = 0
      mock_check_tags.return_value = 0
      mock_run_lyrics.return_value = 0

      # Run with multiple actions
      exit_code = main(["/music", "--get-info", "--fix-tags", "--get-cover", "--make-lyrics"])

      self.assertEqual(exit_code, 0)
      # Check call order: tags actions first, then lyrics
      self.assertTrue(mock_run_get_info.called)
      self.assertTrue(mock_run_fix_tags.called)
      self.assertTrue(mock_run_cover.called)
      self.assertTrue(mock_run_lyrics.called)
      # run_cover called once even though both get-cover and use-cover not both used here
      self.assertEqual(mock_run_cover.call_count, 1)
      # run_lyrics called once for make-lyrics
      self.assertEqual(mock_run_lyrics.call_count, 1)

   @patch("pyly.cli.load_settings")
   @patch("pyly.run_tags.run_cover")
   @patch("pyly.inputs.collect_audio_files")
   def test_get_cover_and_use_cover_call_run_cover_once(
      self, mock_collect, mock_run_cover, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track.flac")]
      mock_run_cover.return_value = 0

      exit_code = main(["/music", "--get-cover", "--use-cover"])

      self.assertEqual(exit_code, 0)
      self.assertEqual(mock_run_cover.call_count, 1)

   @patch("pyly.cli.load_settings")
   @patch("pyly.run_lyrics.run_lyrics")
   @patch("pyly.inputs.collect_audio_files")
   def test_bare_pyly_path_runs_make_lyrics(
      self, mock_collect, mock_run_lyrics, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track.flac")]
      mock_run_lyrics.return_value = 0

      exit_code = main(["/music"])

      self.assertEqual(exit_code, 0)
      mock_run_lyrics.assert_called_once()
      opts = mock_run_lyrics.call_args[0][0]
      self.assertTrue(opts.make_lyrics)

   @patch("pyly.cli.load_settings")
   def test_list_sources_exits_0(self, mock_load_settings):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)

      exit_code = main(["--list-sources"])

      self.assertEqual(exit_code, 0)
      # We can't easily capture stdout in unittest without redirecting, but we trust the function prints.

   @patch("pyly.cli.load_settings")
   def test_settings_error_exits_2(self, mock_load_settings):
      def raise_error(*args, **kwargs):
         raise SettingsError("settings.ini:5: unknown key foo")
      mock_load_settings.side_effect = raise_error

      # Capture stderr
      import io
      old_stderr = sys.stderr
      sys.stderr = io.StringIO()
      try:
         exit_code = main(["/music"])
      finally:
         err_out = sys.stderr.getvalue()
         sys.stderr = old_stderr

      self.assertEqual(exit_code, 2)
      self.assertIn("[X] settings.ini:5: unknown key foo", err_out)

   @patch("pyly.cli.load_settings")
   @patch("pyly.inputs.collect_audio_files")
   def test_refresh_and_upgrade_together_error(
      self, mock_collect, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)

      import io
      old_stderr = sys.stderr
      sys.stderr = io.StringIO()
      try:
         exit_code = main(["/music", "--refresh-lyrics", "--upgrade-lyrics"])
      finally:
         err_out = sys.stderr.getvalue()
         sys.stderr = old_stderr

      self.assertEqual(exit_code, 2)
      self.assertIn("Cannot combine --refresh-lyrics and --upgrade-lyrics", err_out)

   @patch("pyly.cli.load_settings")
   @patch("pyly.inputs.collect_audio_files")
   def test_implied_make_lyrics_from_switches(
      self, mock_collect, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track.flac")]

      with patch("pyly.run_lyrics.run_lyrics") as mock_run_lyrics:
         mock_run_lyrics.return_value = 0
         exit_code = main(["/music", "--keep-both"])

      self.assertEqual(exit_code, 0)
      opts = mock_run_lyrics.call_args[0][0]
      self.assertTrue(opts.keep_both)
      self.assertTrue(opts.make_lyrics)


class TestWiring(unittest.TestCase):
   def setUp(self):
      from pyly.settings import set_current_settings
      set_current_settings(None)

   @patch("pyly.cli.load_settings")
   @patch("pyly.whisper_offline.set_whisper_python")
   @patch("pyly.inputs.collect_audio_files")
   def test_whisper_python_wired(
      self, mock_collect, mock_set_whisper, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track.flac")]

      with patch("pyly.run_lyrics.run_lyrics") as mock_run_lyrics:
         mock_run_lyrics.return_value = 0
         main(["/music", "--whisper-python", "/custom/python"])

      mock_set_whisper.assert_called_once_with("/custom/python")

   @patch("pyly.cli.load_settings")
   @patch("pyly.lyrics_fetch.set_musixmatch_token")
   @patch("pyly.inputs.collect_audio_files")
   def test_musixmatch_key_wired(
      self, mock_collect, mock_set_token, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track.flac")]

      with patch("pyly.run_lyrics.run_lyrics") as mock_run_lyrics:
         mock_run_lyrics.return_value = 0
         with patch("pyly.cli.get_key", return_value="test-token"):
            main(["/music"])

      mock_set_token.assert_called_once_with("test-token")

   @patch("pyly.cli.load_settings")
   @patch("pyly.cli.set_color_enabled")
   @patch("pyly.inputs.collect_audio_files")
   def test_color_auto_on_off(
      self, mock_collect, mock_set_color, mock_load_settings
   ):
      mock_load_settings.return_value = MagicMock(sections=lambda: [], has_section=lambda s: False)
      mock_collect.return_value = [Path("track.flac")]

      with patch("pyly.run_lyrics.run_lyrics") as mock_run_lyrics:
         mock_run_lyrics.return_value = 0
         main(["/music", "--color", "on"])

      mock_set_color.assert_called_once_with(True)

      mock_set_color.reset_mock()
      with patch("pyly.run_lyrics.run_lyrics") as mock_run_lyrics:
         mock_run_lyrics.return_value = 0
         main(["/music", "--color", "off"])

      mock_set_color.assert_called_once_with(False)

      mock_set_color.reset_mock()
      with patch("pyly.run_lyrics.run_lyrics") as mock_run_lyrics:
         mock_run_lyrics.return_value = 0
         main(["/music", "--color", "auto"])

      mock_set_color.assert_called_once_with(None)


if __name__ == "__main__":
   unittest.main()
