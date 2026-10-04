"""Backing up the private pool's runs to its own private Hugging Face dataset.

Private runs never leave the maintainer's machine except to the pool's own remotes: its private
git repository (run.json and cases.jsonl) and, with this, a private Hugging Face dataset that also
holds the full replies the repository leaves out (raw/generations.jsonl). The dataset is named in
the pool's pool.yaml (``hf_dataset``), never in this repository. `forcebench private backup`
uploads each run's run.json, cases.jsonl and raw/generations.jsonl under runs/<run id>/, after
checking that every run is a private run carrying the pool's canary. It refuses a target that is
not private (a gated public dataset is still public), and the public traces dataset. Needs the
`traces` extra (huggingface_hub).
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import HfHubHTTPError

from forcebench.pool import PrivatePool

# What a run holds that is worth keeping: its metadata, its graded cases and its full replies.
# Grading artifacts and locks stay behind.
RUN_FILES = ("run.json", "cases.jsonl", "raw/generations.jsonl")


class BackupError(ValueError):
    """The backup is refused: nothing was uploaded."""


class Hub(Protocol):
    def dataset_info(self, repo_id: str) -> Any: ...
    def create_commit(
        self,
        repo_id: str,
        operations: list[CommitOperationAdd],
        *,
        commit_message: str,
        repo_type: str,
    ) -> Any: ...


@dataclass(frozen=True)
class Backup:
    runs: list[str]
    files: dict[str, Path]  # path in the dataset -> file on this machine

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for p in self.files.values())


def collect(pool: PrivatePool) -> Backup:
    """The files to upload, after checking every run in the pool's results: a private run with
    the pool's canary, and no symbolic links among its files."""
    runs: list[str] = []
    files: dict[str, Path] = {}
    for meta_path in sorted(pool.runs_dir.glob("*/run.json")):
        run_dir = meta_path.parent
        meta = json.loads(meta_path.read_text())
        if meta.get("visibility") != "private" or meta.get("canary") != pool.canary:
            raise BackupError(f"refusing: {run_dir.name} is not a private run of this pool")
        runs.append(run_dir.name)
        for name in RUN_FILES:
            path = run_dir / name
            if path.is_symlink():
                raise BackupError(f"refusing: {run_dir.name}/{name} is a symbolic link")
            if path.is_file():
                files[f"runs/{run_dir.name}/{name}"] = path
    if not files:
        raise BackupError("refusing: the private pool has no runs to back up")
    return Backup(runs, files)


def check_target(api: Hub, repo_id: str, public_repo: str | None) -> None:
    """Refuse unless ``repo_id`` is a private dataset, and not the public traces dataset."""
    if public_repo and repo_id.lower() == public_repo.lower():
        raise BackupError("refusing: that is the public traces dataset")
    try:
        info = api.dataset_info(repo_id)
    except HfHubHTTPError as e:
        status = getattr(e.response, "status_code", "no response")
        raise BackupError(
            f"refusing: Hugging Face would not describe the dataset ({status})"
        ) from None
    if not getattr(info, "private", False):
        gated = getattr(info, "gated", False)
        raise BackupError(
            "refusing: the dataset is public"
            + (", even if gated" if gated else "")
            + "; private runs only go to a private dataset"
        )


def push(api: Hub, repo_id: str, backup: Backup, public_repo: str | None) -> None:
    check_target(api, repo_id, public_repo)
    operations = [
        CommitOperationAdd(path_in_repo=dest, path_or_fileobj=str(src))
        for dest, src in sorted(backup.files.items())
    ]
    try:
        api.create_commit(
            repo_id,
            operations,
            commit_message=f"Back up {len(backup.runs)} private runs",
            repo_type="dataset",
        )
    except (HfHubHTTPError, ValueError) as e:
        raise BackupError(f"Hugging Face refused the upload, nothing was committed: {e}") from None


def hub(token: str) -> Hub:
    return HfApi(token=token)
