#!/usr/bin/env python3
"""Prepare the immutable local model; this is the sole network download path."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REVISION = "a29b04bd15381511a9af671baec01072039215e3"
MODEL_DIRECTORY = Path(f"/models/medium.en-{REVISION}")
MANIFEST_NAME = "whisper-model-manifest.json"
EXPECTED_LOCK = {
    "schema": "whisper-model-lock/v1", "provider": "faster-whisper",
    "provider_version": "1.2.1", "model": "medium.en",
    "model_repository": "Systran/faster-whisper-medium.en", "model_revision": REVISION,
    "compute_type": "int8",
    "required_files": ["config.json", "model.bin", "tokenizer.json", "vocabulary.json"],
}


def cache_model(lock_path=ROOT / "whisper-model.lock.json", snapshot_dir=MODEL_DIRECTORY, *, snapshot_download=None):
    """Download exact revision and atomically inventory every snapshot file."""
    lock_bytes = Path(lock_path).read_bytes()
    lock = json.loads(lock_bytes)
    if lock != EXPECTED_LOCK or not re.fullmatch(r"[a-f0-9]{40}", lock["model_revision"]):
        raise ValueError("invalid immutable model lock")
    snapshot = Path(snapshot_dir).resolve()
    if snapshot_download is None:
        from huggingface_hub import snapshot_download
    downloaded = Path(snapshot_download(repo_id=lock["model_repository"],
                                        revision=lock["model_revision"], local_dir=str(snapshot))).resolve()
    if downloaded != snapshot or not snapshot.is_dir():
        raise ValueError("unexpected model snapshot")
    for name in lock["required_files"]:
        path = snapshot / name
        if not path.is_file() or not path.resolve().is_relative_to(snapshot):
            raise ValueError("missing required model file")
    files = {}
    for directory, directories, names in os.walk(snapshot, followlinks=False):
        for name in sorted(directories + names):
            path = Path(directory) / name
            relative = path.relative_to(snapshot).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                # Internal links may refer only to regular files; never follow
                # directory links, which could create cycles or hide files.
                target = path.resolve(strict=True)
                if not target.is_relative_to(snapshot) or not target.is_file():
                    raise ValueError("unsafe model snapshot link")
            elif stat.S_ISDIR(mode):
                continue
            elif not stat.S_ISREG(mode):
                raise ValueError("unexpected model snapshot file type")
            if relative == MANIFEST_NAME:
                continue
            digest = sha256()
            size = 0
            with path.open("rb") as content:
                for chunk in iter(lambda: content.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            files[relative] = {"bytes": size, "sha256": digest.hexdigest()}
    manifest = {"schema": "whisper-model-manifest/v1", "model_revision": lock["model_revision"],
                "lock_sha256": sha256(lock_bytes).hexdigest(), "files": files}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=snapshot, prefix=".whisper-manifest-", delete=False) as output:
            temporary = Path(output.name)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        destination = snapshot / MANIFEST_NAME
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    cache_model()
