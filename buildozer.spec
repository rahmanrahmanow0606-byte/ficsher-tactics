
[app]
title = Ficsher Tactics
package.name = ficsher-tactics
package.domain = org.ficsher
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,txt
version = 0.1.0
requirements = python3,kivy==2.3.1,python-chess==1.999
orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.api = 35
android.minapi = 23
android.ndk = 28c
android.archs = arm64-v8a
android.add_libs_arm64_v8a = libs/arm64-v8a/libstockfish.so
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
