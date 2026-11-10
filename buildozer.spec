[app]
title = Granny Martha
package.name = grannymartha
package.domain = org.grannymartha
version = 1.0
source.dir = src
source.include_exts = py,html,png
requirements = python3
icon.filename = %(source.dir)s/icon.png
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
android.numeric_version = 100
android.accept_sdk_license = True
p4a.bootstrap = webview
p4a.port = 8074

[buildozer]
log_level = 2
warn_on_root = 0
