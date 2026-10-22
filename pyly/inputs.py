"""
Turning the paths given on the command line into lists of files.

Shared by every PyLy action (lyrics, tags, cover art), so it lives outside
cli.py: any module can collect inputs without importing the command line.
"""

from pathlib import Path


AUDIO_EXTS = {
   ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".alac", ".wma", ".aiff"
}


def is_audio_file(path: Path) -> bool:
   return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def collect_audio_files(
   inputs: list[str | Path],
   recursive: bool = False,
) -> list[Path]:
   """
   Resolve CLI inputs into a sorted list of audio files.

   Rules:
   - Files are accepted only if they are known audio types
   - Directories yield audio files inside them
   - --include-subfolders controls deep traversal
   - Duplicates are removed
   - Output is deterministic
   """

   results: set[Path] = set()

   for raw in inputs:
      p = Path(raw).expanduser()

      if "*" in str(p) or "?" in str(p):
         for gp in p.parent.glob(p.name):
            if gp.is_dir():
               if recursive:
                  for f in gp.rglob("*"):
                     if is_audio_file(f):
                        results.add(f.resolve())
               else:
                  for f in gp.iterdir():
                     if is_audio_file(f):
                        results.add(f.resolve())
            elif is_audio_file(gp):
               results.add(gp.resolve())
         continue

      if not p.exists():
         raise FileNotFoundError(f"Input not found: {p}")

      if p.is_file():
         if is_audio_file(p):
            results.add(p.resolve())
         else:
            raise ValueError(f"Not an audio file: {p}")
         continue

      if p.is_dir():
         if recursive:
            for f in p.rglob("*"):
               if is_audio_file(f):
                  results.add(f.resolve())
         else:
            for f in p.iterdir():
               if is_audio_file(f):
                  results.add(f.resolve())
         continue

   return sorted(results)


def collect_lrc_files(
   inputs: list[str | Path],
   recursive: bool = False,
) -> list[Path]:
   """Collect .lrc files from the given paths."""
   results: set[Path] = set()

   for raw in inputs:
      p = Path(raw).expanduser()

      if not p.exists():
         raise FileNotFoundError(f"Input not found: {p}")

      if p.is_file():
         if p.suffix.lower() == ".lrc":
            results.add(p.resolve())
         else:
            raise ValueError(f"Not an .lrc file: {p}")
         continue

      if p.is_dir():
         for f in (p.rglob("*.lrc") if recursive else p.glob("*.lrc")):
            results.add(f.resolve())

   return sorted(results)
