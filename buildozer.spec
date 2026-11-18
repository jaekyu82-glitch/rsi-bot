[app]
title = BTC ETH 데모 자동매매
package.name = rsidemobot
package.domain = org.rsibot
source.dir = .
source.include_exts = py,ttf,txt
source.exclude_dirs = .github,bin,.buildozer,__pycache__
version = 12.0
requirements = python3,kivy==2.3.1,openssl,sqlite3,pyjnius,android,charset-normalizer@https://files.pythonhosted.org/packages/fc/ad/d07d7862a62ffa6d79d68074d14823243dd235a77c45262acbf6adeb28bf/charset_normalizer-3.5.2-py3-none-any.whl
# charset-normalizer: Kivy가 가져오는 패키지. 빌드 도구가 안드로이드 전용 버전을 설치하지 못해 순수 파이썬 버전을 지정
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,WAKE_LOCK,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,POST_NOTIFICATIONS,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
# 백그라운드 자동매매 서비스: 화면과 별도 프로세스, 상단 알림 표시, 앱을 닫아도 계속 실행(sticky)
services = Trader:service.py:foreground:sticky:foregroundServiceType=dataSync
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False

[buildozer]
log_level = 2
warn_on_root = 1
