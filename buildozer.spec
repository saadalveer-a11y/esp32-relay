[app]
title = ESP32 Power Relay
package.name = esp32relay
package.domain = org.upstart
source.dir = .
source.include_exts = py
version = 1.0
requirements = python3,kivy==2.3.0,paho-mqtt
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE
android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
