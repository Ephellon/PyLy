# PyLy (Python Lyrics Pipeline)

PyLy is an **offline** tool that generates Plex-compatible `.lrc` (synchronized) lyric files from audio by:

1. Running (offline) Whisper transcription to generate an `.srt` file
2. Filtering noise **at the text/subtitle level only** to a `.red.srt`
3. Converting the `.red.srt` file to an `.lrc` for Plex

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
* No overwriting of existing `.lrc` files unless explicitly requested.

## Requirements

* Python 3.10+
* Whisper installed as a Python module
    * `pip install whisper`
* `ffmpeg` available on PATH (used only for decoding during transcription)
    * `ffmpeg.exe` can also be placed at `./ff/ffmpeg.exe`, see [ffmpeg handling](#ffmpeg-handling)

## Optional

* Torch (CUDA)
    * `pip install --upgrade torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121`
* `ffprobe` available on PATH (used only for tag reading)
    * `ffprobe.exe` can also be placed at `./ff/ffprobe.exe`, see [ffmpeg handling](#ffmpeg-handling)

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
* `Track.lrc` — Final Plex-compatible lyrics
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
* A rescue pass can optionally drop low-confidence filler and fill missed base lines when overall similarity is high enough.

---

## Online base lyric fetching

PyLy can fetch **plain text lyrics** as a base reference when a local base file is not provided (or is missing).

* `--fetch` enables fetching (default provider: `lrclib`).
* `-k` / `--keep-as-primary` implies `--fetch` and prefers fetched synced LRC as the primary output when available.
* `-K` / `--keep-as-alternate` implies `--fetch`, keeps fetched synced LRC as `<basename>.fetched.lrc`, and still generates Whisper output.
* Results are cached in `.pyly_cache/` to avoid repeated requests.
* Fetched plain lyrics are treated the same as local base lyrics (timings still come from Whisper).

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

* `{Artist Name}`, `{Album Title}`, `{Track Title}`, `{Track Number}`, `{Disc Number}`, `{Year}`

If tags are missing, you can provide a folder layout hint:

* `--layout lidarr` or `--layout plex` (`artist/album/track` folder structures)
* `--layout flat` (all files in one folder)

You can also supply a **custom layout template** string instead of a preset. If the `--layout` value
contains path separators (`/` or `\`) or token braces (`{}`), it is treated as a template. The template
describes the full path up to the filename stem (the file extension is ignored). Tokens are
case-insensitive and use the same names as fetch templates (matching is case-insensitive on __Windows__).

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

When a custom layout template is used, PyLy will log whether the template matched each file.
If a template does not match, PyLy falls back to the normal path heuristics and continues.

---

## ffmpeg handling

PyLy prefers the user’s existing `ffmpeg` installation if it is available on `PATH`.

If `ffmpeg` is **not** found on `PATH`, PyLy automatically falls back to a copy located at `./ff/`.

This fallback applies **only** to the Whisper subprocess and does not modify the global environment.

If neither a system nor local copy is available, PyLy fails loudly with a clear error message.

---

## Base lyrics vs Truth lyrics

PyLy supports two ways to use a text-only lyrics file (or fetched lyrics) to improve Whisper output. Both modes **preserve Whisper timing** and **never modify audio**.

### Quick rule of thumb

* Use `--base` when you trust Whisper more than `lyrics.txt`
    - *best-effort cleanup*
* Use `--truth` when you trust `lyrics.txt` more than Whisper
    - __only__ activates on high-confidence matches (`--base-diff-threshold`)
    - *fix/replace the garble*

### Base (`--base` / `--lyrics`)

Base lyrics are a *reference* to help clean up Whisper’s text when Whisper is mostly right but has mistakes.

What PyLy does in base mode:

* Matches Whisper lines to the base lyrics in order (monotonic matching).
* Replaces a Whisper line with a base line **only when they are similar enough** (`--base-threshold`).
* Optionally drops unmatched Whisper lines (`--base-strict`).
* Optionally runs a *rescue pass* to fix small “garbage” segments, but **only when the overall match is already close** (`--base-diff-threshold` + `--base-rescue`).

When to use `--base`:

* You have correct-ish lyrics and want better readability.
* Whisper is generally tracking the song, but has occasional garble / wrong words.
* You still want Whisper to “drive” the content when the base doesn’t clearly match.

### Truth (`--truth` / `--base-truth`)

Truth lyrics treat the provided lyrics as **ground truth** *only in safe conditions*.

Truth mode is stricter than base mode:

* PyLy first checks that the base/truth lyrics match the Whisper transcript **overall** (high overall similarity).
* If the match is close enough, PyLy is allowed to **patch mismatched spans**:
    * When Whisper outputs obvious nonsense over a small region, PyLy replaces that region with the truth lines.
    * Timing still comes from Whisper: PyLy reuses timestamps from the mismatched Whisper span.

Truth mode is guarded:

* If the overall similarity is *not* high enough, truth patching is **not applied** (PyLy falls back to normal base behavior or Whisper-only output, depending on flags).

When to use `--truth`:

* You have verified “correct” lyrics (liner notes, official lyrics, etc.).
* The transcript is mostly aligned, but has small garbage sections (ad-libs, repeated nonsense, partially coherent  sections).
* You want those segments fixed even when Whisper can’t be trusted.

### How `--fetch` fits in

`--fetch` just provides lyrics text automatically (like an automatic `--lyrics`) and follows the same rules:

* If you’re in `--base` mode, fetched lyrics act as base lyrics.
* If you’re in `--truth` mode, fetched lyrics act as truth lyrics (but only patch when overall similarity is high).

---

## All flags

| Flag(s) / Syntax(es)                                   | Description                                                                                          |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `<path>`                                               | Audio file or directory to process (positional argument).                                            |
| `--recursive` `-r`                                     | Recurse into subdirectories when `<path>` is a folder.                                               |
| `--overwrite` `-o`                                     | Overwrite an existing `.lrc` file if present.                                                        |
| `--clean` `-c`                                         | Delete intermediate files (`.srt`, `.red.srt`) after successful `.lrc` generation.                   |
| `--dry-run` `-q`                                       | Show planned actions without running Whisper, fetching lyrics, or writing files.                     |
| `--log` `-v`                                           | Write a per-file `*.pyly.log` with Whisper command, ffmpeg source, and base-lyrics statistics.       |
| `--model <name>` `-m <name>`                           | Whisper model to use (`tiny`, `base`, `small`, `medium`, `large`). Default: `small`.                 |
| `--language <code>` `-l <code>`                        | Force Whisper language (e.g. `en`). If omitted, Whisper auto-detects.                                |
| `--device <cpu\|cuda>` `-d <cpu\|cuda>`                | Device to run Whisper on. Passed through directly to Whisper.                                        |
| `--base <lyrics.txt>` `-b <lyrics.txt>`                | Use a text-only lyrics file (no timestamps) as a reference for matching and substitution.            |
| `--base-lyrics <lyrics.txt>`                           | *Alias for `--base`.*                                                                                |
| `--lyrics <lyrics.txt>`                                | *Alias for `--base`.*                                                                                |
| `--truth` `--base-truth` `-u`                           | Treat base lyrics as ground truth for patching on high-confidence matches (uses `--base-diff-threshold`). |
| `--base-strict` `-s`                                   | Drop unmatched Whisper lines when base lyrics are provided.                                          |
| `--base-threshold <0..1>` `-t <0..1>`                  | Similarity required to replace Whisper text with base lyrics. Default: `0.82`.                       |
| `--base-window <N>` `-w <N>`                           | Lookahead window (in base lyric lines) used during matching. Default: `12`.                          |
| `--base-max-merge <N>` `-x <N>`                        | Max Whisper lines to merge into one base match. Default: `5`.                                        |
| `--base-diff-threshold <0..1>` `-i <0..1>`             | Global similarity required to enable rescue pass. Default: `0.75`.                                   |
| `--base-rescue` `-e`                                   | Enable diff-driven rescue pass (default when base lyrics are used).                                  |
| `--no-base-rescue` `-E`                                | Disable diff-driven rescue pass.                                                                     |
| `--lrc-header` `-a`                                    | Write PyLy header tags and statistics into the LRC. Default: on.                                     |
| `--no-lrc-header` `-A`                                 | Do not write header tags into the LRC.                                                               |
| `--fetch [provider/template]` `-f [provider/template]` | Fetch base lyrics online (default provider: `lrclib`). With `-k`, prefer synced online LRC; with `-K`, keep synced online LRC as `<basename>.fetched.lrc`. |
| `--keep-as-primary` `-k`                               | Prefer fetched synced LRC as primary output and skip Whisper when synced lyrics are available.       |
| `--keep-as-alternate` `-K`                             | Keep fetched synced LRC as `<basename>.fetched.lrc` while still generating Whisper primary output.   |
| `--layout <hint>` `-y <hint>`                          | Layout hint or template schema for deriving metadata from paths when tags are missing.               |
