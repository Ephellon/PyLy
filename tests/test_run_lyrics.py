from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from pyly.options import RunOptions
from pyly import run_lyrics


class TestRunLyrics(unittest.TestCase):
   def setUp(self):
      self.tmpdir = tempfile.TemporaryDirectory()
      self.root = Path(self.tmpdir.name)

   def tearDown(self):
      self.tmpdir.cleanup()

   def _make_audio(self, name: str) -> Path:
      p = self.root / name
      p.write_bytes(b"dummy")
      return p

   def _make_lrc(self, name: str, content: str = "") -> Path:
      p = self.root / name
      p.write_text(content, encoding="utf-8")
      return p

   # ------------------------------------------------------------------
   # Helpers to build opts
   # ------------------------------------------------------------------
   def _base_opts(self, **overrides) -> RunOptions:
      opts = RunOptions(paths=[self.root], include_subfolders=False)
      for k, v in overrides.items():
         setattr(opts, k, v)
      return opts

   # ------------------------------------------------------------------
   # Mode: make-lyrics (default)
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   def test_make_lyrics_default_calls_pipeline_with_primary_false(self, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.return_value = {"status": "ok", "lrc": "x.lrc"}

      audio = self._make_audio("track.mp3")
      opts = self._base_opts(make_lyrics=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      mock_pipeline.assert_called()
      call = mock_pipeline.call_args
      self.assertEqual(call.kwargs["fetch_keep_mode"], "primary")
      self.assertEqual(call.kwargs["fetch_only"], False)
      self.assertEqual(call.kwargs["overwrite"], False)

   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   def test_make_lyrics_with_keep_both_sets_alternate(self, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.return_value = {"status": "ok", "lrc": "x.lrc"}

      audio = self._make_audio("track.mp3")
      opts = self._base_opts(make_lyrics=True, keep_both=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      call = mock_pipeline.call_args
      self.assertEqual(call.kwargs["fetch_keep_mode"], "alternate")
      self.assertEqual(call.kwargs["fetch_only"], False)

   # ------------------------------------------------------------------
   # Mode: get-lyrics without make-lyrics
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   def test_get_lyrics_without_make_sets_fetch_only_true(self, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.return_value = {"status": "ok", "lrc": "x.lrc"}

      audio = self._make_audio("track.mp3")
      opts = self._base_opts(get_lyrics="musicbrainz", make_lyrics=False)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      call = mock_pipeline.call_args
      self.assertEqual(call.kwargs["fetch_keep_mode"], "primary")
      self.assertEqual(call.kwargs["fetch_only"], True)
      self.assertEqual(call.kwargs["overwrite"], False)

   # ------------------------------------------------------------------
   # Mode: get-lyrics with make-lyrics (same as make-lyrics)
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   def test_get_lyrics_with_make_lyrics_uses_make_settings(self, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.return_value = {"status": "ok", "lrc": "x.lrc"}

      audio = self._make_audio("track.mp3")
      opts = self._base_opts(get_lyrics="musicbrainz,musixmatch", make_lyrics=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      call = mock_pipeline.call_args
      self.assertEqual(call.kwargs["fetch_keep_mode"], "primary")
      self.assertEqual(call.kwargs["fetch_only"], False)

   # ------------------------------------------------------------------
   # Mode: upgrade-lyrics
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   @patch("pyly.run_lyrics.lyrics_fetch.read_pyly_url_from_lrc")
   def test_upgrade_lyrics_filters_audio_without_pyly_url(self, mock_read_url, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.return_value = {"status": "ok", "lrc": "x.lrc"}
      # First audio has .lrc with URL -> skip; second has .lrc without URL -> keep
      mock_read_url.side_effect = ["https://example.com/lrc", None]

      audio1 = self._make_audio("track1.mp3")
      self._make_lrc("track1.lrc", "[PyLy:https://example.com/lrc]\n[00:00.00]line")
      audio2 = self._make_audio("track2.mp3")
      self._make_lrc("track2.lrc", "[00:00.00]line")
      audio3 = self._make_audio("track3.mp3")  # no .lrc -> skip

      opts = self._base_opts(upgrade_lyrics=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      # Should only call pipeline for track2
      self.assertEqual(mock_pipeline.call_count, 1)
      call = mock_pipeline.call_args
      self.assertEqual(call.kwargs["audio_path"], audio2)
      self.assertEqual(call.kwargs["fetch_keep_mode"], "primary")
      self.assertEqual(call.kwargs["fetch_only"], True)
      self.assertEqual(call.kwargs["overwrite"], True)

   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   @patch("pyly.run_lyrics.lyrics_fetch.read_pyly_url_from_lrc")
   def test_upgrade_lyrics_no_candidates_returns_2(self, mock_read_url, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_read_url.return_value = "https://example.com/lrc"  # all have URL

      audio1 = self._make_audio("track1.mp3")
      self._make_lrc("track1.lrc", "[PyLy:https://example.com/lrc]\n[00:00.00]line")

      opts = self._base_opts(upgrade_lyrics=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)  # nothing to upgrade is not an error
      mock_pipeline.assert_not_called()

   # ------------------------------------------------------------------
   # Mode: refresh-lyrics
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.lyrics_fetch.read_pyly_url_from_lrc")
   @patch("pyly.run_lyrics.lyrics_fetch.fetch_by_url")
   @patch("pyly.run_lyrics.lyrics_fetch.build_lrc_metadata_headers")
   @patch("pyly.run_lyrics.write_lrc")
   def test_refresh_lyrics_saves_by_default(self, mock_write, mock_build, mock_fetch, mock_read_url):
      mock_read_url.return_value = "https://example.com/lrc"
      mock_fetch.return_value = MagicMock(synced_lrc_text="[00:00.00]refreshed")
      mock_build.return_value = ["[re:PyLy]"]

      lrc = self._make_lrc("track.lrc", "[PyLy:https://example.com/lrc]\n[00:00.00]old")
      opts = self._base_opts(refresh_lyrics=True, preview=False)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      mock_write.assert_called_once()
      args, kwargs = mock_write.call_args
      self.assertEqual(args[0], lrc)
      self.assertEqual(kwargs["overwrite"], True)

   @patch("pyly.run_lyrics.lyrics_fetch.read_pyly_url_from_lrc")
   @patch("pyly.run_lyrics.lyrics_fetch.fetch_by_url")
   @patch("pyly.run_lyrics.lyrics_fetch.build_lrc_metadata_headers")
   @patch("pyly.run_lyrics.write_lrc")
   def test_refresh_lyrics_preview_does_not_write(self, mock_write, mock_build, mock_fetch, mock_read_url):
      mock_read_url.return_value = "https://example.com/lrc"
      mock_fetch.return_value = MagicMock(synced_lrc_text="[00:00.00]refreshed")
      mock_build.return_value = ["[re:PyLy]"]

      lrc = self._make_lrc("track.lrc", "[PyLy:https://example.com/lrc]\n[00:00.00]old")
      opts = self._base_opts(refresh_lyrics=True, preview=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      mock_write.assert_not_called()

   @patch("pyly.run_lyrics.lyrics_fetch.read_pyly_url_from_lrc")
   @patch("pyly.run_lyrics.lyrics_fetch.fetch_by_url")
   def test_refresh_lyrics_no_url_tag_counts_as_no_url(self, mock_fetch, mock_read_url):
      mock_read_url.return_value = None

      lrc = self._make_lrc("track.lrc", "[00:00.00]no url")
      opts = self._base_opts(refresh_lyrics=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      mock_fetch.assert_not_called()

   # ------------------------------------------------------------------
   # Exit codes
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   def test_exit_code_1_on_pipeline_exception(self, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.side_effect = RuntimeError("boom")

      audio = self._make_audio("track.mp3")
      opts = self._base_opts(make_lyrics=True)

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 1)

   def test_exit_code_2_when_no_audio_files(self):
      opts = self._base_opts(make_lyrics=True)  # empty directory

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 2)

   # ------------------------------------------------------------------
   # Parameter mapping
   # ------------------------------------------------------------------
   @patch("pyly.run_lyrics.run_pipeline")
   @patch("pyly.run_lyrics.lyrics_fetch.parse_fetch_arg")
   def test_pipeline_receives_all_mapped_parameters(self, mock_parse, mock_pipeline):
      mock_parse.return_value = MagicMock(enabled=True)
      mock_pipeline.return_value = {"status": "ok", "lrc": "x.lrc"}

      audio = self._make_audio("track.mp3")
      opts = self._base_opts(
         make_lyrics=True,
         replace=True,
         preview=True,
         clean_up=True,
         write_log=True,
         model="small",
         language="en",
         device="cpu",
         use_lyrics="ref.txt",
         drop_extras=True,
         trust_lyrics=True,
         match_score=0.9,
         match_window=10,
         merge_limit=3,
         rescue_score=0.8,
         skip_rescue=True,
         skip_header=True,
         allow_scraping=True,
         layout="flat",
      )

      rc = run_lyrics.run_lyrics(opts)

      self.assertEqual(rc, 0)
      call = mock_pipeline.call_args
      kwargs = call.kwargs
      self.assertEqual(kwargs["overwrite"], True)
      self.assertEqual(kwargs["dry_run"], True)
      self.assertEqual(kwargs["clean"], True)
      self.assertEqual(kwargs["write_log"], True)
      self.assertEqual(kwargs["whisper_model"], "small")
      self.assertEqual(kwargs["language"], "en")
      self.assertEqual(kwargs["device"], "cpu")
      self.assertEqual(kwargs["base_lyrics_path"], Path("ref.txt"))
      self.assertEqual(kwargs["base_strict"], True)
      self.assertEqual(kwargs["truth_mode"], True)
      self.assertEqual(kwargs["base_threshold"], 0.9)
      self.assertEqual(kwargs["base_window"], 10)
      self.assertEqual(kwargs["base_max_merge"], 3)
      self.assertEqual(kwargs["base_diff_threshold"], 0.8)
      self.assertEqual(kwargs["base_rescue"], False)
      self.assertEqual(kwargs["lrc_header"], False)
      self.assertEqual(kwargs["fetch_keep_mode"], "primary")
      self.assertEqual(kwargs["layout"], "flat")
      self.assertEqual(kwargs["fetch_only"], False)


if __name__ == "__main__":
   unittest.main()
