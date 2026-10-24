# AGENTS.md

Guide for AI coding agents working on PyLy. Terse by design. Human docs: `README.md`.

## What PyLy is

Offline-first Python CLI for a music library. Writes `.lrc` lyrics (downloaded synced lyrics first, Whisper transcription as fallback), fixes audio tags from `album.nfo`, fills `album.nfo` from MusicBrainz, embeds cover art.

## Repo map

| Path | Role |
|---|---|
| `pyly/options.py` | Contract: `FLAGS` (every flag: name, letter, value, default, help), `RunOptions`, `ACTION_ORDER`. Single source for CLI, settings file, docs. |
| `pyly/cli.py` | Build argparse from `FLAGS`, merge settings, dispatch actions. |
| `pyly/settings.py` | Settings file (`~/.pyly/pyly.ini` / `$PYLY_CONFIG`), `build_options()`, `get_key()`. |
| `pyly/run_lyrics.py` | `run_lyrics(opts)`: make / get / refresh / upgrade lyrics. |
| `pyly/run_tags.py` | `run_get_info`, `run_fix_tags`, `run_cover`, `run_check_tags`. |
| `pyly/pipeline.py` | Per-file pipeline: fetch, Whisper, align, write `.lrc`. |
| `pyly/lyrics_fetch.py` | Lyric sources, MusicBrainz client (rate limit + 503 retry), Musixmatch. |
| `pyly/cover_art.py` | Cover sources, `ArtResolver(prefer="local"/"online")`, ffmpeg embedding. |
| `pyly/file_metadata.py` | Tag and `.nfo` read/write, comparison, fuzzy matching. |
| `pyly/template_tokens.py` | Path/layout guessing (`Artist/Album/Track` default). |
| `pyly/inputs.py` | Collect audio / `.lrc` files from paths. |
| `pyly/whisper_offline.py` | Whisper subprocess; `--whisper-python` selects interpreter. |
| `tests/` | stdlib `unittest` suite. |
| `pyly.bat`, `pyly.sh` | Launchers; put repo first on `PYTHONPATH`. |
| `ff/` | Bundled ffmpeg/ffprobe. |

## Rules

- Standard library only (`dependencies = []`). Python 3.10+. 3-space indent.
- Plain-English comments in every file: module docstring, function/class docstrings, why-comments. Reader may know little Python.
- New or changed flags only via `options.FLAGS`; `RunOptions` field = flag name with underscores.
- Flag naming:
  - action flags = future-tense verbs ("this run will ..."); setting flags = nouns (`--model`, `--layout`);
  - 3 words max, plain English, dash-separated;
  - `get-` = online, `use-` = local; one verb per domain.
- Letters: most used first, then most important, then alphabetical. Taken letter -> next letter of name. Direct opposite -> partner's letter uppercase. `-l` = `--language` (exception). `-h` reserved. Rare flags: no letter.
- `album.nfo` is the truth for tags; then folder layout; then filename.
- README examples: only `R:\Local\Queen\A Night at the Opera`, tracks `03 I'm in Love With My Car.mp3` (3:05), `11 Bohemian Rhapsody.mp3` (5:55).
- Never put keys on the command line, in `.lrc` files, caches or logs.
- No backward-compatibility shims: single user; update `R:\.etc\fetch-lyrics.ps1` (owned by Task Master) when flags change.

## Commands

```bash
PYTHONPATH=. python -m pyly "R:\Local\Queen\A Night at the Opera" --check-tags
python -m unittest discover -s tests -t .
```

- Windows console: set `PYTHONIOENCODING=utf-8` for tests.
- Tests never touch real `~/.pyly`: patch `pathlib.Path.home` to a temp folder.
- Tests and experiments never write under `R:\Local`: copy albums to a temp folder first.

## Behavior quick reference

- Shape: `pyly <path> [<path> ...] [flags]`; paths before or after flags. No action = `--make-lyrics`. Prefix shortening off.
- Run order (fixed): `--get-info`, `--fix-tags`, `--get-cover`/`--use-cover`, `--check-tags`, lyrics.
- Exit: 0 fine; 1 file failed or `--check-tags` mismatch; 2 run could not start.
- Lyrics:
  - `--make-lyrics` (-m): download synced first, Whisper for the rest.
  - `--get-lyrics [SOURCES]` (-g): download only, never Whisper; comma list in order; optional `:query`; default `musicbrainz`. With `--make-lyrics`: picks sources.
  - `--use-lyrics FILE` (-u), `--keep-both` (-k), `--trust-lyrics` (-t), `--drop-extras` (-x): each turns on `--make-lyrics`.
  - `--refresh-lyrics` (-y): re-download from `[PyLy:<url>]` header; saves unless `--preview`.
  - `--upgrade-lyrics`: Whisper-made `.lrc` only; replace when synced found; never Whisper.
  - Whisper hears no words -> marker `.lrc` (`[00:00.00] (Instrumental)` + `[pyly_no_vocals:true]`), status OK, so nightly runs don't redo it.
  - Titles: ` - feat. X` / `(feat. X)` stripped before searching (`strip_featuring`).
  - `--get-info`: MBID 404 -> "stale MusicBrainz id" error + `search_mb_release` best match (artist id from album.nfo/artist.nfo, else name; album; track count; never fewer tracks, prefers official) printed, never written; no match -> search link. Still FAIL.
  - `lyricradar`, `genius`, `musixmatch` need `--allow-scraping` (`--apsss`); `musixmatch` also needs `[keys] musixmatch`.
- Tags/cover:
  - `--get-info` (-e): fill `album.nfo` from MusicBrainz; `--replace` overwrites; never creates one.
  - `--fix-tags` (-f), `--check-tags [strict|loose]` (-c, default strict). Fix compares loose, or in the `--check-tags` mode when both given.
  - `--use-cover` (-s): folder image only. `--get-cover [SOURCE[=KEY]]` (-v): download (default `musicbrainz`), fall back to folder image.
  - Embedded art skipped unless `--replace`. Audio writes keep `.bak` unless `--skip-backup` (-b).
- Run control: `--include-subfolders` (-i), `--replace` (-r), `--preview` (-p), `--clean-up` (-n), `--write-log` (-w), `--skip-header`, `--list-sources`.
- Settings: `--profile`, `--language` (-l), `--layout` (-a), `--model` (-o, default `medium`), `--device` (-d), `--whisper-python`, `--color`. Tuning (no letters): `--match-score`, `--match-window`, `--merge-limit`, `--rescue-score`, `--skip-rescue`.
- Settings file: priority CLI > `[profile]` > `[defaults]` > built-in. Keys = flag names (`get-lyrics = musicbrainz,musixmatch`). Switches: yes/no/true/false/on/off/1/0. Value-or-switch: `yes` = default value, `no` = off. Key lookup: `[keys]` entry > `PYLY_<SOURCE>_KEY` > `~/.pyly/<source>-key`.

## Git

- Commit directly on `master`; no feature branch unless asked.
- Stage only paths the task touched; working tree may hold unrelated changes (line-ending churn).
- Commit messages in normal prose: summary line, blank line, body explaining why.
- Push with `git push origin master`.
