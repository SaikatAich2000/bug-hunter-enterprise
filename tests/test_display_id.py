from app.schemas import display_id_for


def test_display_id_bug():
    assert display_id_for("Bug", 124) == "BUG-124"


def test_display_id_requirement():
    assert display_id_for("Requirement", 125) == "REQ-125"


def test_display_id_task():
    assert display_id_for("Task", 126) == "TASK-126"


def test_display_id_subtask():
    assert display_id_for("Sub-task", 127) == "SUB-127"


def test_display_id_story():
    assert display_id_for("Story", 128) == "USRSTR-128"


def test_display_id_epic():
    assert display_id_for("Epic", 129) == "EPIC-129"


def test_display_id_unknown():
    assert display_id_for("UnknownType", 1) == "ID-1"


def test_display_id_none_type():
    assert display_id_for(None, 5) == "ID-5"


def test_display_id_none_id():
    assert display_id_for("Bug", None) == ""
