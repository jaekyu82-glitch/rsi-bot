[app]
title = BTC ETH 데모 자동매매
package.name = rsidemobot
package.domain = org.rsibot
source.dir = .
source.include_exts = py,ttf,txt
source.exclude_dirs = .github,bin,.buildozer,__pycache__
version = 11.0
requirements = python3,kivy==2.3.0,openssl,sqlite3,pyjnius,android
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,WAKE_LOCK
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False

[buildozer]
log_level = 2
warn_on_root = 1
