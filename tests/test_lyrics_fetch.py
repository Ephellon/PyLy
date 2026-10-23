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


if __name__ == "__main__":
   unittest.main()
