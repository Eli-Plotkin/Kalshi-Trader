"""Store implementations: LocalDirStore atomicity, S3Store key/prefix handling and error propagation
(against a fake boto3 client -- real S3/R2/B2 behaviour is NOT exercised here)."""

import io
import os
import sys
import threading

import pytest

from nba_trading.replication.store import LocalDirStore, S3Store, Store, store_from_env


# --- fake boto3 client -------------------------------------------------------------------------

class FakeClientError(Exception):
    """Shaped like botocore.exceptions.ClientError."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class _Body:
    def __init__(self, data):
        self._io = io.BytesIO(data)

    def read(self, n=-1):
        return self._io.read(n)


class FakeS3Client:
    """In-memory stand-in for boto3.client('s3'). `fail` maps an op name to an exception to raise."""

    def __init__(self, page_size=1000):
        self.objects = {}
        self.calls = []
        self.fail = {}
        self.page_size = page_size

    def _maybe_fail(self, op):
        self.calls.append(op)
        if op in self.fail:
            raise self.fail[op]

    def put_object(self, Bucket, Key, Body):
        self._maybe_fail("put_object")
        self.objects[(Bucket, Key)] = bytes(Body)

    def upload_file(self, Filename, Bucket, Key):
        self._maybe_fail("upload_file")
        with open(Filename, "rb") as fh:
            self.objects[(Bucket, Key)] = fh.read()

    def get_object(self, Bucket, Key):
        self._maybe_fail("get_object")
        if (Bucket, Key) not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self._maybe_fail("delete_object")
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
        self._maybe_fail("list_objects_v2")
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start:start + self.page_size]
        resp = {"Contents": [{"Key": k} for k in page]}
        if start + self.page_size < len(keys):
            resp["IsTruncated"] = True
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp


# --- LocalDirStore -------------------------------------------------------------------------------

def test_local_roundtrip_and_missing(tmp_path):
    s = LocalDirStore(tmp_path / "bucket")
    assert s.get_bytes("a/b.txt") is None
    s.put_bytes("a/b.txt", b"hello")
    assert s.get_bytes("a/b.txt") == b"hello"
    dest = tmp_path / "out.bin"
    assert s.get_file("a/b.txt", str(dest)) is True
    assert dest.read_bytes() == b"hello"
    assert s.get_file("nope", str(tmp_path / "x")) is False
    assert not (tmp_path / "x").exists()


def test_local_put_file_list_delete(tmp_path):
    s = LocalDirStore(tmp_path / "bucket")
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    s.put_file("snapshots/1.sqlite", str(src))
    s.put_bytes("snapshots/2.sqlite", b"x")
    s.put_bytes("latest.json", b"{}")
    assert s.list_keys("snapshots/") == ["snapshots/1.sqlite", "snapshots/2.sqlite"]
    assert s.list_keys("") == ["latest.json", "snapshots/1.sqlite", "snapshots/2.sqlite"]
    s.delete("snapshots/1.sqlite")
    s.delete("snapshots/1.sqlite")          # deleting a missing key is not an error
    assert s.list_keys("snapshots/") == ["snapshots/2.sqlite"]


@pytest.mark.parametrize("bad", ["", "/abs", "../x", "a/../b", "a//b", "a\\b"])
def test_local_rejects_bad_keys(tmp_path, bad):
    s = LocalDirStore(tmp_path)
    with pytest.raises(ValueError):
        s.put_bytes(bad, b"x")


def test_local_put_is_atomic_failed_write_leaves_old_value_and_no_partial(tmp_path, monkeypatch):
    s = LocalDirStore(tmp_path / "b")
    s.put_bytes("latest.json", b"OLD")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        s.put_bytes("latest.json", b"NEW" * 1000)
    monkeypatch.undo()
    assert s.get_bytes("latest.json") == b"OLD"
    assert [n for n in os.listdir(tmp_path / "b")] == ["latest.json"]    # temp file cleaned up


def test_local_readers_never_see_partial_files(tmp_path):
    s = LocalDirStore(tmp_path / "b")
    a, b = b"A" * 200_000, b"B" * 200_000
    s.put_bytes("k", a)
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            data = s.get_bytes("k")
            if data not in (a, b):
                bad.append(len(data or b""))
            for key in s.list_keys(""):
                if ".tmp-" in key:
                    bad.append(key)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(150):
        s.put_bytes("k", b if i % 2 == 0 else a)
    stop.set()
    t.join()
    assert bad == []


def test_local_get_file_failure_leaves_destination_untouched(tmp_path, monkeypatch):
    s = LocalDirStore(tmp_path / "b")
    s.put_bytes("k", b"new")
    dest = tmp_path / "dest"
    dest.write_bytes(b"old")
    import shutil
    monkeypatch.setattr(shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("io")))
    with pytest.raises(OSError):
        s.get_file("k", str(dest))
    assert dest.read_bytes() == b"old"
    assert sorted(os.listdir(tmp_path)) == ["b", "dest"]


# --- S3Store -------------------------------------------------------------------------------------

def test_s3_key_prefix_handling():
    c = FakeS3Client()
    s = S3Store("bkt", prefix="kalshi-bot", client=c)         # trailing slash added
    s.put_bytes("latest.json", b"{}")
    assert ("bkt", "kalshi-bot/latest.json") in c.objects
    s2 = S3Store("bkt", prefix="/p/q/", client=c)
    s2.put_bytes("x", b"1")
    assert ("bkt", "p/q/x") in c.objects
    s3 = S3Store("bkt", client=c)
    s3.put_bytes("y", b"1")
    assert ("bkt", "y") in c.objects


def test_s3_roundtrip_list_strips_prefix_and_paginates(tmp_path):
    c = FakeS3Client(page_size=2)
    s = S3Store("bkt", prefix="pre", client=c)
    other = S3Store("bkt", prefix="other", client=c)
    for i in range(5):
        s.put_bytes(f"snapshots/{i}.sqlite", b"x")
    other.put_bytes("snapshots/9.sqlite", b"x")
    assert s.list_keys("snapshots/") == [f"snapshots/{i}.sqlite" for i in range(5)]
    src = tmp_path / "f"
    src.write_bytes(b"payload")
    s.put_file("f", str(src))
    assert s.get_bytes("f") == b"payload"
    dest = tmp_path / "d"
    assert s.get_file("f", str(dest)) is True and dest.read_bytes() == b"payload"
    s.delete("f")
    assert s.get_bytes("f") is None


def test_s3_missing_key_is_none_false_not_error(tmp_path):
    s = S3Store("bkt", client=FakeS3Client())
    assert s.get_bytes("nope") is None
    assert s.get_file("nope", str(tmp_path / "d")) is False
    assert not (tmp_path / "d").exists()


def test_s3_other_errors_propagate(tmp_path):
    c = FakeS3Client()
    s = S3Store("bkt", client=c)
    s.put_bytes("k", b"v")
    c.fail["get_object"] = FakeClientError("AccessDenied", 403)
    with pytest.raises(FakeClientError):
        s.get_bytes("k")
    with pytest.raises(FakeClientError):
        s.get_file("k", str(tmp_path / "d"))
    c.fail["get_object"] = ConnectionError("network down")
    with pytest.raises(ConnectionError):
        s.get_bytes("k")
    c.fail = {"put_object": TimeoutError("t/o"), "upload_file": OSError("x"),
              "delete_object": FakeClientError("InternalError", 500),
              "list_objects_v2": ConnectionError("nope")}
    with pytest.raises(TimeoutError):
        s.put_bytes("k", b"v")
    with pytest.raises(OSError):
        s.put_file("k", str(tmp_path))
    with pytest.raises(FakeClientError):
        s.delete("k")
    with pytest.raises(ConnectionError):
        s.list_keys("")


def test_s3_get_file_failure_midstream_leaves_no_partial_file(tmp_path):
    class Exploding(_Body):
        def __init__(self):
            super().__init__(b"abc")
            self.n = 0

        def read(self, n=-1):
            self.n += 1
            if self.n > 1:
                raise ConnectionError("reset")
            return b"abc"

    c = FakeS3Client()
    c.get_object = lambda Bucket, Key: {"Body": Exploding()}
    s = S3Store("bkt", client=c)
    dest = tmp_path / "d"
    with pytest.raises(ConnectionError):
        s.get_file("k", str(dest))
    assert os.listdir(tmp_path) == []


def test_s3_without_boto3_and_no_client_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)          # makes `import boto3` raise ImportError
    with pytest.raises(RuntimeError, match="boto3"):
        S3Store("bkt")


def test_s3_needs_bucket():
    with pytest.raises(ValueError):
        S3Store("", client=FakeS3Client())


def test_s3_builds_boto3_client_from_settings(monkeypatch):
    seen = {}

    class FakeBoto3:
        @staticmethod
        def client(name, **kw):
            seen["name"], seen["kw"] = name, kw
            return FakeS3Client()

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3)
    S3Store("b", endpoint_url="https://x.r2", region="auto", access_key_id="AK", secret_access_key="SK")
    assert seen["name"] == "s3"
    assert seen["kw"] == {"endpoint_url": "https://x.r2", "region_name": "auto",
                          "aws_access_key_id": "AK", "aws_secret_access_key": "SK"}


# --- store_from_env ------------------------------------------------------------------------------

def test_store_from_env_disabled_local_and_s3(tmp_path, monkeypatch):
    assert store_from_env(env={}) is None
    s = store_from_env(env={"REPLICATION_LOCAL_DIR": str(tmp_path / "b")})
    assert isinstance(s, LocalDirStore)

    captured = {}

    class FakeBoto3:
        @staticmethod
        def client(name, **kw):
            captured.update(kw)
            return FakeS3Client()

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3)
    s = store_from_env(env={"REPLICATION_BUCKET": "bkt", "REPLICATION_ENDPOINT": "https://e",
                            "REPLICATION_REGION": "auto", "REPLICATION_ACCESS_KEY_ID": "AK",
                            "REPLICATION_SECRET_ACCESS_KEY": "SK"})
    assert isinstance(s, S3Store) and s.bucket == "bkt" and s.prefix == "kalshi-bot/"
    assert captured["endpoint_url"] == "https://e"
    s = store_from_env(env={"REPLICATION_BUCKET": "bkt", "REPLICATION_PREFIX": "x/y"})
    assert s.prefix == "x/y/"
    s = store_from_env("CTRL", env={"CTRL_BUCKET": "bkt"})
    assert s.bucket == "bkt"


def test_store_is_abstract():
    with pytest.raises(NotImplementedError):
        Store().get_bytes("k")
