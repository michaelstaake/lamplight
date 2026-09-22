import sqlite3
import time

from lamplight import db
from lamplight.jobs import FAILED, ID_ALPHABET, SUCCESS, JobRunner, JobStore


def make_store(tmp_path, **kwargs):
    return JobStore(db.connect(tmp_path / "jobs.sqlite3"), **kwargs)


def test_buffered_log_is_visible_before_it_is_flushed(tmp_path):
    store = make_store(tmp_path)
    job_id = store.create("apache", "install")
    store.append_log(job_id, "line one\n")
    assert store.get(job_id)["log"] == "line one\n"


def test_log_survives_the_flush(tmp_path):
    store = make_store(tmp_path)
    job_id = store.create("apache", "install")
    for i in range(500):
        store.append_log(job_id, f"line {i}\n")
    store.finish(job_id, SUCCESS)
    log = store.get(job_id)["log"]
    assert log.startswith("line 0\n")
    assert log.endswith("line 499\n")
    assert log.count("\n") == 500


def test_history_is_capped(tmp_path):
    store = make_store(tmp_path, history=3)
    created = [store.create("apache", "install") for _ in range(6)]
    assert [job["id"] for job in store.list()] == created[:2:-1]


def test_ids_are_random_and_leave_out_the_lookalike_characters(tmp_path):
    store = make_store(tmp_path, history=50)
    ids = {store.create("apache", "install") for _ in range(50)}
    assert len(ids) == 50, "ids must not repeat"
    for job_id in ids:
        assert len(job_id) == 6
        assert set(job_id) <= set(ID_ALPHABET)
        assert "0" not in job_id and "o" not in job_id


def test_old_integer_ids_are_migrated_to_random_ones(tmp_path):
    """A 0.2 database keeps its rows; only the ids change."""
    path = tmp_path / "jobs.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE jobs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, component_id TEXT NOT NULL,"
        " action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',"
        " log TEXT NOT NULL DEFAULT '', error TEXT,"
        " created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',"
        " started_at TEXT, finished_at TEXT);"
    )
    legacy.execute(
        "INSERT INTO jobs (component_id, action, status, log) VALUES ('apache', 'install',"
        " 'success', 'old output')"
    )
    legacy.commit()
    legacy.close()

    store = JobStore(db.connect(path))
    rows = store.list()
    assert len(rows) == 1
    assert rows[0]["component_id"] == "apache"
    assert set(rows[0]["id"]) <= set(ID_ALPHABET)
    assert store.get(rows[0]["id"])["log"] == "old output"


def test_finish_records_the_error(tmp_path):
    store = make_store(tmp_path)
    job_id = store.create("apache", "install")
    store.finish(job_id, FAILED, "apt-get exited 100")
    job = store.get(job_id)
    assert job["status"] == FAILED
    assert job["error"] == "apt-get exited 100"
    assert job["finished_at"]


def test_runner_accepts_one_job_at_a_time():
    runner = JobRunner()
    release = []
    assert runner.submit("aaaaaa", lambda: (time.sleep(0.3), release.append(1)))
    assert runner.busy
    assert runner.submit("bbbbbb", lambda: release.append(2)) is False
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.05)
    assert release == [1]
    assert runner.current_id is None


def test_runner_frees_the_slot_after_a_crash(caplog):
    runner = JobRunner()

    def boom():
        raise RuntimeError("nope")

    assert runner.submit("aaaaaa", boom)
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.05)
    assert not runner.busy
    assert runner.last_error == "job aaaaaa crashed"
    assert "crashed outside its own error handling" in caplog.text
    assert runner.submit("bbbbbb", lambda: None)
