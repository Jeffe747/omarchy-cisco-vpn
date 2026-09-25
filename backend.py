#!/usr/bin/env python3
"""Local Cisco VPN bridge: OpenConnect authentication, NetworkManager tunnel."""

import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

import gi

gi.require_version("NM", "1.0")
from gi.repository import Gio, GLib, NM


PROFILE = "Omarchy Cisco VPN"
SERVICE = "org.freedesktop.NetworkManager.openconnect"
UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
MAX_OUTPUT_BYTES = 1024 * 1024


class VpnError(Exception):
    pass


class VpnCancelled(VpnError):
    pass


active_child = None


def cancel(_signal, _frame):
    if active_child is not None and active_child.poll() is None:
        active_child.terminate()
        try:
            active_child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            active_child.kill()
            active_child.wait()
    raise VpnCancelled("Connection cancelled")


signal.signal(signal.SIGTERM, cancel)


def run(args, *, input=None, timeout=100):
    global active_child
    try:
        with subprocess.Popen(
            args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as child:
            active_child = child
            try:
                output = {child.stdout: [], child.stderr: []}
                total = 0
                deadline = time.monotonic() + timeout
                pending = memoryview(input.encode() if input is not None else b"")
                with selectors.DefaultSelector() as selector:
                    for stream in output:
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ)
                    if input is not None:
                        os.set_blocking(child.stdin.fileno(), False)
                        if pending:
                            selector.register(child.stdin, selectors.EVENT_WRITE)
                        else:
                            child.stdin.close()
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            child.kill()
                            child.wait()
                            raise VpnError(f"{args[0]} timed out")
                        for key, _ in selector.select(remaining):
                            stream = key.fileobj
                            if stream is child.stdin:
                                try:
                                    count = os.write(stream.fileno(), pending[:65536])
                                except BrokenPipeError:
                                    count = len(pending)
                                pending = pending[count:]
                                if not pending:
                                    selector.unregister(stream)
                                    stream.close()
                            else:
                                chunk = os.read(stream.fileno(), 65536)
                                if not chunk:
                                    selector.unregister(stream)
                                else:
                                    total += len(chunk)
                                    if total > MAX_OUTPUT_BYTES:
                                        child.kill()
                                        child.wait()
                                        raise VpnError(f"{args[0]} produced too much output")
                                    output[stream].append(chunk)
                try:
                    child.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    child.kill()
                    child.wait()
                    raise VpnError(f"{args[0]} timed out") from error
                return subprocess.CompletedProcess(
                    args, child.returncode,
                    b"".join(output[child.stdout]).decode(errors="replace"),
                    b"".join(output[child.stderr]).decode(errors="replace"),
                )
            finally:
                active_child = None
    except OSError as error:
        raise VpnError(f"Could not start {args[0]}: {error.strerror}") from error


def nmcli(*args):
    result = run(["nmcli", *args])
    if result.returncode:
        raise VpnError(f"NetworkManager command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def profile_uuid():
    profiles = nmcli("-t", "--escape", "no", "-f", "UUID,TYPE,NAME", "connection", "show")
    matches = []
    for line in profiles.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2] == PROFILE:
            matches.append((parts[0], parts[1]))
    if not matches:
        return ""
    if len(matches) != 1 or matches[0][1] != "vpn" or not UUID.fullmatch(matches[0][0]):
        raise VpnError(f"Conflicting NetworkManager profile named {PROFILE}")
    uuid = matches[0][0]
    if nmcli("-g", "vpn.service-type", "connection", "show", "uuid", uuid) != SERVICE:
        raise VpnError(f"Conflicting NetworkManager profile named {PROFILE}")
    return uuid


def state():
    uuid = profile_uuid()
    if not uuid:
        return {"connected": False, "server": "", "username": ""}
    connection = NM.Client.new(None).get_connection_by_uuid(uuid)
    if connection is None or connection.get_setting_vpn() is None:
        raise VpnError("NetworkManager could not read the VPN profile")
    vpn = connection.get_setting_vpn()
    active = nmcli("-t", "-f", "UUID", "connection", "show", "--active").splitlines()
    return {
        "connected": uuid in active,
        "server": vpn.get_data_item("gateway") or "",
        "username": vpn.get_user_name() or "",
    }


def server_url(value):
    value = value.strip()
    if not value or "," in value or any(char.isspace() for char in value):
        raise VpnError("Enter a VPN server address without spaces or commas")
    address = value if "://" in value else "https://" + value
    parsed = urlsplit(address)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise VpnError("Enter a valid HTTPS VPN server address")
    try:
        parsed.port
    except ValueError as error:
        raise VpnError("Invalid VPN server port") from error
    return address


def ensure_profile(address, username):
    data = f"gateway={address},protocol=anyconnect"
    uuid = profile_uuid()
    if uuid:
        nmcli("connection", "modify", "uuid", uuid, "vpn.data", data, "vpn.user-name", username,
              "connection.autoconnect", "no")
    else:
        nmcli("connection", "add", "type", "vpn", "vpn-type", "openconnect",
              "ifname", "*", "con-name", PROFILE, "autoconnect", "no",
              "--", "vpn.data", data,
              "vpn.user-name", username)
        uuid = profile_uuid()
        if not uuid:
            raise VpnError("NetworkManager did not create the VPN profile")
    return uuid


def authentication_error(stderr):
    details = stderr.lower()
    if any(term in details for term in ("certificate", "servercert", "issuer", "trust anchor")):
        return "VPN server certificate could not be verified; check with your IT team"
    if any(term in details for term in ("resolve", "getaddrinfo", "name or service not known")):
        return "VPN server name could not be resolved"
    if any(term in details for term in ("failed to connect", "connection refused", "network is unreachable")):
        return "Could not reach the VPN server; check the address and network"
    if any(term in details for term in ("authgroup", "non-interactive", "form entry", "token", "challenge")):
        return "VPN requires an additional sign-in step (group or token) not supported by this widget"
    if any(term in details for term in ("login failed", "authentication failed", "invalid credentials", "incorrect password")):
        return "VPN rejected the credentials; check your username and password"
    return "OpenConnect authentication failed; the gateway may require another login step"


def authenticate(address, username, password):
    result = run(
        ["openconnect", "--protocol=anyconnect", "--authenticate",
         "--non-inter", "--passwd-on-stdin", "--user", username, address],
        input=password + "\n", timeout=90,
    )
    if result.returncode:
        raise VpnError(authentication_error(result.stderr))

    values = {}
    for line in result.stdout.splitlines():
        key, separator, raw = line.partition("=")
        if not separator or key not in ("COOKIE", "CONNECT_URL", "FINGERPRINT", "RESOLVE"):
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as error:
            raise VpnError("OpenConnect returned malformed authentication data") from error
        if len(parts) != 1 or "\n" in parts[0] or "\r" in parts[0]:
            raise VpnError("OpenConnect returned malformed authentication data")
        values[key] = parts[0]
    if not all(values.get(key) for key in ("COOKIE", "CONNECT_URL", "FINGERPRINT")):
        raise VpnError("OpenConnect did not return the gateway, cookie, and certificate fingerprint")
    return values


def activate(uuid, values):
    secrets = {
        "cookie": values["COOKIE"],
        "gateway": values["CONNECT_URL"],
        "gwcert": values["FINGERPRINT"],
    }
    if values.get("RESOLVE"):
        secrets["resolve"] = values["RESOLVE"]
    client = NM.Client.new(None)
    connection = client.get_connection_by_uuid(uuid)
    if connection is None:
        raise VpnError("NetworkManager could not find the VPN profile")
    vpn = connection.get_setting_vpn()
    if vpn is None:
        raise VpnError("NetworkManager VPN profile is invalid")
    for key, value in secrets.items():
        vpn.add_secret(key, value)
    try:
        if not connection.commit_changes(False, None):
            raise VpnError("Could not pass VPN credentials to NetworkManager")
        result = run(["nmcli", "--wait", "90", "connection", "up", "uuid", uuid])
        if result.returncode:
            raise VpnError(f"VPN activation failed (NetworkManager exit code {result.returncode})")
    except VpnCancelled:
        if uuid in nmcli("-t", "-f", "UUID", "connection", "show", "--active").splitlines():
            nmcli("connection", "down", "uuid", uuid)
        raise
    finally:
        Gio.bus_get_sync(Gio.BusType.SYSTEM, None).call_sync(
            "org.freedesktop.NetworkManager", connection.get_path(),
            "org.freedesktop.NetworkManager.Settings.Connection", "ClearSecrets",
            None, None, Gio.DBusCallFlags.NONE, 10000, None,
        )


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("status", "connect", "disconnect"):
        raise VpnError("Expected status, connect, or disconnect")
    action = sys.argv[1]
    if action == "connect":
        request = json.loads(sys.stdin.readline())
        address = server_url(request.get("server", ""))
        username = request.get("username", "").strip()
        password = request.get("password", "")
        if (not username or not password
                or any(char in value for value in (username, password) for char in "\r\n")):
            raise VpnError("Enter a username and password")
        uuid = ensure_profile(address, username)
        activate(uuid, authenticate(address, username, password))
    elif action == "disconnect":
        uuid = profile_uuid()
        if not uuid:
            raise VpnError("No Cisco VPN profile exists")
        nmcli("connection", "down", "uuid", uuid)
    return state()


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, **main()}))
    except GLib.Error:
        print(json.dumps({"ok": False, "error": "NetworkManager rejected the VPN operation"}))
        sys.exit(1)
    except (VpnError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
