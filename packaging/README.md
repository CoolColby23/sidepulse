# macOS installer

The release script builds a self-contained `SidePulse.app`, signs it with
hardened runtime, wraps it in a signed PKG, submits it to Apple's notary
service, and staples the ticket.

Requirements:

- Developer ID Application and Developer ID Installer certificates
- Xcode command-line tools
- A notarytool keychain profile

Store notarization credentials once:

```sh
xcrun notarytool store-credentials sidepulse-notary \
  --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PASSWORD
```

Build, sign, notarize, and staple:

```sh
APP_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
INSTALLER_SIGN_IDENTITY="Developer ID Installer: Your Name (TEAMID)" \
NOTARY_PROFILE="sidepulse-notary" \
./packaging/build_macos_pkg.sh
```

The resulting installer is written to `dist/`. On installation it places the
app in `/Applications`, links the `sidepulse` command into `/usr/local/bin`,
and configures hooks and per-user LaunchAgents for the logged-in user.

For local packaging verification when release certificates are unavailable,
run `ALLOW_UNSIGNED=1 ./packaging/build_macos_pkg.sh`. That output is explicitly
not suitable for distribution and cannot be notarized.

For local development, build, install, and configure the standalone app in one
step:

```sh
./scripts/install-macos-local.sh
```

This installs an ad-hoc-signed `/Applications/SidePulse.app` and starts its
LaunchAgent from the frozen SidePulse executable. As a result, macOS attributes
the running menu-bar app and its permissions to SidePulse instead of Python.
Use the signed and notarized PKG workflow above for distribution to other Macs.
