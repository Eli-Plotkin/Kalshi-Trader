"""Object-store abstraction used by the ledger replication (docs/design-replication.md section 3).

Three implementations:

* ``LocalDirStore``  a directory standing in for a bucket (tests, local dev, REPLICATION_LOCAL_DIR).
* ``S3Store``        boto3 against any S3-compatible endpoint (AWS S3, Cloudflare R2, Backblaze B2, MinIO).
                     ``boto3`` is an OPTIONAL import: it is only needed when no ``client`` is injected.
* ``store_from_env`` builds one of the above from REPLICATION_* environment variables.

Contract for every implementation:

* methods RAISE on transport/IO errors, so a caller can tell "the key does not exist"
  (``get_bytes`` -> ``None``, ``get_file`` -> ``False``) from "I could not ask";
* ``put_bytes``/``put_file`` are atomic from a reader's point of view: a concurrent reader sees the
  complete old object or the complete new one, never a partial write.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024

DEFAULT_PREFIX = "kalshi-bot/"      # used whenever a *_PREFIX variable is empty/blank/missing
MAX_CONTROL_BYTES = 4096            # control/pause.json, control/ack.json: tiny by construction
MAX_MANIFEST_BYTES = 65536          # latest.json and seq-floor.json: a few hundred bytes in practice


class ObjectTooLarge(ValueError):
    """A bounded read found an object bigger than the caller's ``max_bytes`` (nothing beyond
    ``max_bytes + 1`` bytes is ever read into memory)."""
_TMP_MARK = ".tmp-"      # LocalDirStore temp files are named ".<name>.tmp-<uuid>" and never listed


def _check_key(key):
    if not isinstance(key, str) or not key:
        raise ValueError(f"invalid store key: {key!r}")
    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError(f"invalid store key: {key!r}")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"invalid store key: {key!r}")
    return key


class Store:
    """Abstract object store. See module docstring for the error contract."""

    def put_file(self, key: str, path: str) -> None:
        raise NotImplementedError

    def put_bytes(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get_bytes(self, key: str, max_bytes: Optional[int] = None) -> Optional[bytes]:
        """Object bytes, or None ONLY if the key does not exist. Any other failure raises.
        With ``max_bytes`` an object larger than that raises ``ObjectTooLarge`` (and at most
        ``max_bytes + 1`` bytes are ever read)."""
        raise NotImplementedError

    def get_file(self, key: str, dest_path: str) -> bool:
        """Download to dest_path (atomically: no partial file at dest_path). False ONLY if the key
        does not exist. Any other failure raises and leaves dest_path untouched."""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        """Delete a key; deleting a missing key is not an error."""
        raise NotImplementedError

    def list_keys(self, prefix: str) -> list:
        """All keys that start with ``prefix`` (sorted). Keys are relative to the store root."""
        raise NotImplementedError


# --- local directory ---------------------------------------------------------------------------

class LocalDirStore(Store):
    def __init__(self, root):
        self.root = os.path.abspath(str(root))
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        return os.path.join(self.root, *_check_key(key).split("/"))

    @staticmethod
    def _tmp_for(path):
        d, name = os.path.split(path)
        return os.path.join(d, f".{name}{_TMP_MARK}{uuid.uuid4().hex}")

    def put_bytes(self, key, data):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = self._tmp_for(path)
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def put_file(self, key, path):
        dest = self._path(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = self._tmp_for(dest)
        try:
            shutil.copyfile(path, tmp)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get_bytes(self, key, max_bytes=None):
        try:
            with open(self._path(key), "rb") as fh:
                if max_bytes is None:
                    return fh.read()
                if os.fstat(fh.fileno()).st_size > max_bytes:
                    raise ObjectTooLarge(f"{key} is larger than {max_bytes} bytes")
                data = fh.read(max_bytes + 1)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        if len(data) > max_bytes:               # grew while we were reading it
            raise ObjectTooLarge(f"{key} is larger than {max_bytes} bytes")
        return data

    def get_file(self, key, dest_path):
        src = self._path(key)
        if not os.path.isfile(src):
            return False
        tmp = self._tmp_for(os.path.abspath(dest_path))
        try:
            try:
                shutil.copyfile(src, tmp)
            except FileNotFoundError:
                return False
            os.replace(tmp, dest_path)
            return True
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, key):
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass

    def list_keys(self, prefix):
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if name.startswith(".") and _TMP_MARK in name:
                    continue                      # an in-flight write, not an object
                rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                key = rel.replace(os.sep, "/")
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)


# --- S3 / S3-compatible --------------------------------------------------------------------------

_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound", "NoSuchBucketKey"}


def _is_not_found(exc):
    """True for botocore's ClientError describing a missing key (duck-typed: no botocore import)."""
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    err = resp.get("Error") or {}
    code = str(err.get("Code", ""))
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in _NOT_FOUND_CODES or status == 404


class S3Store(Store):
    def __init__(self, bucket, prefix="", endpoint_url=None, region=None,
                 access_key_id=None, secret_access_key=None, client=None):
        if not bucket:
            raise ValueError("S3Store needs a bucket name")
        self.bucket = bucket
        prefix = (prefix or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self.prefix = prefix
        if client is None:
            try:
                import boto3                      # optional dependency
            except ImportError as exc:
                raise RuntimeError(
                    "S3Store needs the 'boto3' package (pip install 'boto3>=1.34'); "
                    "it is not installed and no client was injected") from exc
            kwargs = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            if region:
                kwargs["region_name"] = region
            if access_key_id and secret_access_key:
                kwargs["aws_access_key_id"] = access_key_id
                kwargs["aws_secret_access_key"] = secret_access_key
            client = boto3.client("s3", **kwargs)
        self.client = client

    def _k(self, key):
        return self.prefix + _check_key(key)

    def put_bytes(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=self._k(key), Body=bytes(data))

    def put_file(self, key, path):
        # upload_file does multipart for big files; an S3 PUT/multipart is atomic to readers.
        self.client.upload_file(str(path), self.bucket, self._k(key))

    def _get_object(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._k(key))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise

    def get_bytes(self, key, max_bytes=None):
        obj = self._get_object(key)
        if obj is None:
            return None
        body = obj["Body"]
        try:
            if max_bytes is None:
                return body.read()
            length = obj.get("ContentLength")
            if isinstance(length, int) and length > max_bytes:
                raise ObjectTooLarge(f"{key} is {length} bytes, larger than {max_bytes}")
            data = body.read(max_bytes + 1)          # never more than cap+1 bytes in memory
            if len(data) > max_bytes:
                raise ObjectTooLarge(f"{key} is larger than {max_bytes} bytes")
            return data
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                try:
                    close()                           # abandon the rest of the stream
                except Exception:
                    pass

    def get_file(self, key, dest_path):
        obj = self._get_object(key)
        if obj is None:
            return False
        dest_path = os.path.abspath(dest_path)
        tmp = os.path.join(os.path.dirname(dest_path),
                           f".{os.path.basename(dest_path)}{_TMP_MARK}{uuid.uuid4().hex}")
        body = obj["Body"]
        try:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = body.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
            os.replace(tmp, dest_path)
            return True
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=self._k(key))

    def list_keys(self, prefix):
        full = self.prefix + prefix
        out = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": full}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            for item in resp.get("Contents") or []:
                k = item["Key"]
                if k.startswith(self.prefix):
                    out.append(k[len(self.prefix):])
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
                if not token:
                    break
            else:
                break
        return sorted(out)


# --- environment ---------------------------------------------------------------------------------

def store_from_env(prefix_env="REPLICATION", env=None):
    """Build the store described by ``<prefix_env>_*`` variables (docs/design-replication.md section 8).

    Returns ``None`` when replication is not configured (no ``<P>_LOCAL_DIR`` and no ``<P>_BUCKET``);
    callers treat None as "replication disabled".
    """
    env = os.environ if env is None else env

    def get(name, default=""):
        return (env.get(f"{prefix_env}_{name}", default) or "").strip()

    local_dir = get("LOCAL_DIR")
    if local_dir:
        return LocalDirStore(local_dir)
    bucket = get("BUCKET")
    if not bucket:
        return None
    prefix = get("PREFIX")
    if not prefix.strip("/ "):                        # empty / blank / "/" -> the default, never "no prefix"
        prefix = DEFAULT_PREFIX
    return S3Store(
        bucket,
        prefix=prefix,
        endpoint_url=get("ENDPOINT") or None,
        region=get("REGION") or None,
        access_key_id=get("ACCESS_KEY_ID") or None,
        secret_access_key=get("SECRET_ACCESS_KEY") or None,
    )
