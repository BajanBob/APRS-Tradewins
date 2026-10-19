[app]
title = Tradewind
package.name = tradewind
package.domain = org.tradewind.aprs
version = 1.0

# The GitHub workflow copies the app files into android_build/src before building
source.dir = src
source.include_exts = py,html,png

# Pure-Python on Android: APRS-IS + KISS TCP (no numpy / soundcard / USB serial)
requirements = python3,certifi

icon.filename = %(source.dir)s/icon.png
orientation = portrait
fullscreen = 0

android.permissions = INTERNET, ACCESS_NETWORK_STATE, WAKE_LOCK
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True

# Run the Python web server and show the face plate in a full-screen WebView
p4a.bootstrap = webview
p4a.port = 8073

[buildozer]
log_level = 2
warn_on_root = 0
