# Omarchy Cisco VPN

A bar widget for connecting to Cisco AnyConnect-compatible VPNs on
[Omarchy](https://omarchy.org/). OpenConnect authenticates to the gateway,
NetworkManager manages the tunnel, and a Quickshell popup shows the status and
connect/disconnect controls.

## Install

Install the Arch dependencies:

```sh
omarchy pkg add networkmanager-openconnect python-gobject
```

Install the widget from this repository:

```sh
omarchy plugin add https://github.com/Jeffe747/omarchy-cisco-vpn.git --enable
```

Click the VPN icon in the right-hand bar section. Enter the HTTPS VPN server,
username, and password, then choose **Connect**. While connecting, use
**Cancel** to stop the attempt. When connected, the button becomes
**Disconnect**. The server can be entered as a hostname or an HTTPS URL.

If you used the earlier local proof of concept, disable its widget first with
`omarchy plugin disable local.cisco-vpn`. The installed widget uses the same
`Omarchy Cisco VPN` NetworkManager profile, so your saved server and username
remain available. Do not enable both widgets at the same time.

## Credentials and limitations

The server and username are stored in the NetworkManager profile; autoconnect
is disabled. The password is sent over stdin to OpenConnect for one
authentication attempt and is not saved in the profile or passed as a command
argument. A short-lived session cookie is passed through NetworkManager's
in-memory profile and cleared after the attempt. The popup does not print raw
OpenConnect errors because those may contain sensitive details. Never commit
your VPN server details, credentials, NetworkManager profile, or logs to this
repository.

This initial release supports a single Cisco AnyConnect username/password
flow. VPN groups, OTP/MFA prompts, browser SSO, client certificates, and
custom certificate trust are not supported. Certificate validation is never
disabled. A successful login depends on your gateway's authentication policy.

## Development

Run the backend tests on Arch with `python-gobject` and NetworkManager
installed:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
omarchy plugin validate .
```

The shell loads `Panel.qml` directly from this repository when installed.
Changes to files in an installed plugin trigger an Omarchy shell reload.
For a QML edit that does not take effect after hot-reload, run
`omarchy restart shell`.

Licensed under [MIT](LICENSE).
