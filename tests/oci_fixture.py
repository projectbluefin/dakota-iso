"""Tiny OCI archive with real descriptor sizes and SHA-256 digests."""

import hashlib
import io
import json
from pathlib import Path
import tarfile


def make_oci_archive(path):
    blobs = {}

    def descriptor(data, media_type):
        digest = hashlib.sha256(data).hexdigest()
        blobs[f"blobs/sha256/{digest}"] = data
        return dict(mediaType=media_type, digest=f"sha256:{digest}", size=len(data))

    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        entry = tarfile.TarInfo("fixture.txt")
        entry.size = 7
        tar.addfile(entry, io.BytesIO(b"fixture"))
    layer_bytes = layer.getvalue()
    config = descriptor(json.dumps(dict(os="linux", architecture="amd64", rootfs=dict(
        type="layers", diff_ids=["sha256:" + hashlib.sha256(layer_bytes).hexdigest()]
    ))).encode(), "application/vnd.oci.image.config.v1+json")
    manifest = descriptor(json.dumps(dict(schemaVersion=2, config=config, layers=[
        descriptor(layer_bytes, "application/vnd.oci.image.layer.v1.tar")
    ])).encode(), "application/vnd.oci.image.manifest.v1+json")
    files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
             "index.json": json.dumps(dict(schemaVersion=2, manifests=[manifest])).encode(), **blobs}
    with tarfile.open(path, "w") as tar:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return files, manifest, config
