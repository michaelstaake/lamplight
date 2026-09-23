import lamplight


def _reset_commit():
    lamplight.commit_id.cache_clear()


def test_a_stamped_commit_is_shown_with_the_version(monkeypatch):
    monkeypatch.setattr(lamplight, "_stamped_commit", lambda: "abc1234")
    monkeypatch.setattr(lamplight, "_checkout_commit", lambda: "ignored")
    _reset_commit()
    assert lamplight.display_version() == "1.6.abc1234"
    _reset_commit()


def test_a_checkout_commit_is_used_when_nothing_was_stamped(monkeypatch):
    monkeypatch.setattr(lamplight, "_stamped_commit", lambda: "")
    monkeypatch.setattr(lamplight, "_checkout_commit", lambda: "def5678")
    _reset_commit()
    assert lamplight.commit_id() == "def5678"
    _reset_commit()


def test_an_unknown_commit_leaves_the_package_version(monkeypatch):
    monkeypatch.setattr(lamplight, "_stamped_commit", lambda: "")
    monkeypatch.setattr(lamplight, "_checkout_commit", lambda: "")
    _reset_commit()
    assert lamplight.display_version() == lamplight.__version__
    _reset_commit()


def test_a_stamp_must_be_a_commit(tmp_path, monkeypatch):
    stamp = tmp_path / "REVISION"
    monkeypatch.setattr(lamplight, "_STAMP", stamp)
    stamp.write_text("v1.5\n", encoding="utf-8")
    assert lamplight._stamped_commit() == ""
    stamp.write_text("abc1234\n", encoding="utf-8")
    assert lamplight._stamped_commit() == "abc1234"


def test_this_checkout_reports_a_short_commit():
    _reset_commit()
    short = lamplight.commit_id()
    assert len(short) >= 7
    assert lamplight.display_version() == f"{lamplight.__version__}.{short}"
    _reset_commit()
