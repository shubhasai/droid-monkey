"""Where droid-monkey keeps its files in the app repo: everything under .droid-monkey/.

    .droid-monkey/
      .gitignore            created automatically: ignores runs/ and in-progress files
      profile.json          static analysis
      ANALYSIS.md           Claude Code's analysis, for developers
      flows.json            flows from the analysis
      analysis-run.json     cost and session of the last analysis
      screen-map.json       the one current screen graph
      attacks/*.yaml        custom attack specs (committed)
      runs/<run-id>/...     evidence from attack runs (git-ignored)
"""

from pathlib import Path

DATA_DIR = ".droid-monkey"
_GITIGNORE = "# droid-monkey run output and in-progress files\nruns/\n*.partial.json\n"


def data_dir(app_dir: Path) -> Path:
    d = app_dir / DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE)
    return d


def map_path(app_dir: Path) -> Path:
    return data_dir(app_dir) / "screen-map.json"


def partial_map_path(app_dir: Path) -> Path:
    return data_dir(app_dir) / "screen-map.partial.json"


def flows_path(app_dir: Path) -> Path:
    return data_dir(app_dir) / "flows.json"


def profile_path(app_dir: Path) -> Path:
    return data_dir(app_dir) / "profile.json"


def attacks_dir(app_dir: Path) -> Path:
    return data_dir(app_dir) / "attacks"


def run_dir(app_dir: Path, run_id: str) -> Path:
    """Evidence folder for one run. Not created here: it appears only once something is written into it."""
    return data_dir(app_dir) / "runs" / run_id
