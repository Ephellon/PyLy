"""
Tests for pyly.pipeline: what happens when Whisper hears no words.

Whisper, the reducer and the tag reader are replaced with stand-ins, so these
tests run in a temporary folder with no audio, no GPU and no network.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyly import pipeline


class TestNoVocals(unittest.TestCase):
   def setUp(self):
      self.tmpdir = tempfile.TemporaryDirectory()
      self.audio = Path(self.tmpdir.name) / "01 Song.mp3"
      self.audio.write_bytes(b"dummy")

   def tearDown(self):
      self.tmpdir.cleanup()

   def _run(self, **kwargs):
      srt = self.audio.with_suffix(".srt")
      red = self.audio.with_suffix(".red.srt")

      def fake_transcribe(**_kw):
         srt.write_text("", encoding="utf-8")
         return srt

      def fake_reduce(**_kw):
         red.write_text("", encoding="utf-8")
         return red

      with patch("pyly.whisper_offline.transcribe_to_srt", side_effect=fake_transcribe), \
           patch("pyly.reduce_text.reduce_srt_file", side_effect=fake_reduce), \
           patch("pyly.srt_io.read_srt", return_value=[]), \
           patch("pyly.lyrics_fetch.build_lrc_metadata_headers", return_value=["[ar:Test]"]):
         return pipeline.run_pipeline(self.audio, **kwargs)

   def test_writes_marker_lrc_instead_of_failing(self):
      result = self._run()
      self.assertEqual(result["status"], "ok")
      self.assertEqual(result["reason"], "no vocals found")
      text = self.audio.with_suffix(".lrc").read_text(encoding="utf-8")
      self.assertIn("[ar:Test]", text)
      self.assertIn(pipeline.NO_VOCALS_HEADER, text)
      self.assertIn(pipeline.NO_VOCALS_LINE, text)
      # No [PyLy:<url>] line, so --upgrade-lyrics can still try sources later.
      self.assertNotIn("[PyLy:", text)

   def test_skip_header_still_writes_marker_line(self):
      self._run(lrc_header=False)
      text = self.audio.with_suffix(".lrc").read_text(encoding="utf-8")
      self.assertIn(pipeline.NO_VOCALS_LINE, text)
      self.assertNotIn(pipeline.NO_VOCALS_HEADER, text)

   def test_clean_up_removes_srt_files(self):
      self._run(clean=True)
      self.assertFalse(self.audio.with_suffix(".srt").exists())
      self.assertFalse(self.audio.with_suffix(".red.srt").exists())


if __name__ == "__main__":
   unittest.main()
