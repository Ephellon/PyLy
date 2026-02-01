from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .console_ui import info, warn


DEFAULT_PROVIDER = "lrclib"
DEFAULT_TEMPLATE = "{Artist Name} {Track Title}"
KNOWN_PROVIDERS = {DEFAULT_PROVIDER}
_SPACE_RX = re.compile(r"\s+")
_YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")
_TRACK_NUM_RX = re.compile(r"^\s*\d+\s*[-._ ]+\s*")
_LRC_TS_RX = re.compile(r"^\s*\[?\d{1,2}:\d{2}(?:\.\d+)?\]?\s*")


@dataclass(frozen=True)
class FetchConfig:
   enabled: bool
   provider: str = DEFAULT_PROVIDER
   template: str = DEFAULT_TEMPLATE


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


def expand_query(template: str, audio_path: Path) -> str:
   tokens = _extract_tokens(audio_path)
   result = template
   for token, value in tokens.items():
      result = result.replace(token, value)
   result = re.sub(r"\{[^}]+\}", "", result)
   result = _SPACE_RX.sub(" ", result).strip()
   return result


def fetch_base_lyrics_lines(config: FetchConfig, audio_path: Path, log_fn=None) -> list[str] | None:
   provider = (config.provider or DEFAULT_PROVIDER).lower()
   template = config.template or DEFAULT_TEMPLATE
   query = expand_query(template, audio_path)

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
      return cached

   _log_info(f"FETCH: cache miss ({provider})", log_fn=log_fn)

   try:
      if provider == "lrclib":
         text = _fetch_lrclib(query)
      else:
         text = None
   except Exception as exc:
      _log_warn(f"FETCH: failed ({provider}) {exc}", log_fn=log_fn)
      return None

   lines = _text_to_lines(text)
   if not lines:
      _log_warn(f"FETCH: no lyrics found ({provider})", log_fn=log_fn)
      return None

   _write_cache(cache_path, lines)
   return lines


def _fetch_lrclib(query: str) -> str | None:
   base = "https://lrclib.net/api/search"
   url = f"{base}?q={urllib.parse.quote_plus(query)}"
   req = urllib.request.Request(url, headers={"User-Agent": "PyLy/1.0"})

   with urllib.request.urlopen(req, timeout=8) as resp:
      payload = resp.read().decode("utf-8", errors="replace")

   data = json.loads(payload)
   return _extract_lrclib_text(data)


def _extract_lrclib_text(data) -> str | None:
   if isinstance(data, dict):
      for key in ("plainLyrics", "syncedLyrics", "lyrics"):
         if key in data and isinstance(data[key], str):
            return data[key]
      return None

   if isinstance(data, list):
      for item in data:
         if not isinstance(item, dict):
            continue
         plain = item.get("plainLyrics")
         if isinstance(plain, str) and plain.strip():
            return plain
      for item in data:
         if not isinstance(item, dict):
            continue
         synced = item.get("syncedLyrics")
         if isinstance(synced, str) and synced.strip():
            return synced
   return None


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


def _extract_tokens(audio_path: Path) -> dict[str, str]:
   artist = audio_path.parent.name.strip() if audio_path.parent else ""
   title = _clean_title(audio_path.stem)
   year = _guess_year(audio_path)
   return {
      "{Artist Name}": artist or "",
      "{Track Title}": title or "",
      "{Release Year}": year or "",
   }


def _clean_title(stem: str) -> str:
   s = stem.replace("_", " ").strip()
   s = _TRACK_NUM_RX.sub("", s)
   s = _SPACE_RX.sub(" ", s).strip()
   return s


def _guess_year(audio_path: Path) -> str:
   for candidate in (audio_path.parent.name, audio_path.name, audio_path.stem):
      match = _YEAR_RX.search(candidate or "")
      if match:
         return match.group(0)
   if audio_path.parent.parent:
      match = _YEAR_RX.search(audio_path.parent.parent.name or "")
      if match:
         return match.group(0)
   return ""


def _cache_path(provider: str, query: str) -> Path:
   key = f"{provider}:{query}".encode("utf-8")
   digest = hashlib.sha256(key).hexdigest()
   cache_dir = Path(".pyly_cache")
   cache_dir.mkdir(parents=True, exist_ok=True)
   return cache_dir / f"{provider}-{digest}.json"


def _read_cache(path: Path) -> list[str] | None:
   try:
      raw = json.loads(path.read_text(encoding="utf-8"))
   except Exception:
      return None
   if isinstance(raw, dict) and isinstance(raw.get("lines"), list):
      return [str(x) for x in raw["lines"] if str(x).strip()]
   return None


def _write_cache(path: Path, lines: list[str]) -> None:
   try:
      payload = {"lines": lines}
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
