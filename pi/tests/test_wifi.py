import subprocess
import threading
import unittest

from plane_radar.wifi import WifiManager, WifiNetwork, parse_networks


class WifiTests(unittest.TestCase):
    def test_scan_parses_escaped_ssids_and_deduplicates(self):
        networks = parse_networks(
            "*:Home\\:Office:82:WPA2\n"
            ":Cafe:35:--\n"
            ":Cafe:67:--\n"
        )
        self.assertEqual([network.ssid for network in networks], ["Home:Office", "Cafe"])
        self.assertTrue(networks[0].connected)
        self.assertEqual(networks[1].signal, 67)
        self.assertFalse(networks[1].secured)

    def test_password_is_sent_on_stdin_not_in_command(self):
        calls = []

        def runner(command, input_text, timeout):
            calls.append((command, input_text, timeout))
            if "connect" in command:
                return subprocess.CompletedProcess(command, 0, "success")
            return subprocess.CompletedProcess(command, 0, "*:Test WiFi:90:WPA2\n")

        manager = WifiManager(runner=runner, nmcli_path="/usr/bin/nmcli")
        # Let the constructor's asynchronous status check settle, then exercise
        # the worker directly so this assertion remains deterministic.
        for thread in list(threading.enumerate()):
            if thread.name == "wifi-status":
                thread.join(timeout=1)
        secret = "not-in-argv"
        manager._connect_worker(WifiNetwork("Test WiFi", 90, "WPA2"), secret)
        connect_command, input_text, _timeout = next(call for call in calls if "connect" in call[0])
        self.assertNotIn(secret, connect_command)
        self.assertEqual(input_text, secret + "\n")
        self.assertIn("--ask", connect_command)
