from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_SPACE_RX = re.compile(r"\s+")
_YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")
_TRACK_NUM_RX = re.compile(r"^\s*(\d+)\s*[-._ ]+\s*(.+)$")
_TRACK_SIMPLE_RX = re.compile(r"^\s*(\d+)\s+(.+)$")
_TOKEN_RX = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True)
class PathGuess:
   artist: str = ""
   album: str = ""
   title: str = ""
   track_number: int | None = None
   disc_number: int | None = None
   year: str = ""


@dataclass(frozen=True)
class MediaInfo:
   audio_codec: str = ""
   audio_channels: str = ""
   audio_bit_rate: str = ""
   audio_bits_per_sample: str = ""
   audio_sample_rate: str = ""
   container_format: str = ""


@dataclass
class TokenContext:
   audio_path: Path
   layout: str | None
   tags: dict[str, str]
   path_guess: PathGuess
   media_info: MediaInfo
   cache: dict[str, str] = field(default_factory=dict)


def expand_template(template: str, audio_path: Path, layout: str | None = None) -> str:
   ctx = _build_context(audio_path, layout)

   def _replace(match: re.Match) -> str:
      raw = match.group(1).strip()
      resolver = TOKEN_REGISTRY.get(raw.lower())
      if not resolver:
         return ""
      return resolver(ctx)

   result = _TOKEN_RX.sub(_replace, template)
   result = _SPACE_RX.sub(" ", result).strip()
   return result


def _build_context(audio_path: Path, layout: str | None) -> TokenContext:
   tags, media_info = _probe_metadata(audio_path)
   path_guess = _infer_path_guess(audio_path, layout)
   return TokenContext(
      audio_path=audio_path,
      layout=layout,
      tags=tags,
      path_guess=path_guess,
      media_info=media_info,
   )


def _probe_metadata(audio_path: Path) -> tuple[dict[str, str], MediaInfo]:
   data = _run_ffprobe(audio_path)
   tags = _extract_tags(data)
   media_info = _extract_media_info(data)
   return tags, media_info


def _run_ffprobe(audio_path: Path) -> dict:
   if not shutil.which("ffprobe"):
      return {}
   cmd = [
      "ffprobe",
      "-v",
      "quiet",
      "-print_format",
      "json",
      "-show_format",
      "-show_streams",
      str(audio_path),
   ]
   try:
      proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
   except Exception:
      return {}
   if proc.returncode != 0:
      return {}
   try:
      return json.loads(proc.stdout or "{}")
   except Exception:
      return {}


def _extract_tags(data: dict) -> dict[str, str]:
   tags: dict[str, str] = {}
   format_tags = ((data.get("format") or {}).get("tags") or {}) if isinstance(data, dict) else {}
   for key, value in format_tags.items():
      if isinstance(value, (str, int, float)):
         tags[str(key).lower()] = str(value).strip()
   for stream in data.get("streams", []) if isinstance(data, dict) else []:
      if not isinstance(stream, dict):
         continue
      stream_tags = stream.get("tags") or {}
      for key, value in stream_tags.items():
         if isinstance(value, (str, int, float)):
            tags.setdefault(str(key).lower(), str(value).strip())
   return tags


def _extract_media_info(data: dict) -> MediaInfo:
   if not isinstance(data, dict):
      return MediaInfo()
   fmt = data.get("format") or {}
   streams = data.get("streams") or []
   audio_stream = None
   for stream in streams:
      if isinstance(stream, dict) and stream.get("codec_type") == "audio":
         audio_stream = stream
         break
   if not isinstance(audio_stream, dict):
      audio_stream = {}
   return MediaInfo(
      audio_codec=str(audio_stream.get("codec_name") or "").strip(),
      audio_channels=str(audio_stream.get("channels") or "").strip(),
      audio_bit_rate=str(audio_stream.get("bit_rate") or "").strip(),
      audio_bits_per_sample=str(audio_stream.get("bits_per_sample") or "").strip(),
      audio_sample_rate=str(audio_stream.get("sample_rate") or "").strip(),
      container_format=str(fmt.get("format_name") or "").strip(),
   )


def _infer_path_guess(audio_path: Path, layout: str | None) -> PathGuess:
   layout_key = (layout or "").strip().lower() or "default"
   stem = audio_path.stem
   if layout_key == "flat":
      title = _clean_title(stem)
      return PathGuess(title=title or "")

   parent = audio_path.parent
   grandparent = parent.parent if parent else None
   artist = parent.name.strip() if parent else ""
   album = grandparent.name.strip() if grandparent else ""
   track_num, title = _parse_track_from_stem(stem)
   year = _guess_year(audio_path)

   if layout_key == "lidarr":
      artist = grandparent.name.strip() if grandparent else artist
      album = parent.name.strip() if parent else album
      return PathGuess(
         artist=artist or "",
         album=album or "",
         title=title or _clean_title(stem),
         track_number=track_num,
         disc_number=None,
         year=year or "",
      )

   if layout_key == "plex":
      artist = grandparent.name.strip() if grandparent else artist
      album = parent.name.strip() if parent else album
      return PathGuess(
         artist=artist or "",
         album=album or "",
         title=title or _clean_title(stem),
         track_number=track_num,
         disc_number=None,
         year=year or "",
      )

   return PathGuess(
      artist=artist or "",
      album=album or "",
      title=title or _clean_title(stem),
      track_number=track_num,
      disc_number=None,
      year=year or "",
   )


def _parse_track_from_stem(stem: str) -> tuple[int | None, str]:
   if not stem:
      return None, ""
   match = _TRACK_NUM_RX.match(stem)
   if match:
      return _safe_int(match.group(1)), match.group(2).strip()
   match = _TRACK_SIMPLE_RX.match(stem)
   if match:
      return _safe_int(match.group(1)), match.group(2).strip()
   return None, _clean_title(stem)


def _safe_int(value: str | None) -> int | None:
   if not value:
      return None
   try:
      return int(value)
   except Exception:
      return None


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


def _get_tag(tags: dict[str, str], *keys: str) -> str:
   for key in keys:
      val = tags.get(key.lower())
      if val:
         return val.strip()
   return ""


def _parse_track_number(value: str) -> int | None:
   if not value:
      return None
   match = re.match(r"\s*(\d+)", value)
   if match:
      return _safe_int(match.group(1))
   return None


def _clean_title(stem: str) -> str:
   s = stem.replace("_", " ").strip()
   match = _TRACK_NUM_RX.match(s)
   if match:
      s = match.group(2)
   s = _SPACE_RX.sub(" ", s).strip()
   return s


def _clean_text(value: str) -> str:
   if not value:
      return ""
   s = value.replace("_", " ")
   s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
   s = _SPACE_RX.sub(" ", s).strip()
   return s


def _apply_name_the(value: str) -> str:
   if not value:
      return ""
   stripped = value.strip()
   if stripped.lower().startswith("the "):
      return stripped[4:].strip() + ", The"
   return stripped


def _first_character(value: str) -> str:
   if not value:
      return ""
   for ch in value.strip():
      if ch.strip():
         return ch.upper()
   return ""


def _resolve_artist_name(ctx: TokenContext) -> str:
   return _get_tag(ctx.tags, "albumartist", "artist") or ctx.path_guess.artist or ctx.audio_path.parent.name


def _resolve_track_artist_name(ctx: TokenContext) -> str:
   return _get_tag(ctx.tags, "artist") or _resolve_artist_name(ctx)


def _resolve_album_title(ctx: TokenContext) -> str:
   return _get_tag(ctx.tags, "album") or ctx.path_guess.album


def _resolve_track_title(ctx: TokenContext) -> str:
   return _get_tag(ctx.tags, "title") or ctx.path_guess.title or _clean_title(ctx.audio_path.stem)


def _resolve_track_number(ctx: TokenContext) -> int | None:
   tag_value = _get_tag(ctx.tags, "track", "tracknumber")
   return _parse_track_number(tag_value) or ctx.path_guess.track_number


def _resolve_disc_number(ctx: TokenContext) -> int | None:
   tag_value = _get_tag(ctx.tags, "disc", "discnumber")
   return _parse_track_number(tag_value) or ctx.path_guess.disc_number


def _resolve_year(ctx: TokenContext) -> str:
   raw = _get_tag(ctx.tags, "date", "year")
   if raw:
      match = _YEAR_RX.search(raw)
      if match:
         return match.group(0)
   return ctx.path_guess.year or _guess_year(ctx.audio_path)


def _format_number(value: int | None, width: int) -> str:
   if value is None:
      return ""
   if width <= 1:
      return str(value)
   return f"{value:0{width}d}"


def _quality_title(ctx: TokenContext) -> str:
   codec = ctx.media_info.audio_codec
   if codec:
      return codec.upper()
   ext = ctx.audio_path.suffix.lstrip(".")
   return ext.upper() if ext else ""


def _quality_full(ctx: TokenContext) -> str:
   parts: list[str] = []
   codec = ctx.media_info.audio_codec
   if codec:
      parts.append(codec.upper())
   if ctx.media_info.audio_bits_per_sample:
      parts.append(f"{ctx.media_info.audio_bits_per_sample}bit")
   if ctx.media_info.audio_sample_rate:
      try:
         rate = int(ctx.media_info.audio_sample_rate)
         parts.append(f"{rate / 1000:g}kHz")
      except Exception:
         pass
   if ctx.media_info.audio_bit_rate:
      try:
         rate = int(ctx.media_info.audio_bit_rate)
         parts.append(f"{rate // 1000}kbps")
      except Exception:
         pass
   return " ".join(parts).strip()


def _media_info_audio_channels(ctx: TokenContext) -> str:
   return ctx.media_info.audio_channels


def _media_info_audio_codec(ctx: TokenContext) -> str:
   return ctx.media_info.audio_codec


def _media_info_audio_bitrate(ctx: TokenContext) -> str:
   return ctx.media_info.audio_bit_rate


def _media_info_audio_bits_per_sample(ctx: TokenContext) -> str:
   return ctx.media_info.audio_bits_per_sample


def _media_info_audio_sample_rate(ctx: TokenContext) -> str:
   return ctx.media_info.audio_sample_rate


def _original_filename(ctx: TokenContext) -> str:
   return ctx.audio_path.stem


def _original_title(ctx: TokenContext) -> str:
   original = _get_tag(ctx.tags, "originaltitle")
   if original:
      return original
   parent = ctx.audio_path.parent.name if ctx.audio_path.parent else ""
   if parent:
      return f"{parent} - {ctx.audio_path.stem}"
   return ctx.audio_path.stem


def _medium_name(ctx: TokenContext) -> str:
   tag_val = _get_tag(ctx.tags, "disc", "discnumber", "medium")
   disc = _parse_track_number(tag_val) or _resolve_disc_number(ctx)
   if disc is None:
      return ""
   return f"Disc {disc}"


def _medium_format(ctx: TokenContext) -> str:
   return _get_tag(ctx.tags, "media", "format") or ctx.media_info.container_format


TOKEN_REGISTRY = {
   "artist name": _resolve_artist_name,
   "artist cleanname": lambda ctx: _clean_text(_resolve_artist_name(ctx)),
   "artist namethe": lambda ctx: _apply_name_the(_resolve_artist_name(ctx)),
   "artist cleannamethe": lambda ctx: _apply_name_the(_clean_text(_resolve_artist_name(ctx))),
   "artist namefirstcharacter": lambda ctx: _first_character(_resolve_artist_name(ctx)),
   "artist disambiguation": lambda ctx: _get_tag(ctx.tags, "artistdisambiguation", "disambiguation"),
   "artist genre": lambda ctx: _get_tag(ctx.tags, "genre"),
   "artist mbid": lambda ctx: _get_tag(ctx.tags, "musicbrainz_artistid", "musicbrainz_artist_id"),
   "album title": _resolve_album_title,
   "album cleantitle": lambda ctx: _clean_text(_resolve_album_title(ctx)),
   "album titlethe": lambda ctx: _apply_name_the(_resolve_album_title(ctx)),
   "album cleantitlethe": lambda ctx: _apply_name_the(_clean_text(_resolve_album_title(ctx))),
   "album type": lambda ctx: _get_tag(ctx.tags, "albumtype"),
   "album disambiguation": lambda ctx: _get_tag(ctx.tags, "albumdisambiguation", "disambiguation"),
   "album genre": lambda ctx: _get_tag(ctx.tags, "genre"),
   "album mbid": lambda ctx: _get_tag(ctx.tags, "musicbrainz_albumid", "musicbrainz_album_id"),
   "release year": _resolve_year,
   "medium:0": lambda ctx: _format_number(_resolve_disc_number(ctx), 1),
   "medium:00": lambda ctx: _format_number(_resolve_disc_number(ctx), 2),
   "medium name": _medium_name,
   "medium format": _medium_format,
   "track:0": lambda ctx: _format_number(_resolve_track_number(ctx), 1),
   "track:00": lambda ctx: _format_number(_resolve_track_number(ctx), 2),
   "track title": _resolve_track_title,
   "track cleantitle": lambda ctx: _clean_text(_resolve_track_title(ctx)),
   "track artistname": _resolve_track_artist_name,
   "track artistcleanname": lambda ctx: _clean_text(_resolve_track_artist_name(ctx)),
   "track artistnamethe": lambda ctx: _apply_name_the(_resolve_track_artist_name(ctx)),
   "track artistcleannamethe": lambda ctx: _apply_name_the(_clean_text(_resolve_track_artist_name(ctx))),
   "track artistmbid": lambda ctx: _get_tag(ctx.tags, "musicbrainz_trackartistid", "musicbrainz_artistid"),
   "quality full": _quality_full,
   "quality title": _quality_title,
   "mediainfo audiocodec": _media_info_audio_codec,
   "mediainfo audiochannels": _media_info_audio_channels,
   "mediainfo audiobitrate": _media_info_audio_bitrate,
   "mediainfo audiobitspersample": _media_info_audio_bits_per_sample,
   "mediainfo audiosamplerate": _media_info_audio_sample_rate,
   "release group": lambda ctx: _get_tag(ctx.tags, "releasegroup", "musicbrainz_releasegroupid"),
   "custom formats": lambda ctx: _get_tag(ctx.tags, "customformats", "custom formats"),
   "original title": _original_title,
   "original filename": _original_filename,
}
