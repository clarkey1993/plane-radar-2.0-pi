from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .config import RadarConfig


LOG = logging.getLogger("plane-radar-updater")
ARCHIVE_NAME = "plane-radar-pi.tar.gz"
MANIFEST_NAME = "pi-manifest.json"


def version_tuple(value: str) -> tuple[int, ...]:
    clean = value.strip().lower().removeprefix("pi-v").removeprefix("v")
    values: list[int] = []
    for part in clean.split("."):
        digits = "".join(character for character in part if character.isdigit())
        values.append(int(digits or 0))
    return tuple((values + [0, 0, 0])[:3])


def _json_url(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": f"PlaneRadarPi/{__version__}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def _download(url: str, destination: Path, max_bytes: int) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": f"PlaneRadarPi/{__version__}"})
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as target:
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("download exceeds safety limit")
            target.write(chunk)
    return total


def find_release(repository: str, channel: str) -> dict[str, Any] | None:
    releases = _json_url(f"https://api.github.com/repos/{repository}/releases?per_page=30")
    for release in releases:
        tag = str(release.get("tag_name") or "")
        if not tag.startswith("pi-v") or release.get("draft"):
            continue
        if release.get("prerelease") and channel != "beta":
            continue
        assets = {asset.get("name"): asset for asset in release.get("assets", [])}
        if ARCHIVE_NAME in assets and MANIFEST_NAME in assets:
            return release
    return None


def _validate_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {member.name}")
    if member.issym() or member.islnk() or member.isdev():
        raise ValueError(f"unsupported archive entry: {member.name}")


def install_release(release: dict[str, Any], repository: str, install_root: Path) -> bool:
    assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
    with tempfile.TemporaryDirectory(prefix="plane-radar-update-") as temp_name:
        temp = Path(temp_name)
        manifest_path = temp / MANIFEST_NAME
        archive_path = temp / ARCHIVE_NAME
        _download(assets[MANIFEST_NAME], manifest_path, 32 * 1024)
        manifest = json.loads(manifest_path.read_text())
        required = {"version", "platform", "sha256", "size", "repository"}
        if not required.issubset(manifest):
            raise ValueError("update manifest is incomplete")
        if manifest["platform"] != "raspberry-pi" or manifest["repository"] != repository:
            raise ValueError("update manifest target mismatch")
        release_version = str(manifest["version"])
        if version_tuple(release_version) <= version_tuple(__version__):
            LOG.info("Already current: installed=%s available=%s", __version__, release_version)
            return False
        expected_size = int(manifest["size"])
        if expected_size <= 0 or expected_size > 25 * 1024 * 1024:
            raise ValueError("update archive size is invalid")
        actual_size = _download(assets[ARCHIVE_NAME], archive_path, 25 * 1024 * 1024)
        if actual_size != expected_size:
            raise ValueError("update archive size mismatch")
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if digest.lower() != str(manifest["sha256"]).lower():
            raise ValueError("update archive SHA-256 mismatch")

        releases_dir = install_root / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        staging = releases_dir / f".{release_version}.staging"
        destination = releases_dir / release_version
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                _validate_member(member)
            archive.extractall(staging, filter="data")
        if not (staging / "plane_radar" / "__main__.py").is_file():
            raise ValueError("update archive does not contain the application")
        subprocess.run([sys.executable, "-m", "compileall", "-q", str(staging / "plane_radar")], check=True)
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)

        current = install_root / "current"
        temporary_link = install_root / ".current.new"
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(destination)
        os.replace(temporary_link, current)
        LOG.info("Installed Plane Radar Pi %s", release_version)

        installed = sorted(
            (path for path in releases_dir.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: version_tuple(path.name),
            reverse=True,
        )
        for old_release in installed[3:]:
            shutil.rmtree(old_release)
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check for verified Plane Radar Pi updates")
    parser.add_argument("--config", default="/etc/plane-radar/config.json")
    parser.add_argument("--install-root", default="/opt/plane-radar")
    parser.add_argument("--check-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = RadarConfig.load(args.config)
    if not config.auto_update:
        LOG.info("Automatic updates are disabled")
        return 0
    try:
        release = find_release(config.update_repository, config.update_channel)
        if not release:
            LOG.info("No Pi release found")
            return 0
        available = str(release["tag_name"]).removeprefix("pi-v")
        if version_tuple(available) <= version_tuple(__version__):
            LOG.info("Already current: installed=%s available=%s", __version__, available)
            return 0
        LOG.info("Update available: installed=%s available=%s", __version__, available)
        if args.check_only:
            return 10
        changed = install_release(release, config.update_repository, Path(args.install_root))
        if changed:
            subprocess.run(["systemctl", "try-restart", "plane-radar.service"], check=False)
        return 0
    except Exception:
        LOG.exception("Update check failed; keeping the installed version")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
