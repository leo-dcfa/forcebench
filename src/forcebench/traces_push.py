"""Pushing the traces dataset to Hugging Face: run by the maintainer, never by automation.

`forcebench traces push` reads the target from HF_DATASET_REPO and the token from HF_TOKEN (the
environment or .env; the token is never printed). Before uploading it asks Hugging Face what the
target is and refuses unless it is private, or public with gated access; it prints the state it
found. It also refuses a folder holding anything but the dataset's own files, or any record that
is not a public, canaried record of a public task. Needs the `traces` extra (huggingface_hub).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from huggingface_hub import HfApi
from huggingface_hub.errors import HfHubHTTPError

from forcebench import CANARY

_DATA_RE = re.compile(r"data/v[0-9][0-9.]*/[A-Za-z0-9._-]+\.jsonl")
# Never in a record: what would name a provider, an endpoint or a machine.
_FORBIDDEN_FIELDS = frozenset({"provider", "endpoint_model", "base_url", "request", "grader_orgs"})


class PushError(ValueError):
    """The push is refused: nothing was uploaded."""


class Hub(Protocol):
    def dataset_info(self, repo_id: str) -> Any: ...
    def upload_folder(
        self, *, repo_id: str, repo_type: str, folder_path: str, commit_message: str
    ) -> Any: ...


def repo_state(api: Hub, repo_id: str) -> tuple[bool, str]:
    """Whether the dataset may receive the traces, and its state in words. A dataset Hugging Face
    will not describe (missing, or the token cannot read it) refuses the push."""
    try:
        info = api.dataset_info(repo_id)
    except HfHubHTTPError as e:
        status = getattr(e.response, "status_code", "no response")
        raise PushError(f"refusing: Hugging Face would not describe {repo_id} ({status})") from None
    if getattr(info, "private", False):
        return True, "private"
    gated = getattr(info, "gated", False)
    if gated:
        return True, "public, gated" + (f" ({gated} approval)" if isinstance(gated, str) else "")
    return False, "public and not gated"


def check_folder(folder: Path, public_ids: set[str]) -> int:
    """The number of records in a built dataset folder, after checking it holds only README.md,
    LICENSE.md and data/v<version>/*.jsonl, and that every record carries the canary, names a
    public task and no provider or endpoint."""
    records = 0
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = path.relative_to(folder).as_posix()
        if rel in ("README.md", "LICENSE.md"):
            continue
        if not _DATA_RE.fullmatch(rel):
            raise PushError(f"refusing: {rel} is not part of the dataset")
        for n, line in enumerate(path.read_text().splitlines(), 1):
            rec = json.loads(line)
            if rec.get("canary") != CANARY:
                raise PushError(f"refusing: {rel} line {n} does not carry the canary")
            if rec.get("task_id") not in public_ids:
                raise PushError(f"refusing: {rel} line {n} is not a public task's answer")
            if _FORBIDDEN_FIELDS & set(rec):
                raise PushError(f"refusing: {rel} line {n} names a provider or an endpoint")
            records += 1
    if not records:
        raise PushError("refusing: the folder holds no records (build it first)")
    return records


def push(folder: Path, repo_id: str, public_ids: set[str], api: Hub, commit_message: str) -> int:
    """Upload a checked folder to a private or gated dataset; the number of records uploaded."""
    allowed, state = repo_state(api, repo_id)
    if not allowed:
        raise PushError(f"refusing: the dataset {repo_id} is {state}; make it private or gated")
    records = check_folder(folder, public_ids)
    api.upload_folder(
        repo_id=repo_id, repo_type="dataset", folder_path=str(folder), commit_message=commit_message
    )
    return records


def hub(token: str) -> Hub:
    return HfApi(token=token)
