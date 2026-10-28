"""
Tests for pyly.file_metadata: <relatedrelease> blocks in album.nfo.

Works on album.nfo files in a temporary folder; nothing goes online.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pyly.file_metadata import enrich_album_nfo, parse_album_nfo, read_nfo_mbids
from pyly.lyrics_fetch import MBRelease, MBReleaseOption, MBTrack


NFO = """<album>
  <title>A Night at the Opera</title>
  <artist>Queen</artist>
  <year>1975</year>
  <musicbrainzalbumid>xw-id</musicbrainzalbumid>
</album>
"""


def _release(tracks=2):
   return MBRelease(
      release_id="xw-id", release_group_id="group-id", title="A Night at the Opera",
      album_artist="Queen", date="1975-11-21", year="1975", label="EMI", barcode="",
      tracks=[MBTrack(position=n, title=f"Song {n}", artist_credit="Queen", length_ms=180000,
                      recording_id=f"rec-{n}") for n in range(1, tracks + 1)],
   )


def _related():
   return [MBReleaseOption(release_id="us-id", country="US", country_count=40, date="1975-12-02",
                           status="Official", track_titles=("Song 1", "Song 2", "Song 3"))]


class TestRelatedReleases(unittest.TestCase):
   def setUp(self):
      self.tmp = tempfile.TemporaryDirectory()
      self.nfo = Path(self.tmp.name) / "album.nfo"
      self.nfo.write_text(NFO, encoding="utf-8")

   def tearDown(self):
      self.tmp.cleanup()

   def _enrich(self, **kw):
      changes, text = enrich_album_nfo(self.nfo, _release(), related=_related(), **kw)
      self.nfo.write_text(text, encoding="utf-8")
      return changes, text

   def test_writes_related_release_block(self):
      changes, text = self._enrich()
      self.assertIn("+1 related releases", changes)
      self.assertIn("<relatedrelease>", text)
      self.assertIn("<musicbrainzalbumid>us-id</musicbrainzalbumid>", text)
      self.assertIn("<countries>40</countries>", text)
      self.assertIn("<tracks>3</tracks>", text)
      # Related blocks come after the track list.
      self.assertLess(text.rindex("</track>"), text.index("<relatedrelease>"))

   def test_rerun_with_replace_changes_nothing(self):
      self._enrich(overwrite=True)
      changes, _ = self._enrich(overwrite=True)
      self.assertEqual(changes, [])

   def test_album_fields_ignore_related_blocks(self):
      # Put a related block first, so a careless reader would pick its id.
      self.nfo.write_text(NFO.replace("<album>\n", "<album>\n  <relatedrelease><musicbrainzalbumid>us-id"
                                      "</musicbrainzalbumid><date>1999</date></relatedrelease>\n"),
                          encoding="utf-8")
      self.assertEqual(read_nfo_mbids(self.nfo)[0], "xw-id")
      self.assertEqual(parse_album_nfo(self.nfo).year, "1975")


if __name__ == "__main__":
   unittest.main()
