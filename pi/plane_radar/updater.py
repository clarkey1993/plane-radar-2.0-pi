from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

from . import __version__
from .config import RadarConfig


LOG = logging.getLogger("plane-radar-updater")
ARCHIVE_NAME = "plane-radar-pi.tar.gz"
MANIFEST_NAME = "pi-manifest.json"
ProgressCallback = Callable[[float, str], None]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def render_update_frame(
    progress: float,
    status: str,
    spinner_frame: int,
    version: str,
    failed: bool = False,
) -> Image.Image:
    """Render the native 320x480 update screen without requiring Pi hardware."""
    progress = max(0.0, min(100.0, progress))
    image = Image.new("RGB", (320, 480), (1, 7, 10))
    draw = ImageDraw.Draw(image)
    small = _font(11)
    body = _font(14)
    title = _font(22, True)
    percent_font = _font(32, True)

    draw.rectangle((0, 0, 320, 44), fill=(2, 14, 18))
    draw.text((12, 10), "PLANE RADAR", font=_font(17, True), fill=(115, 255, 188))
    draw.text((160, 78), "SOFTWARE UPDATE", font=title, anchor="mm", fill=(225, 250, 240))
    draw.text((160, 108), f"Installing version {version}", font=body, anchor="mm", fill=(85, 170, 175))

    center_x, center_y = 160, 205
    for index in range(12):
        angle = math.radians(index * 30)
        age = (index - spinner_frame) % 12
        intensity = max(0.16, 1.0 - age * 0.075)
        color = (
            int(65 * intensity),
            int(255 * intensity),
            int(178 * intensity),
        )
        inner = (center_x + math.sin(angle) * 23, center_y - math.cos(angle) * 23)
        outer = (center_x + math.sin(angle) * 39, center_y - math.cos(angle) * 39)
        draw.line((inner, outer), fill=color, width=5)

    percent_color = (255, 105, 90) if failed else (130, 255, 205)
    draw.text((160, 270), f"{int(round(progress)):d}%", font=percent_font, anchor="mm", fill=percent_color)
    draw.rounded_rectangle((24, 310, 296, 338), radius=9, fill=(3, 22, 27), outline=(32, 105, 100), width=2)
    fill_width = int(268 * progress / 100.0)
    if fill_width > 0:
        draw.rounded_rectangle((26, 312, 26 + fill_width, 336), radius=7, fill=(255, 80, 70) if failed else (45, 220, 150))
    draw.text((160, 370), status[:42], font=body, anchor="mm", fill=(255, 120, 100) if failed else (155, 210, 205))
    draw.text((160, 437), "DO NOT POWER OFF", font=small, anchor="mm", fill=(255, 180, 80))
    return image


class UpdateDisplay:
    """Own the LCD while the radar service is stopped and animate progress."""

    def __init__(self, config: RadarConfig, version: str):
        self.config = config
        self.version = version
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="update-display", daemon=True)
        self._target = 0.0
        self._displayed = 0.0
        self._status = "Preparing update"
        self._failed = False
        self._available = False
        self._finished = False

    def start(self) -> None:
        self._thread.start()
        self._ready_event.wait(timeout=3.0)

    def update(self, progress: float, status: str) -> None:
        with self._lock:
            self._target = max(self._target, min(100.0, progress))
            self._status = status

    def finish(self, success: bool, status: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        with self._lock:
            self._failed = not success
            self._status = status or ("Update complete" if success else "Update failed — restoring radar")
            if success:
                self._target = 100.0
        if self._available:
            deadline = time.monotonic() + (5.0 if success else 2.0)
            while time.monotonic() < deadline:
                with self._lock:
                    complete = self._displayed >= 99.5 if success else True
                if complete:
                    break
                time.sleep(0.1)
            time.sleep(1.0 if success else 1.5)
        self._stop_event.set()
        self._thread.join(timeout=3.0)

    def _run(self) -> None:
        display = None
        try:
            from .hardware import ST7796Display

            display = ST7796Display(
                self.config.spi_hz,
                self.config.brightness,
                self.config.display_mirror_x,
            )
            self._available = True
        except Exception:
            LOG.exception("Update display unavailable; continuing without animation")
        finally:
            self._ready_event.set()
        if display is None:
            return

        spinner_frame = 0
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    difference = self._target - self._displayed
                    if difference > 0:
                        self._displayed = min(self._target, self._displayed + max(0.35, difference * 0.16))
                    progress = self._displayed
                    status = self._status
                    failed = self._failed
                display.show(render_update_frame(progress, status, spinner_frame, self.version, failed))
                spinner_frame = (spinner_frame + 1) % 12
                time.sleep(0.12)
        finally:
            display.close()


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


def _download(
    url: str,
    destination: Path,
    max_bytes: int,
    progress: Callable[[int], None] | None = None,
) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": f"PlaneRadarPi/{__version__}"})
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as target:
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("download exceeds safety limit")
            target.write(chunk)
            if progress:
                progress(total)
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


def install_release(
    release: dict[str, Any],
    repository: str,
    install_root: Path,
    progress: ProgressCallback | None = None,
) -> bool:
    report = progress or (lambda _value, _status: None)
    assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
    with tempfile.TemporaryDirectory(prefix="plane-radar-update-") as temp_name:
        temp = Path(temp_name)
        manifest_path = temp / MANIFEST_NAME
        archive_path = temp / ARCHIVE_NAME
        report(5, "Downloading update manifest")
        _download(assets[MANIFEST_NAME], manifest_path, 32 * 1024)
        report(12, "Checking update information")
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
        report(18, "Downloading software package")

        def download_progress(downloaded: int) -> None:
            fraction = min(1.0, downloaded / expected_size)
            report(18 + 50 * fraction, "Downloading software package")

        actual_size = _download(
            assets[ARCHIVE_NAME],
            archive_path,
            25 * 1024 * 1024,
            download_progress,
        )
        if actual_size != expected_size:
            raise ValueError("update archive size mismatch")
        report(72, "Verifying download")
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if digest.lower() != str(manifest["sha256"]).lower():
            raise ValueError("update archive SHA-256 mismatch")

        releases_dir = install_root / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        staging = releases_dir / f".{release_version}.staging"
        destination = releases_dir / release_version
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        report(79, "Unpacking update")
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                _validate_member(member)
            archive.extractall(staging, filter="data")
        if not (staging / "plane_radar" / "__main__.py").is_file():
            raise ValueError("update archive does not contain the application")
        report(88, "Validating application")
        subprocess.run([sys.executable, "-m", "compileall", "-q", str(staging / "plane_radar")], check=True)
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)

        report(95, "Installing update")
        current = install_root / "current"
        temporary_link = install_root / ".current.new"
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(destination)
        os.replace(temporary_link, current)
        report(99, "Finalising installation")
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
    screen: UpdateDisplay | None = None
    service_was_stopped = False
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
        stop_result = subprocess.run(["systemctl", "stop", "plane-radar.service"], check=False)
        service_was_stopped = stop_result.returncode == 0
        if service_was_stopped:
            screen = UpdateDisplay(config, available)
            screen.start()
            screen.update(2, "Preparing update")
        else:
            LOG.warning("Could not stop radar service; continuing update without display animation")
        changed = install_release(
            release,
            config.update_repository,
            Path(args.install_root),
            screen.update if screen else None,
        )
        if screen:
            screen.finish(True, "Update complete" if changed else "Software already current")
        return 0
    except Exception:
        LOG.exception("Update check failed; keeping the installed version")
        if screen:
            screen.finish(False)
        return 1
    finally:
        if service_was_stopped:
            subprocess.run(["systemctl", "restart", "plane-radar.service"], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
