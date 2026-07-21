from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int
    security: str
    connected: bool = False

    @property
    def secured(self) -> bool:
        return bool(self.security and self.security != "--")


@dataclass(frozen=True)
class WifiSnapshot:
    available: bool
    connected: bool
    connected_ssid: str
    busy: bool
    status: str
    error: str
    networks: tuple[WifiNetwork, ...]


CommandRunner = Callable[[list[str], str | None, float], subprocess.CompletedProcess[str]]


def _default_runner(command: list[str], input_text: str | None, timeout: float) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env=environment,
    )


def _split_escaped(line: str) -> list[str]:
    """Split nmcli terse output while preserving escaped colons/backslashes."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line.rstrip("\r\n"):
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def parse_networks(output: str) -> tuple[WifiNetwork, ...]:
    by_ssid: dict[str, WifiNetwork] = {}
    for line in output.splitlines():
        fields = _split_escaped(line)
        if len(fields) < 4:
            continue
        active, ssid, raw_signal = fields[:3]
        security = ":".join(fields[3:]).strip()
        if not ssid:
            continue
        try:
            signal = max(0, min(100, int(raw_signal)))
        except ValueError:
            signal = 0
        candidate = WifiNetwork(
            ssid=ssid,
            signal=signal,
            security=security,
            connected=active.strip() in {"*", "yes"},
        )
        previous = by_ssid.get(ssid)
        if previous is None or candidate.connected or candidate.signal > previous.signal:
            by_ssid[ssid] = candidate
    return tuple(sorted(by_ssid.values(), key=lambda item: (not item.connected, -item.signal, item.ssid.lower())))


class WifiManager:
    """Small asynchronous NetworkManager facade for the touchscreen UI."""

    def __init__(self, runner: CommandRunner | None = None, nmcli_path: str | None = None):
        self._runner = runner or _default_runner
        self._nmcli = nmcli_path or shutil.which("nmcli") or ""
        self._lock = threading.Lock()
        self._available = bool(self._nmcli)
        self._connected = False
        self._connected_ssid = ""
        self._busy = False
        self._status = "Checking Wi-Fi" if self._available else "Wi-Fi unavailable"
        self._error = "" if self._available else "NetworkManager (nmcli) was not found"
        self._networks: tuple[WifiNetwork, ...] = ()
        self._last_poll = 0.0
        self._scan_requested = False
        self._pending_connect: tuple[WifiNetwork, str] | None = None
        if self._available:
            self.poll(force=True)

    def snapshot(self) -> WifiSnapshot:
        with self._lock:
            return WifiSnapshot(
                available=self._available,
                connected=self._connected,
                connected_ssid=self._connected_ssid,
                busy=self._busy,
                status=self._status,
                error=self._error,
                networks=self._networks,
            )

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def poll(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._available or self._busy or (not force and now - self._last_poll < 5.0):
                return
            self._busy = True
            self._last_poll = now
        threading.Thread(target=self._scan_worker, args=(False, True), daemon=True, name="wifi-status").start()

    def scan(self) -> None:
        with self._lock:
            if not self._available:
                return
            if self._busy:
                self._scan_requested = True
                return
            self._busy = True
            self._status = "Scanning for networks..."
            self._error = ""
        threading.Thread(target=self._scan_worker, args=(True, False), daemon=True, name="wifi-scan").start()

    def connect(self, network: WifiNetwork, password: str = "") -> None:
        with self._lock:
            if not self._available:
                return
            if self._busy:
                self._pending_connect = (network, password)
                self._status = f"Waiting to connect to {network.ssid}..."
                return
            self._busy = True
            self._status = f"Connecting to {network.ssid}..."
            self._error = ""
        threading.Thread(
            target=self._connect_worker,
            args=(network, password),
            daemon=True,
            name="wifi-connect",
        ).start()

    def _wifi_command(self, rescan: bool) -> list[str]:
        return [
            self._nmcli,
            "--terse",
            "--escape",
            "yes",
            "--colors",
            "no",
            "--fields",
            "IN-USE,SSID,SIGNAL,SECURITY",
            "device",
            "wifi",
            "list",
            "--rescan",
            "yes" if rescan else "no",
            "ifname",
            "wlan0",
        ]

    def _scan_worker(self, rescan: bool, quiet: bool) -> None:
        try:
            result = self._runner(self._wifi_command(rescan), None, 20.0)
            if result.returncode != 0:
                raise RuntimeError((result.stdout or "Unable to scan Wi-Fi").strip().splitlines()[-1])
            networks = parse_networks(result.stdout or "")
            connected = next((network.ssid for network in networks if network.connected), "")
            with self._lock:
                self._networks = networks
                self._connected = bool(connected)
                self._connected_ssid = connected
                self._status = f"Connected: {connected}" if connected else "Not connected"
                self._error = ""
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            with self._lock:
                if not quiet:
                    self._error = str(exc)[:100]
                self._status = "Wi-Fi check failed"
        finally:
            scan_again = False
            pending_connect: tuple[WifiNetwork, str] | None = None
            with self._lock:
                self._busy = False
                if self._pending_connect:
                    pending_connect = self._pending_connect
                    self._pending_connect = None
                    self._busy = True
                    self._status = f"Connecting to {pending_connect[0].ssid}..."
                elif self._scan_requested:
                    self._scan_requested = False
                    self._busy = True
                    self._status = "Scanning for networks..."
                    scan_again = True
            if pending_connect:
                threading.Thread(
                    target=self._connect_worker,
                    args=pending_connect,
                    daemon=True,
                    name="wifi-connect",
                ).start()
            elif scan_again:
                threading.Thread(target=self._scan_worker, args=(True, False), daemon=True, name="wifi-scan").start()

    def _connect_worker(self, network: WifiNetwork, password: str) -> None:
        command = [self._nmcli, "--wait", "30"]
        input_text: str | None = None
        if network.secured:
            # --ask reads the PSK from stdin, keeping it out of argv, logs, and
            # this application's configuration file.
            command.append("--ask")
            input_text = password + "\n"
        command.extend(["device", "wifi", "connect", network.ssid, "ifname", "wlan0"])
        try:
            result = self._runner(command, input_text, 40.0)
            if result.returncode != 0:
                message = (result.stdout or "Connection failed").strip().splitlines()[-1]
                raise RuntimeError(message)
            self._scan_worker(False, True)
            with self._lock:
                if self._connected:
                    self._status = f"Connected: {self._connected_ssid}"
                else:
                    self._status = "Connection completed"
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            with self._lock:
                self._error = str(exc)[:100]
                self._status = "Could not connect"
                self._busy = False
