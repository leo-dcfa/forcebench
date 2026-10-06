"""Errors about the private pool never quote its files or print its path."""

import pytest

from forcebench.pool import PrivatePoolError, _main, init_private_dir, load_private_pool


SECRET = "SECRET-MARKER"


@pytest.fixture
def pool(tmp_path):
    root = tmp_path / f"pool-{SECRET}"  # the path itself must never be printed either
    root.mkdir()
    return init_private_dir(root)


def _message(root) -> str:
    with pytest.raises(PrivatePoolError) as err:
        load_private_pool(root)
    return str(err.value)


@pytest.mark.parametrize(
    ("file", "text", "says"),
    [
        ("pool.yaml", f"canary_guid: '{SECRET}\n  : [\n", "pool.yaml is not valid YAML at line"),
        ("pool.yaml", f"canary_guid: {SECRET}\n", "canary_guid: String should match pattern"),
        ("exposure.yaml", f"t: [{SECRET}\n", "exposure.yaml is not valid YAML at line"),
        (
            "exposure.yaml",
            f"t:\n  - party: x\n    kind: {SECRET}\n    date: 2026-09-30\n",
            "t: kind",
        ),
        ("exposure.yaml", f"t: {SECRET}\n", "the entry of t is not a list"),
    ],
)
def test_a_broken_pool_file_is_reported_without_quoting_it(pool, file, text, says):
    (pool.root / file).write_text(text)
    message = _message(pool.root)
    assert says in message
    assert SECRET not in message, "neither the file's text nor the pool's path"


def test_a_filesystem_error_preparing_mounts_does_not_print_the_path(pool, monkeypatch, capsys):
    monkeypatch.setenv("FORCEBENCH_PRIVATE_DIR", str(pool.root))
    monkeypatch.setattr("forcebench.models.load_dotenv", lambda *a, **k: None)

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied", str(pool.runs_dir))

    monkeypatch.setattr("pathlib.Path.mkdir", refuse)
    assert _main(["docker-args", "sandbox"]) == 1
    out = capsys.readouterr().out
    assert "Permission denied" in out and SECRET not in out
