"""
Unit tests for pyly.run_tags.

Uses stdlib unittest only. Creates temp folders with empty *.mp3 files.
Patches the external dependencies to avoid network, ffmpeg, and real audio.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock, PropertyMock

# Ensure we can import pyly modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from pyly.run_tags import (
   run_get_info,
   run_fix_tags,
   run_check_tags,
   run_cover,
)
from pyly.options import RunOptions


class TestRunGetInfo(unittest.TestCase):
   """Tests for run_get_info."""

   def test_no_album_nfo_returns_zero(self):
      """No album.nfo found should print info line and return 0."""
      with TemporaryDirectory() as tmp:
         tmp_path = Path(tmp)
         # Create a dummy audio file but no album.nfo
         (tmp_path / "track.mp3").write_text("")

         opts = RunOptions(
            paths=[str(tmp_path)],
            get_info=True,
            include_subfolders=False,
            replace=False,
            preview=False,
         )

         result = run_get_info(opts)

         self.assertEqual(result, 0)
         # Should print the "nothing to update" message

   def test_stale_id_suggests_match_but_never_writes(self):
      """A 404 on the album.nfo id prints the best search match and leaves the file alone."""
      import io
      from contextlib import redirect_stdout
      from pyly.lyrics_fetch import MBNotFoundError, MBReleaseMatch

      with TemporaryDirectory() as tmp:
         album = Path(tmp) / "Queen" / "A Night at the Opera"
         album.mkdir(parents=True)
         for name in ("03 I'm in Love With My Car.mp3", "11 Bohemian Rhapsody.mp3"):
            (album / name).write_text("")
         nfo = album / "album.nfo"
         nfo_text = ("<album><title>A Night at the Opera</title><artist>Queen</artist>"
                     "<musicbrainzalbumid>stale-id</musicbrainzalbumid></album>")
         nfo.write_text(nfo_text, encoding="utf-8")

         match = MBReleaseMatch(release_id="new-id", title="A Night at the Opera", artist="Queen",
                                track_count=2, date="1975-11-21", exact=True)
         out = io.StringIO()
         with patch("pyly.run_tags.fetch_mb_release", side_effect=MBNotFoundError("stale-id")), \
              patch("pyly.run_tags.search_mb_release", return_value=match) as mock_search, \
              redirect_stdout(out):
            result = run_get_info(RunOptions(paths=[str(album)], get_info=True, replace=True))

         self.assertEqual(result, 1)
         # Track 11 exists, so the album has at least 11 tracks even with only 2 files here.
         mock_search.assert_called_once_with("Queen", "A Night at the Opera", 11, artist_id="")
         self.assertIn("stale-id", out.getvalue())
         self.assertIn("https://musicbrainz.org/release/new-id", out.getvalue())
         self.assertEqual(nfo.read_text(encoding="utf-8"), nfo_text)
         self.assertFalse((album / "album.nfo.bak").exists())


class TestRunFixTags(unittest.TestCase):
   """Tests for run_fix_tags."""

   def setUp(self):
      self.tmpdir = TemporaryDirectory()
      self.tmp_path = Path(self.tmpdir.name)
      (self.tmp_path / "track.mp3").write_text("")

   def tearDown(self):
      self.tmpdir.cleanup()

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.find_album_nfo")
   @patch("pyly.run_tags.find_artist_nfo")
   @patch("pyly.run_tags.read_embedded_tags")
   @patch("pyly.run_tags.expected_metadata")
   @patch("pyly.run_tags.actual_metadata")
   @patch("pyly.run_tags.layout_metadata")
   @patch("pyly.run_tags.compare_metadata")
   @patch("pyly.run_tags.has_problems")
   @patch("pyly.run_tags.write_tags")
   @patch("pyly.run_tags.banner")
   def test_write_mode_passed_through(
      self, mock_banner, mock_write_tags, mock_has_problems, mock_compare,
      mock_layout_meta, mock_actual_meta, mock_expected_meta, mock_read_tags,
      mock_find_artist, mock_find_album, mock_collect
   ):
      """write_mode from opts.write_mode should be passed to write_tags."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_find_album.return_value = None
      mock_find_artist.return_value = None
      mock_read_tags.return_value = {}
      mock_expected_meta.return_value = {"title": "Test"}
      mock_actual_meta.return_value = {}
      mock_layout_meta.return_value = {}
      mock_compare.return_value = [MagicMock(status="missing", field="title", expected="Test", actual="")]
      mock_has_problems.return_value = True
      mock_write_tags.return_value = (True, "updated")

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         fix_tags=True,
         include_subfolders=False,
         skip_backup=True,  # write_mode = "direct"
         preview=False,
         layout=None,
      )

      result = run_fix_tags(opts)

      self.assertEqual(result, 0)
      mock_write_tags.assert_called_once()
      args, kwargs = mock_write_tags.call_args
      self.assertEqual(kwargs.get("mode"), "direct")

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.find_album_nfo", return_value=None)
   @patch("pyly.run_tags.find_artist_nfo", return_value=None)
   @patch("pyly.run_tags.read_embedded_tags", return_value={})
   @patch("pyly.run_tags.expected_metadata", return_value={})
   @patch("pyly.run_tags.actual_metadata", return_value={})
   @patch("pyly.run_tags.layout_metadata", return_value={})
   @patch("pyly.run_tags.compare_metadata", return_value=[])
   @patch("pyly.run_tags.has_problems", return_value=False)
   @patch("pyly.run_tags.banner")
   def test_strictness_follows_check_tags(self, _banner, _has_problems, mock_compare, *_mocks):
      """--fix-tags compares in the --check-tags mode when given, else loose."""
      _mocks[-1].return_value = [self.tmp_path / "track.mp3"]  # collect_audio_files
      for check_tags, expected in ((None, "loose"), ("strict", "strict"), ("loose", "loose")):
         with self.subTest(check_tags=check_tags):
            mock_compare.reset_mock()
            opts = RunOptions(paths=[str(self.tmp_path)], fix_tags=True, check_tags=check_tags)
            run_fix_tags(opts)
            self.assertEqual(mock_compare.call_args.kwargs.get("mode"), expected)


class TestRunCheckTags(unittest.TestCase):
   """Tests for run_check_tags."""

   def setUp(self):
      self.tmpdir = TemporaryDirectory()
      self.tmp_path = Path(self.tmpdir.name)
      (self.tmp_path / "track.mp3").write_text("")

   def tearDown(self):
      self.tmpdir.cleanup()

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.find_album_nfo")
   @patch("pyly.run_tags.find_artist_nfo")
   @patch("pyly.run_tags.read_embedded_tags")
   @patch("pyly.run_tags.expected_metadata")
   @patch("pyly.run_tags.actual_metadata")
   @patch("pyly.run_tags.layout_metadata")
   @patch("pyly.run_tags.compare_metadata")
   @patch("pyly.run_tags.has_problems")
   @patch("pyly.run_tags.banner")
   def test_returns_one_on_mismatch(
      self, mock_banner, mock_has_problems, mock_compare, mock_layout_meta,
      mock_actual_meta, mock_expected_meta, mock_read_tags,
      mock_find_artist, mock_find_album, mock_collect
   ):
      """Should return 1 when any mismatch found."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_find_album.return_value = None
      mock_find_artist.return_value = None
      mock_read_tags.return_value = {}
      mock_expected_meta.return_value = {"title": "Test"}
      mock_actual_meta.return_value = {"title": "Wrong"}
      mock_layout_meta.return_value = {}
      # mismatch diff
      diff = MagicMock()
      diff.status = "mismatch"
      diff.field = "title"
      diff.expected = "Test"
      diff.actual = "Wrong"
      mock_compare.return_value = [diff]
      mock_has_problems.return_value = True

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         check_tags="strict",
         include_subfolders=False,
         layout=None,
      )

      result = run_check_tags(opts)

      self.assertEqual(result, 1)

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.find_album_nfo")
   @patch("pyly.run_tags.find_artist_nfo")
   @patch("pyly.run_tags.read_embedded_tags")
   @patch("pyly.run_tags.expected_metadata")
   @patch("pyly.run_tags.actual_metadata")
   @patch("pyly.run_tags.layout_metadata")
   @patch("pyly.run_tags.compare_metadata")
   @patch("pyly.run_tags.has_problems")
   @patch("pyly.run_tags.banner")
   def test_returns_zero_when_clean(
      self, mock_banner, mock_has_problems, mock_compare, mock_layout_meta,
      mock_actual_meta, mock_expected_meta, mock_read_tags,
      mock_find_artist, mock_find_album, mock_collect
   ):
      """Should return 0 when all tags match."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_find_album.return_value = None
      mock_find_artist.return_value = None
      mock_read_tags.return_value = {}
      mock_expected_meta.return_value = {"title": "Test"}
      mock_actual_meta.return_value = {"title": "Test"}
      mock_layout_meta.return_value = {}
      mock_compare.return_value = []
      mock_has_problems.return_value = False

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         check_tags="loose",
         include_subfolders=False,
         layout=None,
      )

      result = run_check_tags(opts)

      self.assertEqual(result, 0)


class TestRunCover(unittest.TestCase):
   """Tests for run_cover."""

   def setUp(self):
      self.tmpdir = TemporaryDirectory()
      self.tmp_path = Path(self.tmpdir.name)
      (self.tmp_path / "track.mp3").write_text("")

   def tearDown(self):
      self.tmpdir.cleanup()

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.can_embed")
   @patch("pyly.run_tags.has_embedded_art")
   @patch("pyly.run_tags.ArtResolver")
   @patch("pyly.run_tags.embed_cover")
   @patch("pyly.run_tags.banner")
   def test_use_cover_builds_resolver_with_prefer_local(
      self, mock_banner, mock_embed, mock_resolver_cls, mock_has_art, mock_can_embed, mock_collect
   ):
      """--use-cover should build ArtResolver with prefer='local' and provider=None."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_can_embed.return_value = True
      mock_has_art.return_value = False

      mock_resolver = MagicMock()
      mock_resolver.resolve.return_value = MagicMock(image=Path("cover.jpg"), label="local")
      mock_resolver_cls.return_value = mock_resolver
      mock_embed.return_value = (True, "embedded")

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=True,
         get_cover=None,
         include_subfolders=False,
         replace=False,
         preview=False,
         skip_backup=False,  # write_mode = "backup"
         layout=None,
      )

      result = run_cover(opts)

      self.assertEqual(result, 0)
      mock_resolver_cls.assert_called_once_with(None, None, prefer="local", layout=None)
      mock_resolver.close.assert_called_once()

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.can_embed")
   @patch("pyly.run_tags.has_embedded_art")
   @patch("pyly.run_tags.ArtResolver")
   @patch("pyly.run_tags.embed_cover")
   @patch("pyly.run_tags.split_source_key")
   @patch("pyly.run_tags.resolve_art_key")
   @patch("pyly.run_tags.banner")
   def test_get_cover_uses_prefer_online_with_parsed_source(
      self, mock_banner, mock_resolve_key, mock_split, mock_embed,
      mock_resolver_cls, mock_has_art, mock_can_embed, mock_collect
   ):
      """--get-cover should parse source and use prefer='online'."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_can_embed.return_value = True
      mock_has_art.return_value = False

      mock_spec = MagicMock()
      mock_spec.provider = "musicbrainz"
      mock_spec.key = "test-key"
      mock_split.return_value = mock_spec
      mock_resolve_key.return_value = "resolved-key"

      mock_resolver = MagicMock()
      mock_resolver.resolve.return_value = MagicMock(image=Path("cover.jpg"), label="online")
      mock_resolver_cls.return_value = mock_resolver
      mock_embed.return_value = (True, "embedded")

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=False,
         get_cover="musicbrainz=test-key",
         include_subfolders=False,
         replace=False,
         preview=False,
         skip_backup=False,
         layout=None,
      )

      result = run_cover(opts)

      self.assertEqual(result, 0)
      mock_split.assert_called_once_with("musicbrainz=test-key")
      mock_resolve_key.assert_called_once_with("musicbrainz", "test-key")
      mock_resolver_cls.assert_called_once_with("musicbrainz", "resolved-key", prefer="online", layout=None)
      mock_resolver.close.assert_called_once()

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.split_source_key")
   @patch("pyly.run_tags.banner")
   def test_bad_source_returns_two(self, mock_banner, mock_split, mock_collect):
      """Invalid --get-cover value should return 2 and print error."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_split.side_effect = ValueError("bad source format")

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=False,
         get_cover="invalid",
         include_subfolders=False,
         replace=False,
         preview=False,
         skip_backup=False,
         layout=None,
      )

      result = run_cover(opts)

      self.assertEqual(result, 2)

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.can_embed")
   @patch("pyly.run_tags.has_embedded_art")
   @patch("pyly.run_tags.ArtResolver")
   @patch("pyly.run_tags.embed_cover")
   @patch("pyly.run_tags.banner")
   def test_replace_controls_skipping(
      self, mock_banner, mock_embed, mock_resolver_cls, mock_has_art, mock_can_embed, mock_collect
   ):
      """--replace should control whether files with existing art are skipped."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_can_embed.return_value = True

      mock_resolver = MagicMock()
      mock_resolver.resolve.return_value = MagicMock(image=Path("cover.jpg"), label="local")
      mock_resolver_cls.return_value = mock_resolver
      mock_embed.return_value = (True, "embedded")

      # Without replace: has_embedded_art=True should skip
      mock_has_art.return_value = True
      opts_no_replace = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=True,
         get_cover=None,
         include_subfolders=False,
         replace=False,
         preview=False,
         skip_backup=False,
         layout=None,
      )
      result = run_cover(opts_no_replace)
      self.assertEqual(result, 0)
      # embed_cover should NOT be called because file has art and no replace
      mock_embed.assert_not_called()
      mock_has_art.reset_mock()
      mock_embed.reset_mock()

      # With replace: has_embedded_art=True should NOT skip
      mock_has_art.return_value = True
      opts_replace = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=True,
         get_cover=None,
         include_subfolders=False,
         replace=True,
         preview=False,
         skip_backup=False,
         layout=None,
      )
      result = run_cover(opts_replace)
      self.assertEqual(result, 0)
      mock_embed.assert_called_once()

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.can_embed")
   @patch("pyly.run_tags.has_embedded_art")
   @patch("pyly.run_tags.ArtResolver")
   @patch("pyly.run_tags.embed_cover")
   @patch("pyly.run_tags.banner")
   def test_write_mode_passed_to_embed_cover(
      self, mock_banner, mock_embed, mock_resolver_cls, mock_has_art, mock_can_embed, mock_collect
   ):
      """opts.write_mode should be passed to embed_cover."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_can_embed.return_value = True
      mock_has_art.return_value = False

      mock_resolver = MagicMock()
      mock_resolver.resolve.return_value = MagicMock(image=Path("cover.jpg"), label="local")
      mock_resolver_cls.return_value = mock_resolver
      mock_embed.return_value = (True, "embedded")

      # skip_backup=True -> write_mode="direct"
      opts = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=True,
         get_cover=None,
         include_subfolders=False,
         replace=False,
         preview=False,
         skip_backup=True,
         layout=None,
      )

      result = run_cover(opts)

      self.assertEqual(result, 0)
      mock_embed.assert_called_once()
      args, kwargs = mock_embed.call_args
      self.assertEqual(kwargs.get("mode"), "direct")

   @patch("pyly.run_tags.collect_audio_files")
   @patch("pyly.run_tags.can_embed")
   @patch("pyly.run_tags.has_embedded_art")
   @patch("pyly.run_tags.ArtResolver")
   @patch("pyly.run_tags.embed_cover")
   @patch("pyly.run_tags.banner")
   def test_both_use_cover_and_get_cover_uses_get_cover(
      self, mock_banner, mock_embed, mock_resolver_cls, mock_has_art, mock_can_embed, mock_collect
   ):
      """When both --use-cover and --get-cover are set, --get-cover wins."""
      mock_collect.return_value = [self.tmp_path / "track.mp3"]
      mock_can_embed.return_value = True
      mock_has_art.return_value = False

      mock_resolver = MagicMock()
      mock_resolver.resolve.return_value = MagicMock(image=Path("cover.jpg"), label="online")
      mock_resolver_cls.return_value = mock_resolver
      mock_embed.return_value = (True, "embedded")

      opts = RunOptions(
         paths=[str(self.tmp_path)],
         use_cover=True,
         get_cover="musicbrainz",
         include_subfolders=False,
         replace=False,
         preview=False,
         skip_backup=False,
         layout=None,
      )

      with patch("pyly.run_tags.split_source_key") as mock_split:
         mock_spec = MagicMock()
         mock_spec.provider = "musicbrainz"
         mock_spec.key = None
         mock_split.return_value = mock_spec
         with patch("pyly.run_tags.resolve_art_key") as mock_resolve_key:
            mock_resolve_key.return_value = None
            result = run_cover(opts)

      self.assertEqual(result, 0)
      # Should have used get_cover path (prefer="online")
      mock_resolver_cls.assert_called_once_with("musicbrainz", None, prefer="online", layout=None)


if __name__ == "__main__":
   unittest.main()
