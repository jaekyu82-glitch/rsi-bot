"""백그라운드 자동매매 서비스 (안드로이드 포그라운드 서비스).

화면(main.py)과 별도 프로세스에서 돌기 때문에 화면이 꺼지거나, 다른 앱을 쓰거나,
최근 앱 목록에서 앱을 닫아도 자동매매가 계속됩니다. 실행 중에는 상단에 알림이 표시됩니다.
매매 로직은 core.py를 그대로 사용합니다.
"""

import os
import sqlite3
import sys
import time
from pathlib import Path
from queue import Empty, Full, Queue, SimpleQueue
from threading import Event, Lock, Thread

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
import core  # noqa: E402

AUTO_READY = "실행 모드 · 데모 자동매매"
RUNNING_MESSAGE = "데모 자동매매 실행 중 (백그라운드)"
RECONNECT_SECONDS = 30     # 인터넷이 끊기면 이 간격으로 계속 재연결합니다.
LOCK_CHECK_SECONDS = 60    # 절전 방지 잠금이 풀렸는지 확인하는 간격
# 이런 오류는 잠깐의 인터넷·서버 문제이므로 자동매매를 끝내지 않고 다시 연결합니다.
TRANSIENT_ERRORS = ("연결 실패 또는 응답 시간 초과", "JSON 응답을 받지 못했습니다", "JSON 오류 응답을 받지 못했습니다",
                    "HTTP 429", "HTTP 418", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")


def on_android():
    return "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


def is_transient(exc):
    text = str(exc)
    return any(marker in text for marker in TRANSIENT_ERRORS)


def android_service():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonService").mService


def schedule_watchdog():
    """10분마다 서비스가 살아 있는지 확인하는 감시 장치를 예약합니다 (재부팅·업데이트 후 자동 재시작 포함)."""
    if not on_android():
        return
    try:
        from jnius import autoclass
        autoclass("org.rsibot.rsidemobot.TraderWatchdog").schedule(android_service())
    except Exception as exc:
        print(f"감시 장치 예약 실패 · {exc}", flush=True)


class WakeLock:
    """화면이 꺼져도 CPU와 와이파이가 잠들지 않게 합니다 (안드로이드 전용)."""

    def __init__(self):
        self.lock = None
        self.wifi = None

    def acquire(self):
        if not on_android():
            return
        try:
            from jnius import autoclass
            service = android_service()
            context = autoclass("android.content.Context")
            if self.lock is None:
                power = autoclass("android.os.PowerManager")
                manager = service.getSystemService(context.POWER_SERVICE)
                self.lock = manager.newWakeLock(power.PARTIAL_WAKE_LOCK, "rsibot:trader")
                self.lock.setReferenceCounted(False)
            if not self.lock.isHeld():
                self.lock.acquire()
        except Exception as exc:
            print(f"절전 방지 설정 실패 · {exc}", flush=True)
        try:
            from jnius import autoclass
            if self.wifi is None:
                context = autoclass("android.content.Context")
                manager = android_service().getApplicationContext().getSystemService(context.WIFI_SERVICE)
                if manager is None:
                    return
                self.wifi = manager.createWifiLock(3, "rsibot:trader")  # WIFI_MODE_FULL_HIGH_PERF
                self.wifi.setReferenceCounted(False)
            if not self.wifi.isHeld():
                self.wifi.acquire()
        except Exception as exc:
            print(f"와이파이 절전 방지 설정 실패 · {exc}", flush=True)

    def release(self):
        for lock in (self.wifi, self.lock):
            try:
                if lock is not None and lock.isHeld():
                    lock.release()
            except Exception:
                pass


class Service:
    def __init__(self, root, job, resumed):
        self.root = root
        self.job = job
        self.resumed = resumed
        self.session = job["session"]
        self.state = "starting"
        self.message = "자동매매 시작 중"
        self.status_lock = Lock()
        self.log_lock = Lock()
        self.log = open(bridge.log_path(root), "a", encoding="utf-8")
        self.stop = Event()        # 사용자가 중지를 눌렀을 때
        self.run_stop = Event()    # 이번 실행 회차를 끝낼 때 (중지 또는 오류)
        self.started = False       # 한 번이라도 자동매매가 정상 실행됐는지
        self.wakelock = WakeLock()
        self.finished = Event()
        self.wake = Event()
        self.updates = SimpleQueue()
        self.manual_orders = Queue(maxsize=1)

    # ── 상태·로그 ──

    def set_status(self, state=None, message=None):
        with self.status_lock:
            if state is not None:
                self.state = state
            if message is not None:
                self.message = message
            bridge.write_status(self.root, state=self.state, message=self.message, session=self.session,
                                symbols=list(self.job["symbols"]), pid=os.getpid())

    def write(self, text):
        if not text:
            return 0
        with self.log_lock:
            self.log.write(text)
            self.log.flush()
        if text.startswith(AUTO_READY) and not self.stop.is_set():
            self.started = True
            if self.state != "running" or self.message != RUNNING_MESSAGE:
                self.set_status("running", RUNNING_MESSAGE)
        return len(text)

    def flush(self):
        pass

    # ── 보조 스레드 ──

    def heartbeat(self):
        try:
            self._heartbeat()
        finally:
            if on_android():  # 안드로이드 기능을 쓴 스레드는 끝나기 전에 연결을 풀어야 합니다.
                try:
                    import jnius
                    jnius.detach()
                except Exception:
                    pass

    def _heartbeat(self):
        last_check = time.monotonic()
        while not self.finished.wait(bridge.HEARTBEAT_SECONDS):  # 중지 마무리 중에도 신호 유지
            self.set_status()
            if time.monotonic() - last_check >= LOCK_CHECK_SECONDS:
                last_check = time.monotonic()
                self.wakelock.acquire()   # 기기가 잠금을 풀어 버렸으면 다시 잡습니다.
                schedule_watchdog()
            with self.log_lock:
                if self.log.tell() > bridge.LOG_LIMIT:
                    self.log.close()
                    bridge.trim_log(self.root)
                    self.log = open(bridge.log_path(self.root), "a", encoding="utf-8")

    def commands(self):
        while not self.finished.wait(0.3):
            for command in bridge.take_commands(self.root):
                kind = command[0]
                if kind == "stop":
                    print("중지 요청 · 진행 중인 요청을 마무리하는 중", flush=True)
                    self.set_status(message="중지 요청 · 진행 중인 요청을 마무리하는 중")
                    self.stop.set()
                    self.run_stop.set()
                    self.wake.set()
                elif kind == "update":
                    settings = command[1]
                    self.job["settings"] = settings
                    bridge.save_job(self.root, self.job)  # 재시작 시에도 새 조건 사용
                    self.updates.put(settings)
                    self.wake.set()
                elif kind == "manual":
                    _, symbol, direction, order = command
                    try:
                        self.manual_orders.put_nowait((symbol, direction, order, time.monotonic() + 15))
                    except Full:
                        print("앞선 시장가 진입 요청을 처리 중입니다.", flush=True)
                    self.wake.set()

    # ── 실행 ──

    def run_once(self):
        api, hedge_mode = core.authenticate_demo(self.job["key"], self.job["secret"])
        api.symbol = self.job["order_symbol"]
        if self.stop.is_set():
            return
        while True:  # 대기 중 들어온 조건 변경은 새 실행 회차에 바로 반영합니다.
            try:
                self.job["settings"] = self.updates.get_nowait()
            except Empty:
                break
        self.run_stop = Event()
        if self.stop.is_set():
            return
        core.run_symbols(api, hedge_mode, self.job["settings"], tuple(self.job["symbols"]),
                         self.run_stop, self.updates, self.manual_orders, self.wake)

    def run_until_stopped(self):
        """인터넷·서버 문제로 끊겨도 중지를 누를 때까지 계속 다시 연결합니다."""
        attempt = 0
        while not self.stop.is_set():
            try:
                self.run_once()
                return
            except Exception as exc:
                if self.stop.is_set():
                    return
                # 처음 시작할 때의 오류는 바로 알려 줍니다. 실행 중이었거나 시스템이 다시 띄운 경우에만 재연결합니다.
                if not ((self.started or self.resumed) and is_transient(exc)):
                    raise
                attempt += 1
                print(f"연결 끊김 · {RECONNECT_SECONDS}초 후 재연결 ({attempt}회째) · {exc}", flush=True)
                self.set_status(message="인터넷 연결 대기 중 · 자동으로 재연결합니다 (백그라운드)")
                if self.stop.wait(RECONNECT_SECONDS):
                    return

    def run(self):
        sys.stdout = sys.stderr = self
        self.wakelock.acquire()
        schedule_watchdog()
        self.set_status("starting", "데모 자동매매 다시 시작 중" if self.resumed else "데모 자동매매 시작 중")
        Thread(target=self.heartbeat, daemon=True).start()
        Thread(target=self.commands, daemon=True).start()
        if self.resumed:
            print("시스템이 앱을 다시 실행해 자동매매를 이어갑니다.", flush=True)
        result = "중지됨"
        try:
            self.run_until_stopped()
        except (RuntimeError, sqlite3.Error) as exc:
            print(f"중단 · {exc}", flush=True)
            result = "실행 중단 · 로그를 확인하세요."
        except Exception as exc:
            print(f"중단 · 예상하지 못한 응답 또는 실행 오류가 발생했습니다. ({type(exc).__name__})", flush=True)
            result = "실행 중단 · 로그를 확인하세요."
        finally:
            self.stop.set()
            self.run_stop.set()
            self.wake.set()
            bridge.clear_job(self.root)   # 직접 중지·오류 종료 시에는 다시 시작하지 않습니다.
            print(f"자동매매 종료 · {result}", flush=True)
            self.finished.set()
            self.set_status("done", result)
            self.wakelock.release()
            with self.log_lock:
                self.log.close()


def main():
    root = bridge.root_dir()
    job = bridge.load_job(root)
    if job is None:
        status = bridge.read_status(root)
        if status is not None and status.get("state") != "done":
            bridge.write_status(root, **{**status, "state": "done", "message": "중지됨"})
        return
    # 화면이 시작하면 상태를 'starting'으로 적어 둡니다. 그렇지 않으면 시스템이 다시 띄운 경우입니다.
    status = bridge.read_status(root)
    resumed = not (status and status.get("session") == job["session"] and status.get("state") == "starting"
                   and not status.get("pid"))
    core.configure_data_dir(job["data_dir"])
    Service(root, job, resumed).run()


if __name__ == "__main__":
    main()
