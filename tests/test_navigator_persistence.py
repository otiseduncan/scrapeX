from pathlib import Path

from scrapex.db import Store


def test_create_and_read_navigator_task(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task(
        "alldata", {"year": 2023, "make": "Toyota"}, "blind spot calibration", 50
    )
    task = store.navigator_task(task_id)
    assert task["provider"] == "alldata"
    assert task["target"] == {"year": 2023, "make": "Toyota"}
    assert task["topic"] == "blind spot calibration"
    assert task["state"] == "pending"
    assert task["step_count"] == 0
    assert task["action_budget"] == 50
    assert task["verified"] is False


def test_navigator_task_not_found_returns_none(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    assert store.navigator_task("does-not-exist") is None


def test_append_step_advances_count_and_caches_observation(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task("alldata", {}, "topic", 50)
    ordinal = store.append_navigator_step(
        task_id, {"action": "click", "ref": "e0"}, {"url": "https://x/1", "elements": []}
    )
    assert ordinal == 1
    task = store.navigator_task(task_id)
    assert task["step_count"] == 1
    assert task["last_observation"] == {"url": "https://x/1", "elements": []}

    ordinal2 = store.append_navigator_step(
        task_id, {"action": "click", "ref": "e1"}, {"url": "https://x/2", "elements": []}
    )
    assert ordinal2 == 2

    steps = store.navigator_task_steps(task_id)
    assert [s["ordinal"] for s in steps] == [1, 2]
    assert steps[0]["action"] == {"action": "click", "ref": "e0"}


def test_cache_observation_updates_last_observation_without_advancing_step_count(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task("alldata", {}, "topic", 50)
    store.cache_navigator_observation(task_id, {"url": "https://x/1", "elements": []})
    task = store.navigator_task(task_id)
    assert task["last_observation"] == {"url": "https://x/1", "elements": []}
    assert task["step_count"] == 0
    assert store.navigator_task_steps(task_id) == []


def test_append_step_unknown_task_raises(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    try:
        store.append_navigator_step("missing", {}, {})
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_set_state_and_save_verification(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task("alldata", {}, "topic", 50)
    store.set_navigator_task_state(task_id, "active", graph={"history": ["fp1"]})
    task = store.navigator_task(task_id)
    assert task["state"] == "active"
    assert task["graph"] == {"history": ["fp1"]}

    store.save_navigator_verification(task_id, {"verified": True, "reason": None})
    task = store.navigator_task(task_id)
    assert task["verified"] is True
    assert task["state"] == "verified"  # verified=True promotes state automatically

    store.save_navigator_verification(task_id, {"verified": False, "reason": "drift"})
    task = store.navigator_task(task_id)
    assert task["verified"] is False
    assert task["state"] == "verified"  # state itself is a separate, explicit transition


def test_restart_recovery_pauses_active_navigator_tasks(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task("alldata", {}, "topic", 50)
    store.set_navigator_task_state(task_id, "active")
    store.recover_after_restart()
    task = store.navigator_task(task_id)
    assert task["state"] == "paused"
    assert task["last_error"]


def test_restart_recovery_leaves_terminal_states_alone(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task("alldata", {}, "topic", 50)
    store.set_navigator_task_state(task_id, "verified")
    store.recover_after_restart()
    task = store.navigator_task(task_id)
    assert task["state"] == "verified"
