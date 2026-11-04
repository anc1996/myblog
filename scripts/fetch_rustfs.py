"""Load the pinned official amd64 RustFS image through the authorized proxy.

The proxy is used by this process, so the Docker daemon and other services do
not need a configuration change or restart. No image layer is extracted onto
the host filesystem: Docker alone applies the verified layers.
"""

import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


ROOT = Path("/mnt/f/openclaw/workspace/wagtail/myblog")
CACHE = Path("/opt/myblog-services/rustfs-image")
IMAGE = "rustfs/rustfs:1.0.4"
INDEX = "sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2"
MANIFEST = "sha256:e287e99762a4a3e7b706526e01f2ff755f67db165e9be9f6b907adc6fb6d6ee1"
CONFIG = "sha256:2755bcf766f77f221f39e056864948fb4d1862381831ed0b347226e4bc1a43a9"
PROXY = "http://192.168.20.3:5071"
REGISTRY = "https://registry-1.docker.io/v2/rustfs/rustfs/"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main():
    if Path.cwd() != ROOT:
        raise RuntimeError("Use this project's authorized WSL directory")
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}),
    )
    with opener.open(
        "https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository:rustfs/rustfs:pull", timeout=45,
    ) as response:
        token = json.load(response)["token"]

    def fetch(path, expected):
        destination = CACHE / expected.split(":")[1]
        if destination.exists() and digest(destination.read_bytes()) == expected:
            return destination
        request = urllib.request.Request(REGISTRY + path, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.index.v1+json,"
                      "application/vnd.oci.image.manifest.v1+json",
        })
        # A failed or interrupted download is never mistaken for a cached blob.
        partial = destination.with_suffix(".partial")
        with opener.open(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(partial.read_bytes()) != expected:
            raise RuntimeError("Official image content digest mismatch")
        partial.replace(destination)
        return destination

    index = json.loads(fetch("manifests/" + INDEX, INDEX).read_bytes())
    platforms = [item for item in index["manifests"]
                 if item.get("platform") == {"architecture": "amd64", "os": "linux"}]
    if len(platforms) != 1 or platforms[0]["digest"] != MANIFEST:
        raise RuntimeError("Unexpected official amd64 image manifest")
    manifest = json.loads(fetch("manifests/" + MANIFEST, MANIFEST).read_bytes())
    if manifest["config"]["digest"] != CONFIG:
        raise RuntimeError("Unexpected official image configuration")
    config_path = fetch("blobs/" + CONFIG, CONFIG)
    config = json.loads(config_path.read_bytes())
    diff_ids = config["rootfs"]["diff_ids"]
    if len(diff_ids) != len(manifest["layers"]):
        raise RuntimeError("Layer configuration mismatch")

    with tempfile.TemporaryDirectory(prefix="load-", dir=CACHE) as directory:
        work = Path(directory)
        layers = []
        for number, (layer, diff_id) in enumerate(zip(manifest["layers"], diff_ids)):
            if layer["mediaType"] != "application/vnd.oci.image.layer.v1.tar+gzip":
                raise RuntimeError("Unsupported official layer format")
            compressed = fetch("blobs/" + layer["digest"], layer["digest"])
            target = work / f"layer-{number}.tar"
            with gzip.open(compressed, "rb") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            with target.open("rb") as source:
                actual = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
            if actual != diff_id:
                raise RuntimeError("Uncompressed official layer digest mismatch")
            layers.append(target.name)
            print(f"Verified official RustFS layer {number + 1}/{len(diff_ids)}", flush=True)
        config_name = CONFIG.split(":")[1] + ".json"
        archive_path = work / "rustfs.docker.tar"
        with tarfile.open(archive_path, "w") as archive:
            archive.add(config_path, arcname=config_name)
            for layer_name in layers:
                archive.add(work / layer_name, arcname=layer_name)
            content = json.dumps([{
                "Config": config_name, "RepoTags": [IMAGE], "Layers": layers,
            }]).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        subprocess.run(
            ["docker", "--host=unix:///var/run/docker.sock", "image", "load",
             "--input", str(archive_path)], check=True,
        )
        # Docker's containerd store may assign a converted manifest digest as
        # .Id. Verify the exported config bytes instead of assuming .Id is the
        # upstream config digest (that assumption only holds on classic stores).
        exported = work / "exported.tar"
        subprocess.run(
            ["docker", "--host=unix:///var/run/docker.sock", "image", "save",
             "--output", str(exported), IMAGE], check=True,
        )
        with tarfile.open(exported) as archive:
            saved = json.load(archive.extractfile("manifest.json"))
            if len(saved) != 1:
                raise RuntimeError("Unexpected exported image count")
            exported_config = archive.extractfile(saved[0]["Config"]).read()
            if digest(exported_config) != CONFIG:
                raise RuntimeError("Loaded image config differs from official config bytes")
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", "image", "inspect",
         IMAGE, "--format", "{{.Id}}"], check=True, capture_output=True, text=True,
    )
    loaded_id = result.stdout.strip()
    (CACHE / "verified.json").write_text(
        json.dumps({"image": IMAGE, "index": INDEX, "manifest": MANIFEST,
                    "config": CONFIG, "loaded_id": loaded_id},
                   indent=2) + "\n", encoding="utf-8",
    )
    print("Pinned official RustFS image verified and loaded:", IMAGE, loaded_id)


if __name__ == "__main__":
    main()
