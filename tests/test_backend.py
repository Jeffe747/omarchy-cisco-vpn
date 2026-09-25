import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import backend


class BackendTest(unittest.TestCase):
    def test_run_limits_combined_subprocess_output(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaisesRegex(
                    backend.VpnError, "produced too much output"):
                backend.run([sys.executable, "-c",
                             f"import sys; sys.{stream}.buffer.write(b'x' * (2 * 1024 * 1024))"],
                            timeout=5)

    def test_run_enforces_exact_combined_limit(self):
        script = ("import sys; sys.stdout.buffer.write(b'x' * 524288); "
                  "sys.stderr.buffer.write(b'y' * int(sys.argv[1]))")
        result = backend.run([sys.executable, "-c", script, "524288"], timeout=5)
        self.assertEqual(len(result.stdout) + len(result.stderr), backend.MAX_OUTPUT_BYTES)
        with self.assertRaisesRegex(backend.VpnError, "produced too much output"):
            backend.run([sys.executable, "-c", script, "524289"], timeout=5)

    def test_run_preserves_stdin_and_both_output_streams(self):
        result = backend.run(
            [sys.executable, "-c",
             "import sys; print(sys.stdin.readline().strip()); print('warning', file=sys.stderr)"],
            input="secret\n", timeout=5,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "secret\n", "warning\n"))

    def test_run_still_times_out_without_output(self):
        with self.assertRaisesRegex(backend.VpnError, "timed out"):
            backend.run([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.1)

    def test_status_reads_gateway_from_vpn_data(self):
        vpn = Mock()
        vpn.get_data_item.return_value = "https://vpn.example.com"
        vpn.get_user_name.return_value = "name"
        connection = Mock()
        connection.get_setting_vpn.return_value = vpn
        client = Mock()
        client.get_connection_by_uuid.return_value = connection
        with patch.object(backend, "profile_uuid", return_value="test-uuid"), patch.object(
                backend.NM.Client, "new", return_value=client), patch.object(
                backend, "nmcli", return_value="test-uuid"):
            self.assertEqual(backend.state(), {
                "connected": True, "server": "https://vpn.example.com", "username": "name"
            })
        vpn.get_data_item.assert_called_with("gateway")

    def test_existing_profile_removes_agent_only_secret_flags(self):
        with patch.object(backend, "profile_uuid", return_value="test-uuid"), patch.object(
                backend, "nmcli") as nmcli:
            backend.ensure_profile("https://vpn.example.com", "name")
        self.assertEqual(nmcli.call_args.args[4:6],
                         ("vpn.data", "gateway=https://vpn.example.com,protocol=anyconnect"))

    def test_server_requires_https_without_userinfo(self):
        self.assertEqual(backend.server_url("vpn.example.com/path"), "https://vpn.example.com/path")
        for address in ("http://vpn.example.com", "https://user:pass@vpn.example.com",
                        "https://vpn.example.com:bad", "vpn.example.com\nbad",
                        "vpn.example.com/path,other=option"):
            with self.subTest(address=address), self.assertRaises(backend.VpnError):
                backend.server_url(address)

    def test_authenticate_parses_only_expected_fields(self):
        text = ("COOKIE='private-cookie'\nHOST='10.0.0.1'\n"
                "CONNECT_URL='https://vpn.example.com/+vpn'\n"
                "FINGERPRINT='sha256:abc'\nRESOLVE='vpn.example.com:10.0.0.1'\n")
        with patch.object(backend, "run",
                          return_value=subprocess.CompletedProcess([], 0, text, "")) as runner:
            values = backend.authenticate("https://vpn.example.com", "name", "private-password")
        self.assertEqual(values["COOKIE"], "private-cookie")
        self.assertEqual(values["CONNECT_URL"], "https://vpn.example.com/+vpn")
        self.assertNotIn("HOST", values)
        self.assertNotIn("private-password", runner.call_args.args[0])
        self.assertEqual(runner.call_args.kwargs["input"], "private-password\n")

    def test_activation_passes_secrets_only_to_nm_memory(self):
        values = {"COOKIE": "private-cookie", "CONNECT_URL": "https://vpn.example.com",
                  "FINGERPRINT": "sha256:abc"}
        setting = Mock()
        connection = Mock()
        connection.get_setting_vpn.return_value = setting
        connection.commit_changes.return_value = True
        client = Mock()
        client.get_connection_by_uuid.return_value = connection
        bus = Mock()
        with patch.object(backend.NM.Client, "new", return_value=client), patch.object(
                backend.Gio, "bus_get_sync", return_value=bus), patch.object(
                backend, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as runner:
            backend.activate("00000000-0000-0000-0000-000000000000", values)
        setting.add_secret.assert_any_call("cookie", "private-cookie")
        self.assertEqual(bus.call_sync.call_args.args[3], "ClearSecrets")
        self.assertEqual(connection.commit_changes.call_count, 1)
        self.assertEqual(connection.commit_changes.call_args.args, (False, None))
        self.assertNotIn("private-cookie", " ".join(runner.call_args.args[0]))

    def test_failed_authentication_never_exposes_cookie(self):
        with patch.object(backend, "run",
                          return_value=subprocess.CompletedProcess([], 2, "", "private-password")):
            with self.assertRaisesRegex(backend.VpnError, "OpenConnect authentication failed") as error:
                backend.authenticate("https://vpn.example.com", "name", "private-password")
            self.assertNotIn("private-password", str(error.exception))

    def test_authentication_error_classifies_without_exposing_server_output(self):
        cases = (
            ("Server certificate verify failed: vpn.example.com", "certificate"),
            ("Failed to connect to vpn.example.com: Connection refused", "Could not reach"),
            ("Failed to resolve host vpn.example.com", "name could not be resolved"),
            ("Authentication failed: wrong password for user name", "rejected the credentials"),
            ("Authentication requires form entry: token for user name", "additional sign-in step"),
        )
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                message = backend.authentication_error(stderr)
                self.assertIn(expected, message)
                self.assertNotIn("vpn.example.com", message)
                self.assertNotIn("user name", message)

    def test_connect_reads_one_line_without_waiting_for_eof(self):
        request = {"server": "vpn.example.com", "username": "name", "password": "secret"}
        stdin = io.StringIO(json.dumps(request) + "\nextra input")
        with patch.object(sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", stdin), patch.object(
                backend, "ensure_profile", return_value="uuid"), patch.object(
                backend, "authenticate", return_value={"COOKIE": "cookie"}), patch.object(
                backend, "activate"), patch.object(
                backend, "state", return_value={"connected": True}) as state:
            self.assertEqual(backend.main(), state.return_value)
        self.assertEqual(stdin.read(), "extra input")

    def test_cancel_stops_child(self):
        child = Mock()
        child.poll.return_value = None
        with patch.object(backend, "active_child", child):
            with self.assertRaisesRegex(backend.VpnCancelled, "cancelled"):
                backend.cancel(None, None)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
