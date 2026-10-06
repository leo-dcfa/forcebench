"""A task as a folder of plain files (forcebench.task_folder): unpack, edit, pack."""

import pytest

from forcebench.task_folder import INTRO, TaskFolderError, pack, render_reply, unpack
from forcebench.tasks import EVERY_STATUS, Task, all_tasks, load_suites


def _repack(folder, original: Task) -> Task:
    data = pack(folder)
    managed = {"canary": original.canary, "visibility": original.visibility}
    return Task.model_validate({**data, **managed, "status": original.status})


def test_every_public_task_survives_the_round_trip(tmp_path):
    for task in all_tasks(load_suites(statuses=EVERY_STATUS)):
        folder = tmp_path / task.id
        unpack(task, folder)
        assert _repack(folder, task).content_sha() == task.content_sha(), task.id


def test_a_files_reply_becomes_its_files(tmp_path, make_task):
    reply = render_reply({"force-app/main/default/classes/A.cls": "public class A {}"}, "Here.")
    task = make_task(
        {"format": "files", "files": ["force-app/main/default/classes/A.cls"]},
        reference_output=reply,
        negative_outputs=["Answer: not files at all"],
        grader={
            "type": "org_deploy",
            "hidden_files": {"force-app/main/default/classes/T.cls": "x\n"},
        },
    )
    unpack(task, tmp_path / "t")
    ref = tmp_path / "t" / "reference"
    assert (ref / "force-app/main/default/classes/A.cls").read_text() == "public class A {}\n"
    assert (ref / INTRO).read_text() == "Here.\n"
    assert (tmp_path / "t" / "negatives" / "1.md").read_text() == "Answer: not files at all"
    assert (tmp_path / "t" / "hidden" / "force-app/main/default/classes/T.cls").read_text() == "x\n"
    assert "hidden_files" not in (tmp_path / "t" / "task.yaml").read_text()


def test_editing_a_file_changes_the_reply(tmp_path, make_task):
    task = make_task(
        {"format": "files", "files": ["force-app/main/default/classes/A.cls"]},
        reference_output=render_reply({"force-app/main/default/classes/A.cls": "class A {}"}),
    )
    unpack(task, tmp_path / "t")
    negative = tmp_path / "t" / "negatives" / "1" / "force-app/main/default/classes/A.cls"
    negative.parent.mkdir(parents=True)
    negative.write_text("class A { void broken( }\n")
    data = pack(tmp_path / "t")
    assert data["negative_outputs"] == [
        "File: force-app/main/default/classes/A.cls\n```apex\nclass A { void broken( }\n```\n"
    ]


def test_a_folder_that_does_not_make_a_task_is_refused(tmp_path, make_task):
    task = make_task({"format": "text"})
    unpack(task, tmp_path / "t")
    with pytest.raises(TaskFolderError, match="already exists"):
        unpack(task, tmp_path / "t")
    (tmp_path / "t" / "negatives").mkdir()
    (tmp_path / "t" / "negatives" / "first.md").write_text("Answer: y")
    with pytest.raises(TaskFolderError, match="only 1/, 2/"):
        pack(tmp_path / "t")
    (tmp_path / "t" / "negatives" / "first.md").unlink()
    (tmp_path / "t" / "task.yaml").write_text("id: test-task\nstatus: ready\n")
    with pytest.raises(TaskFolderError, match="the task's own fields only"):
        pack(tmp_path / "t")


def test_paths_never_leave_the_folder(tmp_path, make_task):
    task = make_task({"format": "text"}, context_files={"../outside.txt": "x"})
    with pytest.raises(TaskFolderError, match="not a path"):
        unpack(task, tmp_path / "t")
    assert not (tmp_path / "outside.txt").exists()
