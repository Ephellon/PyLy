"""
Tests for pyly.lyrics_fetch: featured-artist titles and stale MusicBrainz ids.

Network calls are replaced with stand-ins; nothing here goes online.
"""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pyly import lyrics_fetch
from pyly.lyrics_fetch import FetchConfig, MBNotFoundError, strip_featuring


class TestStripFeaturing(unittest.TestCase):
   def test_removes_featured_credits(self):
      cases = {
         "Smaxk Or Die - feat. Playboi Carti": "Smaxk Or Die",
         "Smaxk Or Die (feat. Playboi Carti)": "Smaxk Or Die",
         "Smaxk Or Die [ft. Playboi Carti]": "Smaxk Or Die",
         "Smaxk Or Die featuring Playboi Carti": "Smaxk Or Die",
         "Song (feat. Guest) (Remastered 2011)": "Song (Remastered 2011)",
      }
      for title, bare in cases.items():
         with self.subTest(title=title):
            self.assertEqual(strip_featuring(title), bare)

   def test_leaves_other_titles_alone(self):
      for title in ("Bohemian Rhapsody", "I'm in Love With My Car", "Left Hand Free", "Feather"):
         with self.subTest(title=title):
            self.assertEqual(strip_featuring(title), title)


class TestFetchUsesBareTitle(unittest.TestCase):
   def test_structured_source_gets_title_without_feat(self):
      seen = {}

      def fake_fetch(query, *, config=None, log_fn=None, **_kw):
         seen["query"] = query
         seen["title"] = config.title
         return None

      tmp = tempfile.TemporaryDirectory()
      self.addCleanup(tmp.cleanup)
      audio = Path(tmp.name) / "Fatt Smaxk" / "Smaxk Or Die" / "01 Smaxk Or Die - feat. Playboi Carti.mp3"
      audio.parent.mkdir(parents=True)
      audio.write_bytes(b"dummy")

      provider = lyrics_fetch.PROVIDER_REGISTRY["musixmatch"]
      config = FetchConfig(True, provider="musixmatch", allow_scraping=True,
                           artist="Fatt Smaxk", title="Smaxk Or Die - feat. Playboi Carti")
      query = "Fatt Smaxk Smaxk Or Die - feat. Playboi Carti"
      with patch.object(provider, "fetch_fn", side_effect=fake_fetch), \
           patch.object(lyrics_fetch, "expand_query", return_value=query), \
           patch.object(lyrics_fetch, "_read_cache", return_value=None), \
           patch.object(lyrics_fetch, "_write_cache", create=True):
         lyrics_fetch._fetch_from_provider(config, "musixmatch", audio)

      self.assertEqual(seen["title"], "Smaxk Or Die")
      self.assertEqual(seen["query"], "Fatt Smaxk Smaxk Or Die")


class TestStaleReleaseId(unittest.TestCase):
   def _http_error(self, code):
      return urllib.error.HTTPError("https://musicbrainz.org", code, "err", {}, None)

   def test_404_raises_when_asked(self):
      with patch.object(lyrics_fetch, "_mb_get", side_effect=self._http_error(404)):
         with self.assertRaises(MBNotFoundError):
            lyrics_fetch.fetch_mb_release("308e8d04-35c2-4ce1-a5a0-325c4fefa63c", raise_not_found=True)

   def test_404_returns_none_by_default(self):
      with patch.object(lyrics_fetch, "_mb_get", side_effect=self._http_error(404)):
         self.assertIsNone(lyrics_fetch.fetch_mb_release("308e8d04-35c2-4ce1-a5a0-325c4fefa63c"))

   def test_other_errors_return_none(self):
      with patch.object(lyrics_fetch, "_mb_get", side_effect=self._http_error(500)):
         self.assertIsNone(lyrics_fetch.fetch_mb_release("x", raise_not_found=True))


class TestSearchRelease(unittest.TestCase):
   """search_mb_release picks a replacement for a stale id from a MusicBrainz search."""

   def _release(self, rid, title, artist, count, score=100, status="Official"):
      return {"id": rid, "title": title, "track-count": count, "score": score, "date": "1975-11-21",
              "status": status, "artist-credit": [{"name": artist}]}

   def test_rejects_release_with_fewer_tracks(self):
      # e.g. the single of the same name
      self.assertIsNone(self._search([self._release("single", "A Night at the Opera", "Queen", 1)]))

   def test_prefers_official_over_withdrawn(self):
      match = self._search([
         self._release("withdrawn", "A Night at the Opera", "Queen", 12, status="Withdrawn"),
         self._release("official", "A Night at the Opera", "Queen", 12, score=80),
      ])
      self.assertEqual(match.release_id, "official")

   def _search(self, releases, track_count=12):
      with patch.object(lyrics_fetch, "_mb_get", return_value={"releases": releases}):
         return lyrics_fetch.search_mb_release("Queen", "A Night at the Opera", track_count)

   def test_prefers_same_track_count(self):
      match = self._search([
         self._release("deluxe", "A Night at the Opera", "Queen", 24),
         self._release("standard", "A Night at the Opera", "Queen", 12, score=90),
      ])
      self.assertEqual(match.release_id, "standard")
      self.assertTrue(match.exact)
      self.assertEqual(match.url, "https://musicbrainz.org/release/standard")

   def test_rejects_other_artist_or_album(self):
      self.assertIsNone(self._search([
         self._release("a", "A Night at the Opera", "Blind Guardian", 12),
         self._release("b", "A Day at the Races", "Queen", 10),
      ]))

   def test_closest_match_is_not_exact(self):
      match = self._search([self._release("x", "A Night at the Opera", "Queen", 24)])
      self.assertEqual(match.release_id, "x")
      self.assertFalse(match.exact)

   def test_search_error_returns_none(self):
      with patch.object(lyrics_fetch, "_mb_get", side_effect=OSError("offline")):
         self.assertIsNone(lyrics_fetch.search_mb_release("Queen", "A Night at the Opera"))


if __name__ == "__main__":
   unittest.main()
