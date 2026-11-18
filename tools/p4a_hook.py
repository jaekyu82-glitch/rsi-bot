"""python-for-android 빌드 훅: 감시 장치(TraderWatchdog)를 AndroidManifest.xml에 등록합니다.

buildozer.spec 의 p4a.hook 으로 연결됩니다. 앱에는 포함되지 않습니다.
"""

from pathlib import Path

MARKER = "org.rsibot.rsidemobot.TraderWatchdog"
RECEIVER = """
        <receiver android:name="org.rsibot.rsidemobot.TraderWatchdog"
                  android:enabled="true"
                  android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED" />
                <action android:name="android.intent.action.QUICKBOOT_POWERON" />
                <action android:name="com.htc.intent.action.QUICKBOOT_POWERON" />
                <action android:name="android.intent.action.MY_PACKAGE_REPLACED" />
            </intent-filter>
        </receiver>
    </application>"""


def _patch(path):
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return
    if text.count("</application>") != 1:
        raise RuntimeError(f"{path}: </application> 위치를 찾을 수 없습니다.")
    path.write_text(text.replace("</application>", RECEIVER.lstrip("\n"), 1), encoding="utf-8")
    print(f"[p4a_hook] TraderWatchdog 등록 · {path}")


def before_apk_assemble(toolchain):
    # 이 시점의 작업 폴더는 배포 폴더(dist)이며 매니페스트가 막 만들어진 상태입니다.
    for name in ("src/main/AndroidManifest.xml", "AndroidManifest.xml"):
        path = Path(name)
        if path.exists():
            _patch(path)
