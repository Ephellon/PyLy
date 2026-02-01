from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _project_root_dir() -> Path:
   # repo layout →
   # PyLy/
   #   ff/
   #   pyly/
   return Path(__file__).resolve().parents[1]


def _bundled_ff_dir() -> Path:
   return _project_root_dir() / "ff"


def _find_system_ffmpeg() -> str | None:
   return shutil.which("ffmpeg")


def _env_with_bundled_ffmpeg(base_env: dict[str, str] | None = None) -> tuple[dict[str, str], str]:
   """
   Prefer system ffmpeg on PATH. If not found, prepend ./ff to PATH.
   Returns: (env, ffmpeg_source_string)
   """
   env = dict(base_env) if base_env else dict(os.environ)

   sys_ff = _find_system_ffmpeg()
   if sys_ff:
      return env, "system PATH"

   ff_dir = _bundled_ff_dir()
   ff_exe = ff_dir / "ffmpeg.exe"

   if ff_exe.exists():
      # Prepend bundled folder for the Whisper subprocess only.
      old = env.get("PATH", "")
      env["PATH"] = str(ff_dir) + (os.pathsep + old if old else "")
      return env, f"bundled ({ff_exe})"

   return env, "missing"


def _log_append(log_path: Path | None, msg: str) -> None:
   if not log_path:
      return
   try:
      with open(log_path, "a", encoding="utf-8", newline="\n") as f:
         f.write(msg.rstrip("\n") + "\n")
   except Exception:
      pass


def _is_progress_spam(line: str) -> bool:
   """
   Whisper uses tqdm (and other tools) that emit carriage-return progress updates.
   We keep logs clean by discarding lines that look like progress bars.
   """
   if not line:
      return True

   # If it's a carriage-return style update, it's almost always a progress bar.
   if "\r" in line:
      return True

   s = line.strip()
   if not s:
      return True

   # Typical tqdm patterns: " 12%|###...| 123/999 [..]"
   if "|" in s and "%" in s:
      return True

   # Another common: "0/19961 [00:00<?, ?frames/s]"
   if "[" in s and "]" in s and "/" in s and ("it/s" in s or "frames/s" in s):
      return True

   return False


def whisper_to_srt(
   audio_path: Path,
   output_dir: Path,
   model: str = "small",
   language: str | None = None,
   device: str | None = None,
   log_path: Path | None = None,
) -> Path:
   """
   Runs offline Whisper via: python -m whisper <audio> ... -> writes <stem>.srt in output_dir
   Returns the expected .srt path.
   """

   audio_path = Path(audio_path)
   output_dir = Path(output_dir)

   if not audio_path.exists():
      raise FileNotFoundError(str(audio_path))

   output_dir.mkdir(parents=True, exist_ok=True)

   out_srt = output_dir / (audio_path.stem + ".srt")

   cmd: list[str] = [
      sys.executable, "-m", "whisper",
      str(audio_path),
      "--task", "transcribe",
      "--model", str(model),
      "--output_format", "srt",
      "--output_dir", str(output_dir),
      "--verbose", "False",
   ]

   if language:
      cmd += ["--language", str(language)]

   if device:
      cmd += ["--device", str(device)]

   env, ffmpeg_src = _env_with_bundled_ffmpeg()

   _log_append(log_path, "CMD: " + " ".join(cmd))
   _log_append(log_path, "FFMPEG: " + ffmpeg_src)

   if ffmpeg_src == "missing":
      raise RuntimeError("ffmpeg not found on PATH and bundled ./ff/ffmpeg.exe not found.")

   # Reduce progress spam in logs:
   # - TQDM_DISABLE sometimes works depending on upstream usage
   # - Still filter any progress-like lines as a failsafe
   env = dict(env)
   env.setdefault("TQDM_DISABLE", "1")

   # Run whisper; keep the console alive but keep logs readable.
   # - stdout: generally quiet; still capture for errors/debug
   # - stderr: where tqdm + warnings usually go; we capture and filter into log
   proc = subprocess.Popen(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      encoding="utf-8",
      errors="replace",
      env=env,
      cwd=str(output_dir),
   )

   # Stream stderr so user sees activity; log filtered lines
   assert proc.stderr is not None
   assert proc.stdout is not None

   # stdout is usually empty; read after
   for raw in proc.stderr:
      # show on console (so user sees it's alive)
      try:
         sys.stderr.write(raw)
      except Exception:
         pass

      if not _is_progress_spam(raw):
         _log_append(log_path, raw.rstrip("\n"))

   stdout_text = proc.stdout.read() or ""
   rc = proc.wait()

   if stdout_text.strip():
      # Rare, but keep it for debugging
      _log_append(log_path, "WHISPER_STDOUT:")
      for ln in stdout_text.splitlines():
         if not _is_progress_spam(ln):
            _log_append(log_path, ln)

   if rc != 0:
      raise RuntimeError(f"Whisper exited with code {rc} (see log: {log_path})")

   if not out_srt.exists():
      # Whisper sometimes changes output naming in odd edge cases; be explicit.
      raise RuntimeError(f"Whisper completed but expected SRT not found: {out_srt}")

   return out_srt


def transcribe_to_srt(
   audio_path: Path,
   output_dir: Path,
   model: str = "small",
   language: str | None = None,
   device: str | None = None,
   log_path: Path | None = None,
) -> Path:
   """
   Wrapper used by pipeline.py. Kept for historical naming consistency.
   """
   return whisper_to_srt(
      audio_path=audio_path,
      output_dir=output_dir,
      model=model,
      language=language,
      device=device,
      log_path=log_path,
   )
