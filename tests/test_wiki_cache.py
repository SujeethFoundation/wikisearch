"""Tests for the wiki cache: shared cold reads, the startup preload and the optional shared snapshot."""
import importlib
import json
import threading
import time
from types import SimpleNamespace

import azure.functions as func
import pytest

import function_app as fa


def make_docs(n=2, tag="a"):
    return [{"title": f"Page {i} {tag}", "url": f"https://github.com/o/r/wiki/P{i}", "source": "wiki",
             "path": f"P{i}.md", "text": f"Text of page {i} {tag}."} for i in range(n)]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    fa.cache.update({"time": 0, "docs": [], "index": None, "source": None})
    fa._last_failure.update(time=0.0, error=None)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "")
    yield
    # let any background thread finish before the next test touches the shared state
    for t in threading.enumerate():
        if t.name in ("wiki-refresh", "wiki-snapshot", "wiki-preload"):
            t.join(timeout=5)


class Reader:
    """A stand-in for read_wiki that counts calls and can be slow or fail."""

    def __init__(self, docs=None, delay=0.0, fail=False):
        self.docs, self.delay, self.fail, self.calls = docs or make_docs(), delay, fail, 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("GitHub is down")
        return self.docs


def wait_for(predicate, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- one shared read on a cold instance ----

def test_requests_on_a_cold_instance_share_one_github_read(monkeypatch):
    reader = Reader(delay=0.3)
    monkeypatch.setattr(fa, "read_wiki", reader)
    results = []
    threads = [threading.Thread(target=lambda: results.append(fa.documents())) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert reader.calls == 1 and len(results) == 8 and all(len(r) == 2 for r in results)


def test_preload_starts_the_read_and_early_requests_share_it(monkeypatch):
    reader = Reader(delay=0.4)
    monkeypatch.setattr(fa, "read_wiki", reader)
    pre = threading.Thread(target=fa._preload, name="wiki-preload")
    pre.start()
    time.sleep(0.05)
    results = []
    threads = [threading.Thread(target=lambda: results.append(fa.documents())) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    pre.join()
    assert reader.calls == 1 and all(len(r) == 2 for r in results)
    assert fa.cache["source"] == "github"


def test_a_failed_preload_does_not_raise_and_the_next_request_tries_again(monkeypatch):
    reader = Reader(fail=True)
    monkeypatch.setattr(fa, "read_wiki", reader)
    fa._preload()  # must not raise
    assert not fa.cache["docs"]
    reader.fail = False
    monkeypatch.setattr(fa, "time", SimpleNamespace(time=lambda: time.time() + 60, sleep=time.sleep))
    assert len(fa.documents()) == 2


def _reload_with(monkeypatch, value):
    created = []

    class Recorder(threading.Thread):
        def start(self):
            created.append(self.name)

    monkeypatch.setenv("PRELOAD_ON_START", value)
    monkeypatch.setattr(threading, "Thread", Recorder)
    importlib.reload(fa)
    monkeypatch.undo()
    return created


def test_preload_thread_starts_at_import_only_when_enabled(monkeypatch):
    try:
        assert "wiki-preload" in _reload_with(monkeypatch, "true")
        assert "wiki-preload" not in _reload_with(monkeypatch, "false")
    finally:
        monkeypatch.setenv("PRELOAD_ON_START", "false")
        importlib.reload(fa)


# ---- the optional shared snapshot (logic) ----

def test_cold_instance_loads_the_snapshot_and_does_not_read_github(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(fa, "read_wiki", reader)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "x")
    monkeypatch.setattr(fa, "_snapshot_load", lambda: (make_docs(3, "snap"), time.time() - 10))
    assert len(fa.documents()) == 3
    assert reader.calls == 0 and fa.cache["source"] == "snapshot"
    assert fa.rank("Page 1 snap")  # the index was built from the snapshot


def test_a_stale_snapshot_is_served_at_once_then_refreshed_in_the_background(monkeypatch):
    reader = Reader(docs=make_docs(2, "fresh"))
    saved = []
    monkeypatch.setattr(fa, "read_wiki", reader)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "x")
    monkeypatch.setattr(fa, "_snapshot_save", lambda docs, at: saved.append(docs))
    monkeypatch.setattr(fa, "_snapshot_load", lambda: (make_docs(3, "old"), time.time() - fa.CACHE_SECONDS - 100))
    first = fa.documents()
    assert len(first) == 3 and reader.calls == 0           # served without waiting for GitHub
    fa.documents()                                          # the next request starts the refresh
    assert wait_for(lambda: reader.calls == 1 and fa.cache["source"] == "github")
    assert wait_for(lambda: len(saved) == 1)
    assert fa.cache["docs"][0]["title"].endswith("fresh")


def test_a_read_from_github_saves_the_snapshot(monkeypatch):
    saved = []
    monkeypatch.setattr(fa, "read_wiki", Reader(docs=make_docs(2, "g")))
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "x")
    monkeypatch.setattr(fa, "_snapshot_load", lambda: None)
    monkeypatch.setattr(fa, "_snapshot_save", lambda docs, at: saved.append((docs, at)))
    fa.documents()
    assert wait_for(lambda: len(saved) == 1)
    assert saved[0][0][0]["title"].endswith("g") and abs(saved[0][1] - time.time()) < 5


def test_without_a_usable_snapshot_it_reads_github_as_before(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(fa, "read_wiki", reader)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "x")
    monkeypatch.setattr(fa, "_snapshot_load", lambda: None)
    monkeypatch.setattr(fa, "_snapshot_save", lambda docs, at: None)
    assert len(fa.documents()) == 2 and reader.calls == 1 and fa.cache["source"] == "github"


def test_a_forced_refresh_reads_github_even_when_a_snapshot_exists(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(fa, "read_wiki", reader)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "x")
    monkeypatch.setattr(fa, "_snapshot_load", lambda: (make_docs(5), time.time()))
    monkeypatch.setattr(fa, "_snapshot_save", lambda docs, at: None)
    assert len(fa.documents(force=True)) == 2 and reader.calls == 1


def test_github_failing_with_a_snapshot_available_still_serves_pages(monkeypatch):
    reader = Reader(fail=True)
    monkeypatch.setattr(fa, "read_wiki", reader)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "x")
    monkeypatch.setattr(fa, "_snapshot_load", lambda: (make_docs(4, "s"), time.time() - 5))
    assert len(fa.documents()) == 4 and reader.calls == 0


def test_snapshot_is_off_when_not_configured():
    assert fa._snapshot_enabled() is False
    assert fa._snapshot_load() is None
    fa._snapshot_save(make_docs(), time.time())  # does nothing, does not raise


def test_snapshot_validation():
    assert fa._valid_docs(make_docs())
    assert not fa._valid_docs([]) and not fa._valid_docs(None) and not fa._valid_docs("x")
    assert not fa._valid_docs([{"title": "t"}])
    bad = make_docs(); bad[0]["text"] = None
    assert not fa._valid_docs(bad)


# ---- the optional shared snapshot (storage calls, with a fake client) ----

class FakeBlobClient:
    payload = b""
    error = None

    @classmethod
    def from_connection_string(cls, conn, container, blob):
        cls.seen = (conn, container, blob)
        return cls()

    def download_blob(self, timeout=None):
        if FakeBlobClient.error:
            raise FakeBlobClient.error
        return SimpleNamespace(readall=lambda: FakeBlobClient.payload)


class FakeContainer:
    uploads, created = [], 0

    @classmethod
    def from_connection_string(cls, conn, container):
        return cls()

    def create_container(self):
        FakeContainer.created += 1

    def upload_blob(self, name, data, overwrite=False, timeout=None):
        FakeContainer.uploads.append((name, json.loads(data), overwrite))


@pytest.fixture
def fake_storage(monkeypatch):
    import azure.storage.blob as blob
    monkeypatch.setattr(blob, "BlobClient", FakeBlobClient)
    monkeypatch.setattr(blob, "ContainerClient", FakeContainer)
    monkeypatch.setattr(fa, "SNAPSHOT_CONNECTION", "UseDevelopmentStorage=true")
    FakeBlobClient.error, FakeBlobClient.payload = None, b""
    FakeContainer.uploads, FakeContainer.created = [], 0


def test_load_reads_a_valid_snapshot(fake_storage):
    FakeBlobClient.payload = json.dumps({"saved_at": 1234.5, "docs": make_docs(2)}).encode()
    docs, saved_at = fa._snapshot_load()
    assert len(docs) == 2 and saved_at == 1234.5
    assert FakeBlobClient.seen[1:] == ("wiki-cache", "wiki-snapshot.json")


@pytest.mark.parametrize("payload", [b"not json", b"{}", json.dumps({"saved_at": 1, "docs": []}).encode(),
                                      json.dumps({"saved_at": "x", "docs": make_docs()}).encode()])
def test_load_ignores_a_bad_snapshot(fake_storage, payload):
    FakeBlobClient.payload = payload
    assert fa._snapshot_load() is None


def test_load_returns_none_when_storage_fails(fake_storage):
    FakeBlobClient.error = RuntimeError("blob not found")
    assert fa._snapshot_load() is None


def test_save_creates_the_container_and_overwrites_the_blob(fake_storage):
    fa._snapshot_save(make_docs(2), 99.0)
    name, data, overwrite = FakeContainer.uploads[0]
    assert name == "wiki-snapshot.json" and overwrite is True
    assert data["saved_at"] == 99.0 and len(data["docs"]) == 2 and FakeContainer.created == 1


def test_save_never_raises(fake_storage, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("storage down")
    monkeypatch.setattr(FakeContainer, "upload_blob", boom)
    fa._snapshot_save(make_docs(), 1.0)  # logged, not raised


# ---- health ----

def test_health_reports_where_the_copy_came_from(monkeypatch):
    monkeypatch.setattr(fa, "read_wiki", Reader())
    fa.documents()
    req = func.HttpRequest("GET", "/api/health", headers={}, body=b"")
    body = json.loads(fa.health._function._func(req).get_body())
    assert body["cache_loaded"] is True and body["loaded_from"] == "github" and body["snapshot_enabled"] is False
