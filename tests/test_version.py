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


def test_a_short_commit_matches_its_full_sha():
    full = "abc1234" + "d" * 33
    assert lamplight.same_commit("abc1234", full)
    assert lamplight.same_commit(full, "ABC1234")
    assert not lamplight.update_available("abc1234", full)
    assert lamplight.update_available("abc1234", "e" * 40)
    assert not lamplight.update_available("", "e" * 40)
    assert not lamplight.update_available("abc1234", "")


def test_upstream_commit_reads_a_raw_sha(monkeypatch):
    class Response:
        def read(self, _limit):
            return b"abcdef0\n"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(lamplight.urllib.request, "urlopen", lambda *a, **k: Response())
    lamplight.clear_upstream_cache()
    assert lamplight.upstream_commit() == "abcdef0"
    lamplight.clear_upstream_cache()


def test_upstream_commit_reads_a_json_sha(monkeypatch):
    class Response:
        def read(self, _limit):
            return b'{"sha": "1234abc"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(lamplight.urllib.request, "urlopen", lambda *a, **k: Response())
    lamplight.clear_upstream_cache()
    assert lamplight.upstream_commit() == "1234abc"
    lamplight.clear_upstream_cache()


def test_upstream_commit_is_blank_when_github_is_unreachable(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(lamplight.urllib.request, "urlopen", boom)
    lamplight.clear_upstream_cache()
    assert lamplight.upstream_commit() == ""
    lamplight.clear_upstream_cache()


def test_upstream_commit_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return "a" * 40

    monkeypatch.setattr(lamplight, "_fetch_upstream_commit", fake)
    lamplight.clear_upstream_cache()
    assert lamplight.upstream_commit() == "a" * 40
    assert lamplight.upstream_commit() == "a" * 40
    assert calls["n"] == 1
    lamplight.clear_upstream_cache()
