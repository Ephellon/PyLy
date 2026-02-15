from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .console_ui import info, warn
from .template_tokens import expand_template

import unicodedata


DEFAULT_PROVIDER = "lrclib"
DEFAULT_TEMPLATE = "{Artist Name} {Track Title}"
KNOWN_PROVIDERS = {DEFAULT_PROVIDER}
_LRC_TS_RX = re.compile(r"^\s*\[?\d{1,2}:\d{2}(?:\.\d+)?\]?\s*")


@dataclass(frozen=True)
class FetchConfig:
   enabled: bool
   provider: str = DEFAULT_PROVIDER
   template: str = DEFAULT_TEMPLATE


@dataclass(frozen=True)
class FetchedLyrics:
   synced_lrc_text: str | None
   plain_text_lines: list[str] | None
   provider: str
   query: str
   cache_hit: bool


def parse_fetch_arg(value: str | None) -> FetchConfig | None:
   if value is None:
      return None

   raw = value.strip()
   if ":" in raw:
      provider, template = raw.split(":", 1)
      provider = provider.strip().lower() or DEFAULT_PROVIDER
      template = template.strip() or DEFAULT_TEMPLATE
      return FetchConfig(True, provider=provider, template=template)

   if not raw:
      return FetchConfig(True, provider=DEFAULT_PROVIDER, template=DEFAULT_TEMPLATE)

   lowered = raw.lower()
   if lowered in KNOWN_PROVIDERS:
      return FetchConfig(True, provider=lowered, template=DEFAULT_TEMPLATE)

   return FetchConfig(True, provider=DEFAULT_PROVIDER, template=raw)


def expand_query(template: str, audio_path: Path, layout: str | None = None) -> str:
   return expand_template(template, audio_path, layout=layout)


def _repair_mojibake(s: str) -> str:
   if not s:
      return s
   # common signature of UTF-8 bytes mis-decoded as cp1252/latin1
   if "â" in s or "Ã" in s:
      for enc in ("cp1252", "latin-1"):
         try:
            fixed = s.encode(enc).decode("utf-8")
            if "â" not in fixed and "Ã" not in fixed:
               return unicodedata.normalize("NFC", fixed)
         except Exception:
            pass
   return unicodedata.normalize("NFC", s)


def fetch_lyrics_data(
   config: FetchConfig,
   audio_path: Path,
   log_fn=None,
   layout: str | None = None,
) -> FetchedLyrics | None:
   provider = (config.provider or DEFAULT_PROVIDER).lower()
   template = config.template or DEFAULT_TEMPLATE
   query = expand_query(template, audio_path, layout=layout)
   query = _repair_mojibake(query)

   if not query:
      _log_info(f"FETCH: skipped (empty query)", log_fn=log_fn)
      return None

   _log_info(f"FETCH: provider={provider} query={query}", log_fn=log_fn)

   if provider not in KNOWN_PROVIDERS:
      _log_warn(f"FETCH: unknown provider '{provider}'", log_fn=log_fn)
      return None

   cache_path = _cache_path(provider, query)
   cached = _read_cache(cache_path)
   if cached is not None:
      _log_info(f"FETCH: cache hit ({provider})", log_fn=log_fn)
      if not cached.synced_lrc_text and not cached.plain_text_lines:
         return None
      return FetchedLyrics(
         synced_lrc_text=cached.synced_lrc_text,
         plain_text_lines=cached.plain_text_lines,
         provider=provider,
         query=query,
         cache_hit=True,
      )

   _log_info(f"FETCH: cache miss ({provider})", log_fn=log_fn)

   try:
      if provider == "lrclib":
         fetched = _fetch_lrclib(query)
      else:
         fetched = None
   except Exception as exc:
      _log_warn(f"FETCH: failed ({provider}) {exc}", log_fn=log_fn)
      return None

   if not fetched:
      _log_warn(f"FETCH: no lyrics found ({provider})", log_fn=log_fn)
      return None

   if not fetched.synced_lrc_text and not fetched.plain_text_lines:
      _log_warn(f"FETCH: no lyrics found ({provider})", log_fn=log_fn)
      return None

   _write_cache(cache_path, fetched)
   return FetchedLyrics(
      synced_lrc_text=fetched.synced_lrc_text,
      plain_text_lines=fetched.plain_text_lines,
      provider=provider,
      query=query,
      cache_hit=False,
   )


def fetch_base_lyrics_lines(
   config: FetchConfig,
   audio_path: Path,
   log_fn=None,
   layout: str | None = None,
) -> list[str] | None:
   fetched = fetch_lyrics_data(config, audio_path, log_fn=log_fn, layout=layout)
   if not fetched:
      return None
   return fetched.plain_text_lines


def _fetch_lrclib(query: str) -> FetchedLyrics | None:
   base = "https://lrclib.net/api/search"
   url = f"{base}?q={urllib.parse.quote_plus(query)}"
   req = urllib.request.Request(url, headers={"User-Agent": "PyLy/1.0"})

   with urllib.request.urlopen(req, timeout=8) as resp:
      payload = resp.read().decode("utf-8", errors="replace")

   data = json.loads(payload)
   return _extract_lrclib_lyrics(data, query=query)


def _extract_lrclib_lyrics(data, query: str) -> FetchedLyrics | None:
   def _from_item(item: dict) -> FetchedLyrics | None:
      synced_text = _extract_synced_lrc_text(item)
      plain_text = item.get("plainLyrics") or item.get("lyrics")
      plain_lines = _text_to_lines(plain_text) if isinstance(plain_text, str) else None
      if synced_text and not plain_lines:
         plain_lines = _text_to_lines(synced_text)
      if not synced_text and not plain_lines:
         return None
      return FetchedLyrics(
         synced_lrc_text=synced_text,
         plain_text_lines=plain_lines,
         provider=DEFAULT_PROVIDER,
         query=query,
         cache_hit=False,
      )

   if isinstance(data, dict):
      return _from_item(data)

   if isinstance(data, list):
      for item in data:
         if not isinstance(item, dict):
            continue
         entry = _from_item(item)
         if entry and entry.synced_lrc_text:
            return entry
      for item in data:
         if not isinstance(item, dict):
            continue
         entry = _from_item(item)
         if entry:
            return entry
   return None


def _extract_synced_lrc_text(item: dict) -> str | None:
   synced = item.get("syncedLyrics")
   if isinstance(synced, str) and synced.strip():
      return synced

   synced_lines = item.get("syncedLines") or item.get("synced_lyrics")
   if isinstance(synced_lines, list):
      lines: list[str] = []
      for row in synced_lines:
         if not isinstance(row, dict):
            continue
         ts = _line_timestamp(row)
         text = str(row.get("text", "")).strip()
         if ts and text:
            lines.append(f"[{ts}] {text}")
      if lines:
         return "\n".join(lines)
   return None


def _line_timestamp(row: dict) -> str | None:
   if isinstance(row.get("timestamp"), str):
      ts = row["timestamp"].strip()
      if ts:
         return ts

   for key in ("time", "start", "startTime", "start_time"):
      val = row.get(key)
      if isinstance(val, (int, float)):
         return _seconds_to_lrc_timestamp(float(val))
      if isinstance(val, str):
         sval = val.strip()
         if not sval:
            continue
         if ":" in sval:
            return sval
         try:
            return _seconds_to_lrc_timestamp(float(sval))
         except ValueError:
            continue
   return None


def _seconds_to_lrc_timestamp(seconds: float) -> str:
   if seconds < 0:
      seconds = 0.0
   total_centis = int(round(seconds * 100.0))
   mins, rem = divmod(total_centis, 6000)
   secs, centis = divmod(rem, 100)
   return f"{mins:02d}:{secs:02d}.{centis:02d}"


def _text_to_lines(text: str | None) -> list[str]:
   if not text:
      return []
   lines: list[str] = []
   for raw in text.splitlines():
      s = raw.strip()
      if not s:
         continue
      s = _LRC_TS_RX.sub("", s).strip()
      if s:
         lines.append(s)
   return lines


def _cache_path(provider: str, query: str) -> Path:
   key = f"{provider}:{query}".encode("utf-8")
   digest = hashlib.sha256(key).hexdigest()
   cache_dir = Path(".pyly_cache")
   cache_dir.mkdir(parents=True, exist_ok=True)
   return cache_dir / f"{provider}-{digest}.json"


def _read_cache(path: Path) -> FetchedLyrics | None:
   try:
      raw = json.loads(path.read_text(encoding="utf-8"))
   except Exception:
      return None

   if isinstance(raw, dict) and isinstance(raw.get("lines"), list):
      # Backward-compatible cache schema.
      lines = [str(x) for x in raw["lines"] if str(x).strip()]
      return FetchedLyrics(
         synced_lrc_text=None,
         plain_text_lines=lines,
         provider=DEFAULT_PROVIDER,
         query="",
         cache_hit=True,
      )

   if isinstance(raw, dict):
      synced = raw.get("synced_lrc_text")
      plain = raw.get("plain_text_lines")
      synced_value = synced if isinstance(synced, str) and synced.strip() else None
      plain_value = [str(x) for x in plain if str(x).strip()] if isinstance(plain, list) else None
      if synced_value or plain_value:
         return FetchedLyrics(
            synced_lrc_text=synced_value,
            plain_text_lines=plain_value,
            provider=DEFAULT_PROVIDER,
            query="",
            cache_hit=True,
         )
   return None


def _write_cache(path: Path, fetched: FetchedLyrics) -> None:
   try:
      payload = {
         "synced_lrc_text": fetched.synced_lrc_text,
         "plain_text_lines": fetched.plain_text_lines,
      }
      path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
   except Exception:
      pass


def _log_info(message: str, log_fn=None) -> None:
   print(info(message))
   if log_fn:
      log_fn(message)


def _log_warn(message: str, log_fn=None) -> None:
   print(warn(message))
   if log_fn:
      log_fn(message)
