"""
Tests for pyly.mismatch_feed: reading FAFO's mismatches.jsonl and showing its
flags in --check-tags.

Works on files in a temporary folder; nothing goes online.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pyly.mismatch_feed import describe, is_stale, load_mismatch_feed, track_flags
from pyly.options import RunOptions
from pyly.run_tags import run_check_tags


def _iso(ts: float) -> str:
   return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


class FeedCase(unittest.TestCase):
   def setUp(self):
      self.tmp = tempfile.TemporaryDirectory()
      self.addCleanup(self.tmp.cleanup)
      self.album = Path(self.tmp.name) / "Queen" / "A Night at the Opera"
      self.album.mkdir(parents=True)
      self.car = self.album / "03 I'm in Love With My Car.mp3"
      self.rhapsody = self.album / "11 Bohemian Rhapsody.mp3"
      for f in (self.car, self.rhapsody):
         f.write_bytes(b"dummy")
      self.feed_path = Path(self.tmp.name) / "mismatches.jsonl"

   def write_feed(self, *records, extra_lines=()):
      lines = [json.dumps(r) for r in records] + list(extra_lines)
      self.feed_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

   def track(self, path, *mismatches, checked=None):
      return {"file": str(path), "status": "mismatch", "mismatches": list(mismatches),
              "checked": checked or _iso(time.time() + 60)}


class TestLoadFeed(FeedCase):
   def test_last_record_wins_and_junk_is_skipped(self):
      old = self.track(self.car, {"code": "DURATION", "library": 185, "service": 190, "delta": 5})
      new = self.track(self.car, {"code": "AUDIO", "service": "apple", "ber": 0.48})
      self.write_feed(old, {"warning": "Spotify skipped"}, new,
                      extra_lines=['{"file": "half written'])
      feed = load_mismatch_feed(self.feed_path)
      self.assertEqual(len(feed), 1)
      # Path case doesn't matter on Windows.
      upper = Path(str(self.car).upper()) if os.name == "nt" else self.car
      self.assertEqual(track_flags(feed, upper)[0].split(":")[0], "AUDIO")

   def test_album_record_keyed_by_folder(self):
      self.write_feed({"folder": str(self.album), "mismatches": [{"code": "TRACKLIST", "missing": ["01"]}]})
      self.assertEqual(len(load_mismatch_feed(self.feed_path).albums), 1)


class TestStale(FeedCase):
   def test_file_changed_after_check_is_stale(self):
      record = self.track(self.car, {"code": "AUDIO"}, checked=_iso(time.time() - 3600))
      self.assertTrue(is_stale(record, self.car))
      record["checked"] = _iso(time.time() + 3600)
      self.assertFalse(is_stale(record, self.car))

   def test_size_and_mtime_beat_checked_time(self):
      st = self.car.stat()
      record = self.track(self.car, {"code": "AUDIO"}, checked=_iso(time.time() - 3600))
      record.update(size=st.st_size, mtime=st.st_mtime)
      self.assertFalse(is_stale(record, self.car))
      record["size"] = st.st_size + 1
      self.assertTrue(is_stale(record, self.car))


class TestDescribe(unittest.TestCase):
   def test_audio_mentions_downloaded_lyrics(self):
      line = describe({"code": "AUDIO", "service": "apple", "ber": 0.485}, has_downloaded_lyrics=True)
      self.assertIn("Apple", line)
      self.assertIn("0.485", line)
      self.assertIn(".lrc", line)

   def test_small_duration_gap_leaves_lyrics_alone(self):
      self.assertNotIn(".lrc", describe({"code": "DURATION", "library": 355, "service": 357, "delta": 2},
                                        has_downloaded_lyrics=True))
      self.assertIn(".lrc", describe({"code": "DURATION", "library": 355, "service": 365, "delta": 10},
                                     has_downloaded_lyrics=True))

   def test_tracklist_names_missing_tracks(self):
      line = describe({"code": "TRACKLIST", "missing": ["01 Death on Two Legs", "02 Lazing on a Sunday Afternoon",
                                                        "04 You're My Best Friend", "05 '39"]})
      self.assertIn("missing 4: 01 Death on Two Legs", line)
      self.assertIn("+1 more", line)

   def test_unknown_code_still_shown(self):
      self.assertTrue(describe({"code": "NEW_THING", "x": 1}).startswith("NEW_THING:"))


class TestCheckTagsShowsFlags(FeedCase):
   def run_check(self, mismatch_file):
      opts = RunOptions(paths=[self.album], check_tags="strict", mismatch_file=mismatch_file)
      out = io.StringIO()
      with patch("pyly.run_tags.read_embedded_tags", return_value={}), \
           patch("pyly.run_tags.has_problems", return_value=False), \
           redirect_stdout(out):
         code = run_check_tags(opts)
      return code, out.getvalue()

   def test_flags_printed_but_exit_code_unchanged(self):
      self.write_feed(self.track(self.car, {"code": "AUDIO", "service": "apple", "ber": 0.48}),
                      {"folder": str(self.album), "mismatches": [{"code": "TRACKLIST", "missing": ["01"]}]})
      code, out = self.run_check(str(self.feed_path))
      self.assertEqual(code, 0)
      self.assertIn("AUDIO: sounds unlike the Apple preview", out)
      self.assertEqual(out.count("TRACKLIST"), 1)   # album flag once, not per track
      self.assertIn("1 - FAFO", out)

   def test_missing_feed_warns_and_carries_on(self):
      code, out = self.run_check(str(self.feed_path))   # never written
      self.assertEqual(code, 0)
      self.assertIn("FAFO mismatch file not read", out)


if __name__ == "__main__":
   unittest.main()
