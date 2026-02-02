# PyLy (Python Lyrics Pipeline)

PyLy is an **offline-default, Python-first** tool that generates PlexAmp-compatible `.lrc` lyric files from audio by:

1. Running offline Whisper transcription to `.srt`
2. Filtering noise **at the text/subtitle level only** to `.red.srt`
3. Converting to `.lrc` for PlexAmp

## Table of Contents

* [Overview](#pyly-python-lyrics-pipeline)
* [Non-negotiables](#non-negotiables)
* [Requirements](#requirements)
* [Install](#install)
* [Usage](#usage)
* [Outputs](#outputs)
* [Audio formats](#audio-formats)
* [Base lyrics assistance](#base-lyrics-assistance)
* [Online base lyric fetching](#online-base-lyric-fetching)
* [Fetch templates and layout hints](#fetch-templates-and-layout-hints)
* [ffmpeg handling](#ffmpeg-handling)
* [Online mode](#online-mode)
* [All flags](#all-flags)

## Non‑negotiables

* **Source audio is never modified** (no re-encode, normalize, denoise, etc).
* Filtering is **text-only** on transcription output.
* Offline Whisper (`python -m whisper`) is the default engine.
* Output is plain `.lrc` with `[mm:ss.xx] lyric line` timestamps (UTF‑8).
* No overwrite of existing `.lrc` unless explicitly requested.

## Requirements

* Python 3.10+
* Whisper installed as a Python module
    * `pip install whisper`
* ffmpeg available on PATH (used only for decoding during transcription)
    * ffmpeg `6.1.1` is available at `./ff/`, see [ffmpeg handling](#ffmpeg-handling)

## Optional

* Torch (CUDA)
    * `pip install --upgrade torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121`

## Install

```bash
pip install -e .
```

* Editable install (recommended while developing)

```bash
pyly.bat <file-path(s)> [flags]
```

* Installs PyLy and runs the command afterwards

## Usage

### Single file

```bash
pyly "X:\Music\Artist\Album\01 - Track.flac"
```

### Batch

```bash
pyly "X:\Music" --recursive
```

### Overwrite existing `.lrc`

```bash
pyly "X:\Music" --recursive --overwrite
```

### Remove intermediates after success

```bash
pyly "X:\Music\Artist\Album\01 - Track.flac" --clean
```

---

## Outputs

For `Track.flac`:

* `Track.srt` — Whisper transcription output
* `Track.red.srt` — Noise-reduced subtitles (text-only filtering)
* `Track.lrc` — Final PlexAmp-compatible lyrics
* `Track.pyly.log` — Optional per-file log (if `--log`)

## Audio formats

PyLy accepts common lossless and lossy audio formats:

* `.mp3`, `.flac`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.alac`, `.wma`, `.aiff`

## Base lyrics assistance

PyLy can optionally use a **text-only lyrics file** (no timestamps) as a reference to improve output quality when Whisper is inaccurate or incoherent.

This does **not** rewrite lyrics or alter meaning. The base lyrics are used only for **matching and substitution** against Whisper’s transcription while preserving Whisper-derived timing.

### Base lyric flags

* `--base <lyrics.txt>`

  * Provide a plain UTF-8 text file containing lyrics in order (no timestamps).
  * Lines are matched monotonically against Whisper output.
  * Supports wildcards in filename (e.g., `--base "*.txt"` resolves per-audio file).

* `--base-strict`

  * Drop unmatched Whisper lines when base lyrics are provided.
  * Useful when Whisper output is very noisy and the base lyrics are trusted.

* `--base-threshold <0..1>` (default: `0.82`)

  * Similarity score required to replace a Whisper line with a base lyric line.

* `--base-window <N>` (default: `12`)

  * Lookahead window (in base lyric lines) when attempting to match Whisper output.

* `--base-max-merge <N>` (default: `5`)

  * Maximum number of Whisper lines that can merge into a single base lyric match.

* `--base-diff-threshold <0..1>` (default: `0.75`)

  * Global similarity required to enable the rescue pass.

* `--base-rescue` / `--no-base-rescue`

  * Toggle the diff-driven rescue pass (enabled by default when base lyrics are used).

* `--lrc-header` / `--no-lrc-header`

  * Control whether PyLy writes `[re:]`, `[by:]`, and base-match stats into the LRC header.

### Behavior

* Whisper timings are always preserved.
* Base lyrics are never modified or rewritten.
* Unmatched lines fall back to Whisper output unless `--base-strict` is used.
* If base lyrics do not align well, PyLy safely degrades to Whisper-only behavior.
* A rescue pass can optionally drop low-confidence filler and fill missed base lines when global similarity is high enough.

---

## Online base lyric fetching

PyLy can fetch **plain text lyrics** as a base reference when a local base file is not provided (or is missing).

* `--fetch` enables fetching (default provider: `lrclib`).
* Results are cached in `.pyly_cache/` to avoid repeated requests.
* Fetched lyrics are treated the same as local base lyrics (timings still come from Whisper).

Examples:

```bash
pyly "X:\Music\Artist\Album\01 - Track.flac" --fetch
pyly "X:\Music\Artist\Album\01 - Track.flac" --fetch lrclib
pyly "X:\Music\Artist\Album\01 - Track.flac" --fetch "Radiohead Nude"
```

## Fetch templates and layout hints

Fetch queries can be template-driven to use metadata or path structure:

```bash
pyly "X:\Music\Artist\Album\01 - Track.flac" --fetch "lrclib:{Artist Name} {Track Title}"
```

Available tokens are derived from tags (via `ffprobe`) or inferred from the path. Common tokens include:

* `{Artist Name}`, `{Album Title}`, `{Track Title}`
* `{Track Number}`, `{Disc Number}`, `{Year}`

If tags are missing, you can provide a folder layout hint:

* `--layout lidarr` or `--layout plex` (artist/album/track folder structures)
* `--layout flat` (all files in one folder)

You can also supply a **custom layout template** string instead of a preset. If the `--layout` value
contains path separators (`/` or `\`) or token braces (`{}`), it is treated as a template. The template
describes the full path up to the filename stem (the audio extension is ignored). Tokens are
case-insensitive and use the same names as fetch templates (matching is case-insensitive on Windows).

Examples:

```bash
pyly "X:\Music" --recursive --layout "X:\Music\{Album Title} - {Artist Name}\{Track Title} ({track:00})"
```

```bash
pyly "/music" --recursive --layout "/music/{Artist Name}/{Album Title}/{track:0} - {Track Title}"
```

```bash
pyly "X:\Music" --recursive --layout "X:\Music\{Artist Name}\{Album Title}\{track:00} - {Track Title}"
```

When a custom layout template is used, PyLy will log (cyan) whether the template matched each file.
If a template does not match, PyLy falls back to the normal path heuristics and continues.

---

## ffmpeg handling

PyLy prefers the user’s existing `ffmpeg` installation if it is available on `PATH`.

If `ffmpeg` is **not** found on `PATH`, PyLy automatically falls back to a bundled copy located at `./ff/`.

This fallback applies **only** to the Whisper subprocess and does not modify the global environment.

If neither a system nor bundled `ffmpeg` is available, PyLy fails loudly with a clear error message.

---

## Online mode

`--online` is opt-in and must never silently replace offline behavior.

* Current behavior: **unimplemented** (fails loudly if used).

---

## All flags

| Flag                         | Description                                                                                     |
| ---------------------------- | ----------------------------------------------------------------------------------------------- |
| `<path>`                     | Audio file or directory to process (positional argument).                                       |
| `--recursive`                | Recurse into subdirectories when `<path>` is a folder.                                          |
| `--overwrite`                | Overwrite an existing `.lrc` file if present.                                                   |
| `--clean`                    | Delete intermediate files (`.srt`, `.red.srt`) after successful `.lrc` generation.              |
| `--dry-run`                  | Show what actions would be taken without running Whisper or writing files.                      |
| `--log`                      | Write a per-file `*.pyly.log` containing Whisper command, ffmpeg source, and base-lyrics stats. |
| `--model <name>`             | Whisper model to use (`tiny`, `base`, `small`, `medium`, `large`). Default: `small`.            |
| `--language <code>`          | Force Whisper language (e.g. `en`). If omitted, Whisper auto-detects.                           |
| `--device <cpu\|cuda>`       | Device to run Whisper on. Passed through directly to Whisper.                                   |
| `--online`                   | Opt-in online mode. **Currently unimplemented and will fail loudly.**                           |
| `--base <lyrics.txt>`        | Use a text-only lyrics file (no timestamps) as a reference for matching and substitution.       |
| `--base-lyrics <lyrics.txt>` | *Alias for `--base`.*                                                                           |
| `--lyrics <lyrics.txt>`      | *Alias for `--base`.*                                                                           |
| `--base-strict`              | Drop unmatched Whisper lines when base lyrics are provided.                                     |
| `--base-threshold <0..1>`    | Similarity threshold required to replace Whisper text with base lyrics. Default: `0.82`.        |
| `--base-window <N>`          | Lookahead window (in base lyric lines) used during matching. Default: `12`.                     |
| `--base-max-merge <N>`       | Max Whisper lines to merge into one base match. Default: `5`.                                   |
| `--base-diff-threshold <0..1>` | Global similarity required to enable rescue pass. Default: `0.75`.                             |
| `--base-rescue`              | Enable diff-driven rescue pass (default when base lyrics are used).                             |
| `--no-base-rescue`           | Disable diff-driven rescue pass.                                                                |
| `--lrc-header`               | Write PyLy header tags (`[re:]`, `[by:]`, stats) into the LRC. Default: on.                      |
| `--no-lrc-header`            | Do not write header tags into the LRC.                                                          |
| `--fetch [provider/template]` | Fetch base lyrics online (default provider: `lrclib`).                                         |
| `--layout <hint>`            | Folder layout hint (`lidarr`, `plex`, `flat`) for fetch templates when tags are missing.        |
