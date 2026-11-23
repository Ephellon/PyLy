"""
Tests for pyly.acoustid: reading AcoustID answers, the per-file cache, and the
--fingerprints flags in --check-tags and --fix-tags.

Nothing goes online and fpcalc never runs: fingerprint() and lookup() are
patched. ~/.pyly is redirected to a temporary folder.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pyly.acoustid import Identifier, SoundMatch, _parse_results, fix_title, sound_flags
from pyly.options import RunOptions
from pyly.run_tags import run_check_tags, run_fix_tags

CAR = SoundMatch(0.97, "rec-car", "I'm in Love With My Car", "Queen")
RHAPSODY = SoundMatch(0.95, "rec-bo-rhap", "Bohemian Rhapsody", "Queen")


class TestParse(unittest.TestCase):
   def test_one_entry_per_recording_best_first(self):
      results = [
         {"score": 0.6, "recordings": [{"id": "a", "title": "A", "artists": [{"name": "Queen"}]}]},
         {"score": 0.9, "recordings": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]},
         {"score": 0.8},  # a fingerprint with no linked recording
      ]
      matches = _parse_results(results)
      self.assertEqual([(m.recording_id, m.score) for m in matches], [("a", 0.9), ("b", 0.9)])


class TestFlags(unittest.TestCase):
   def test_recording_id_match_is_quiet(self):
      self.assertEqual(sound_flags([RHAPSODY], "Bohemian Rhapsody", "rec-bo-rhap"), [])

   def test_wrong_recording_is_flagged(self):
      flags = sound_flags([CAR], "Bohemian Rhapsody", "rec-bo-rhap")
      self.assertEqual(len(flags), 1)
      self.assertIn("I'm in Love With My Car", flags[0])

   def test_title_compare_without_id(self):
      self.assertEqual(sound_flags([RHAPSODY], "Bohemian Rhapsody (2011 Remaster)"), [])
      self.assertTrue(sound_flags([CAR], "Bohemian Rhapsody"))

   def test_unknown_song_is_quiet(self):
      self.assertEqual(sound_flags([], "Bohemian Rhapsody"), [])

   def test_fix_title_needs_a_sure_match(self):
      self.assertEqual(fix_title([RHAPSODY]), "Bohemian Rhapsody")
      self.assertEqual(fix_title([SoundMatch(0.7, "x", "Maybe", "")]), "")
      self.assertEqual(fix_title(None), "")


class _Album(unittest.TestCase):
   """A temporary Queen album folder and a temporary home for ~/.pyly."""

   def setUp(self):
      self.tmp = tempfile.TemporaryDirectory()
      root = Path(self.tmp.name)
      self.home = root / "home"
      self.home.mkdir()
      self.album = root / "Queen" / "A Night at the Opera"
      self.album.mkdir(parents=True)
      self.song = self.album / "11 Bohemian Rhapsody.mp3"
      self.song.write_text("")
      patches = [
         patch("pathlib.Path.home", return_value=self.home),
         patch("pyly.acoustid.fpcalc_path", return_value="fpcalc"),
         patch("pyly.acoustid.fingerprint", return_value=(355, "FP")),
         patch("pyly.run_tags.get_key", return_value="test-key"),
      ]
      for p in patches:
         p.start()
         self.addCleanup(p.stop)

   def tearDown(self):
      self.tmp.cleanup()

   def run_quiet(self, func, opts):
      out = io.StringIO()
      with redirect_stdout(out):
         code = func(opts)
      return code, out.getvalue()


class TestIdentifierCache(_Album):
   def test_second_run_uses_cache_and_never_stores_key(self):
      with patch("pyly.acoustid.lookup", return_value=[RHAPSODY]) as lookup:
         first = Identifier("test-key")
         self.assertEqual(first.identify(self.song), [RHAPSODY])
         first.save()
         self.assertEqual(Identifier("test-key").identify(self.song), [RHAPSODY])
         self.assertEqual(lookup.call_count, 1)
      cache = (self.home / ".pyly" / "acoustid-cache.json").read_text(encoding="utf-8")
      self.assertNotIn("test-key", cache)

   def test_no_key_answers_from_cache_only(self):
      ears = Identifier(None)
      self.assertTrue(ears.problem)
      self.assertIsNone(ears.identify(self.song))


class TestCheckAndFix(_Album):
   def test_check_flags_wrong_song_without_changing_exit(self):
      opts = RunOptions(paths=[self.album], check_tags="loose", fingerprints=True)
      with patch("pyly.acoustid.lookup", return_value=[CAR]):
         code_on, out = self.run_quiet(run_check_tags, opts)
      self.assertIn("[AcoustID]", out)
      self.assertIn("1 - SOUND", out)
      opts.fingerprints = False
      code_off, _ = self.run_quiet(run_check_tags, opts)
      self.assertEqual(code_on, code_off)

   def test_fix_takes_sure_title_only_when_nfo_lacks_the_track(self):
      opts = RunOptions(paths=[self.album], fix_tags=True, preview=True, fingerprints=True)
      with patch("pyly.acoustid.lookup", return_value=[CAR]):
         _, out = self.run_quiet(run_fix_tags, opts)
      self.assertIn("sounds like \"I'm in Love With My Car\"", out)

      (self.album / "album.nfo").write_text(
         "<album><title>A Night at the Opera</title><artist>Queen</artist>"
         "<track><position>11</position><title>Bohemian Rhapsody</title></track></album>",
         encoding="utf-8")
      self.song.write_text(" ")  # changed file: not answered from the cache
      with patch("pyly.acoustid.lookup", return_value=[CAR]) as lookup:
         _, out = self.run_quiet(run_fix_tags, opts)
      self.assertNotIn("sounds like", out)
      lookup.assert_not_called()


if __name__ == "__main__":
   unittest.main()
