"""
PyLy 2 contract: every flag, the options object one run carries, and the
functions the command line calls. Written first so the settings loader, the
command line and the action modules can be built separately against it.

Naming rules (the operator's, for every flag):
  - Action flags read as "this run will <flag>": --make-lyrics, --fix-tags.
  - Setting flags are plain nouns: --model, --layout.
  - Three words or fewer, plain English, words joined by dashes.
  - Online actions start with "get", local ones with "use".
  - Letters: most used flag first, then most important, then alphabetical.
    A taken letter moves to the next letter of the name; a direct opposite
    takes its partner's letter in uppercase. -l is --language (the one
    exception). -h stays reserved for help. Rare flags get no letter.

Who builds what against this file:
  pyly/settings.py   load_settings(), get_key()          (job pyly2-front)
  pyly/cli.py        main(): parse, combine, dispatch     (job pyly2-front)
  pyly/run_lyrics.py run_lyrics(opts)                     (job pyly2-lyrics)
  pyly/run_tags.py   run_get_info / run_fix_tags /
                     run_cover / run_check_tags (opts)    (job pyly2-tags)
Each run_* function takes a finished RunOptions and returns an exit code:
0 = everything fine, 1 = some file failed or (for --check-tags) mismatched,
2 = the run could not start (bad input). It collects its own files with
pyly.inputs.collect_audio_files(opts.paths, opts.include_subfolders).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Flag table: the single source for argparse, the settings file and the README
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Flag:
   """
   name     long name without dashes; also the settings-file key and, with
            dashes turned into underscores, the RunOptions field.
   letter   single letter, or None. Extra long names (e.g. "apsss") go in
            aliases.
   kind     "action"   does something (switch or optional value)
            "switch"   changes how actions behave (on/off)
            "setting"  takes a value (a noun)
   value    None (on/off switch), "text", "int", "float", or a tuple of
            allowed words.
   optional True when the flag works with or without a value; then
            `const` is the value used when none is given.
   default  value when neither the command line nor the settings file sets it.
   """
   name: str
   letter: str | None
   kind: str
   help: str
   value: object = None
   optional: bool = False
   const: object = None
   default: object = None
   metavar: str | None = None
   aliases: tuple[str, ...] = ()
   group: str = ""

   @property
   def dest(self) -> str:
      return self.name.replace("-", "_")


FLAGS: tuple[Flag, ...] = (
   # -- Lyrics actions -------------------------------------------------------
   Flag("make-lyrics", "m", "action", group="lyrics",
        help="create .lrc lyrics: download synced lyrics first, then run Whisper "
             "for any track still without them. The default when no action is given."),
   Flag("get-lyrics", "g", "action", group="lyrics", value="text", optional=True, const="",
        metavar="SOURCES",
        help="download lyrics only (never Whisper), trying each source in order, "
             "e.g. musicbrainz,musixmatch. With --make-lyrics, picks the sources it searches. "
             "An optional :query after the sources sets a custom search."),
   Flag("use-lyrics", "u", "action", group="lyrics", value="text", metavar="FILE",
        help="use a local lyrics text file (or a pattern such as *.txt) as the reference "
             "Whisper is matched against. Turns on --make-lyrics."),
   Flag("refresh-lyrics", "y", "action", group="lyrics",
        help="download lyrics again from the address saved in each .lrc and save them."),
   Flag("upgrade-lyrics", None, "action", group="lyrics",
        help="replace Whisper-made .lrc files wherever a source has synced lyrics. "
             "Never runs Whisper."),
   Flag("keep-both", "k", "switch", group="lyrics",
        help="save downloaded lyrics as <name>.fetched.lrc and still run Whisper for the .lrc. "
             "Turns on --make-lyrics."),
   Flag("trust-lyrics", "t", "switch", group="lyrics",
        help="treat the reference lyrics as correct when patching Whisper's text. "
             "Turns on --make-lyrics."),
   Flag("drop-extras", "x", "switch", group="lyrics",
        help="drop Whisper lines that match nothing in the reference lyrics. "
             "Turns on --make-lyrics."),

   # -- Tag, album.nfo and cover actions ----------------------------------------
   Flag("get-info", "e", "action", group="tags",
        help="fill in missing album.nfo details from MusicBrainz (with --replace, "
             "overwrite details already there)."),
   Flag("fix-tags", "f", "action", group="tags",
        help="rewrite audio tags that don't match album.nfo."),
   Flag("check-tags", "c", "action", group="tags", value=("strict", "loose"), optional=True,
        const="strict", metavar="MODE",
        help="report tags that don't match album.nfo; writes nothing. MODE: strict (default) "
             "or loose."),
   Flag("get-cover", "v", "action", group="tags", value="text", optional=True, const="musicbrainz",
        metavar="SOURCE[=KEY]",
        help="download cover art and embed it, falling back to the folder's own image when "
             "the source finds nothing. Default source: musicbrainz."),
   Flag("use-cover", "s", "action", group="tags",
        help="embed the folder's album/poster/default/cover/folder image. Never downloads."),

   # -- Run control (switches) ---------------------------------------------------
   Flag("include-subfolders", "i", "switch", group="run",
        help="process every folder under the path, not just the path itself."),
   Flag("replace", "r", "switch", group="run",
        help="replace outputs that already exist: .lrc files, album.nfo details, embedded art."),
   Flag("preview", "p", "switch", group="run",
        help="show what would happen and change nothing."),
   Flag("clean-up", "n", "switch", group="run",
        help="delete the temporary .srt files Whisper leaves behind."),
   Flag("write-log", "w", "switch", group="run",
        help="save a log file next to each .lrc."),
   Flag("skip-backup", "b", "switch", group="run",
        help="don't keep a .bak copy before rewriting tags or embedding art."),
   Flag("skip-header", None, "switch", group="run",
        help="leave the tag lines out of the .lrc header."),
   Flag("allow-scraping", None, "switch", group="run", aliases=("apsss",),
        help="allow sources that read websites instead of a public API "
             "(lyricradar, genius, musixmatch)."),
   Flag("list-sources", None, "action", group="run",
        help="print the lyric and cover sources, then stop."),

   # -- Settings (nouns) ---------------------------------------------------------
   Flag("profile", None, "setting", group="settings", value="text", metavar="NAME",
        help="use a [NAME] section of the settings file (~/.pyly/pyly.ini or $PYLY_CONFIG)."),
   Flag("language", "l", "setting", group="settings", value="text", metavar="CODE",
        help="language Whisper should expect, e.g. en. Whisper guesses when it's not set."),
   Flag("layout", "a", "setting", group="settings", value="text", metavar="LAYOUT",
        help="how folders are arranged: lidarr, plex, flat, or a template such as "
             "R:\\Local\\{Artist Name}\\{Album Title}\\{track:00} {Track Title}. "
             "Default: Artist/Album/Track."),
   Flag("model", "o", "setting", group="settings", value=("tiny", "base", "small", "medium", "large"),
        default="medium", metavar="SIZE", help="Whisper model size. Default: medium."),
   Flag("device", "d", "setting", group="settings", value=("cpu", "cuda"), metavar="DEVICE",
        help="where Whisper runs. Whisper decides when it's not set."),
   Flag("whisper-python", None, "setting", group="settings", value="text", metavar="PYTHON",
        help="the Python that has Whisper installed. Default: $PYLY_WHISPER_PYTHON, then the "
             "Python running PyLy."),
   Flag("color", None, "setting", group="settings", value=("auto", "on", "off"), default="auto",
        metavar="WHEN", help="colored output. Default: auto (on in a terminal)."),
   Flag("mismatch-file", None, "setting", group="settings", value="text", metavar="FILE",
        help="FAFO's mismatches.jsonl; --check-tags shows its flags beside tag problems. "
             "Read only, never acted on."),

   # -- Fine-tuning for matching reference lyrics (rarely used, no letters) ---
   Flag("match-score", None, "setting", group="tuning", value="float", default=0.82, metavar="0-1",
        help="how similar a Whisper line must be to a reference line to be replaced by it."),
   Flag("match-window", None, "setting", group="tuning", value="int", default=12, metavar="N",
        help="how many reference lines ahead to look for a match."),
   Flag("merge-limit", None, "setting", group="tuning", value="int", default=5, metavar="N",
        help="most Whisper lines that may merge into one reference line."),
   Flag("rescue-score", None, "setting", group="tuning", value="float", default=0.75, metavar="0-1",
        help="overall similarity needed before the rescue pass runs."),
   Flag("skip-rescue", None, "switch", group="tuning",
        help="turn off the rescue pass after matching."),
)

FLAG_BY_NAME: dict[str, Flag] = {f.name: f for f in FLAGS}

# Actions in the order a run performs them. A bare `pyly <path>` with no
# action means make-lyrics.
ACTION_ORDER = (
   "get-info", "fix-tags", "get-cover", "use-cover", "check-tags",
   "make-lyrics", "get-lyrics", "refresh-lyrics", "upgrade-lyrics",
)
DEFAULT_ACTION = "make-lyrics"
DEFAULT_LYRIC_SOURCES = "musicbrainz"


# ---------------------------------------------------------------------------
# One run's choices
# ---------------------------------------------------------------------------

@dataclass
class RunOptions:
   """
   Everything one run needs, after the command line, the chosen profile, the
   [defaults] section and the built-in defaults have been merged (in that
   order of priority). Field names are the flag names with dashes as
   underscores. Value-taking actions hold None when not requested.
   """
   paths: list[Path] = field(default_factory=list)

   # lyrics
   make_lyrics: bool = False
   get_lyrics: str | None = None          # "" = default sources; "a,b" or "a,b:query"
   use_lyrics: str | None = None
   refresh_lyrics: bool = False
   upgrade_lyrics: bool = False
   keep_both: bool = False
   trust_lyrics: bool = False
   drop_extras: bool = False

   # tags, album.nfo, cover
   get_info: bool = False
   fix_tags: bool = False
   check_tags: str | None = None          # "strict" | "loose"
   get_cover: str | None = None           # "source" or "source=key"
   use_cover: bool = False

   # run control
   include_subfolders: bool = False
   replace: bool = False
   preview: bool = False
   clean_up: bool = False
   write_log: bool = False
   skip_backup: bool = False
   skip_header: bool = False
   allow_scraping: bool = False
   list_sources: bool = False

   # settings
   profile: str | None = None
   language: str | None = None
   layout: str | None = None
   model: str = "medium"
   device: str | None = None
   whisper_python: str | None = None
   color: str = "auto"
   mismatch_file: str | None = None

   # tuning
   match_score: float = 0.82
   match_window: int = 12
   merge_limit: int = 5
   rescue_score: float = 0.75
   skip_rescue: bool = False

   @property
   def lyric_sources(self) -> str:
      """Sources for make-lyrics/get-lyrics/upgrade-lyrics, e.g. 'musicbrainz,musixmatch'."""
      return self.get_lyrics or DEFAULT_LYRIC_SOURCES

   @property
   def write_mode(self) -> str:
      """Mode for file_metadata.write_tags / cover_art.embed_cover."""
      return "direct" if self.skip_backup else "backup"

   def actions(self) -> list[str]:
      """Requested actions in run order (with make-lyrics filled in when none)."""
      chosen = [a for a in ACTION_ORDER if getattr(self, a.replace("-", "_")) not in (False, None)]
      return chosen or [DEFAULT_ACTION]
