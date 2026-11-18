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
from queue import Full, Queue, SimpleQueue
from threading import Event, Lock, Thread

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bridge  # noqa: E402
import core  # noqa: E402

AUTO_READY = "실행 모드 · 데모 자동매매"
RESUME_RETRIES = 20        # 시스템이 서비스를 다시 띄웠을 때 네트워크를 기다리는 횟수
RESUME_RETRY_SECONDS = 30


def on_android():
    return "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


class WakeLock:
    """화면이 꺼져도 CPU가 잠들지 않게 합니다 (안드로이드 전용)."""

    def __init__(self):
        self.lock = None

    def acquire(self):
        if not on_android():
            return
        try:
            from jnius import autoclass
            service = autoclass("org.kivy.android.PythonService").mService
            context = autoclass("android.content.Context")
            power = autoclass("android.os.PowerManager")
            manager = service.getSystemService(context.POWER_SERVICE)
            self.lock = manager.newWakeLock(power.PARTIAL_WAKE_LOCK, "rsibot:trader")
            self.lock.setReferenceCounted(False)
            self.lock.acquire()
        except Exception as exc:
            print(f"절전 방지 설정 실패 · {exc}", flush=True)

    def release(self):
        try:
            if self.lock is not None and self.lock.isHeld():
                self.lock.release()
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
        self.stop = Event()
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
        if text.startswith(AUTO_READY) and self.state == "starting" and not self.stop.is_set():
            self.set_status("running", "데모 자동매매 실행 중 (백그라운드)")
        return len(text)

    def flush(self):
        pass

    # ── 보조 스레드 ──

    def heartbeat(self):
        while not self.finished.wait(bridge.HEARTBEAT_SECONDS):  # 중지 마무리 중에도 신호 유지
            self.set_status()
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

    def authenticate(self):
        attempts = RESUME_RETRIES if self.resumed else 1
        for attempt in range(1, attempts + 1):
            try:
                return core.authenticate_demo(self.job["key"], self.job["secret"])
            except RuntimeError as exc:
                if attempt == attempts or self.stop.is_set():
                    raise
                print(f"재연결 대기 ({attempt}/{attempts}) · {exc}", flush=True)
                if self.stop.wait(RESUME_RETRY_SECONDS):
                    raise RuntimeError("중지됨") from None
        raise RuntimeError("연결 실패")

    def run(self):
        sys.stdout = sys.stderr = self
        wakelock = WakeLock()
        wakelock.acquire()
        self.set_status("starting", "데모 자동매매 다시 시작 중" if self.resumed else "데모 자동매매 시작 중")
        Thread(target=self.heartbeat, daemon=True).start()
        Thread(target=self.commands, daemon=True).start()
        if self.resumed:
            print("시스템이 앱을 다시 실행해 자동매매를 이어갑니다.", flush=True)
        result = "중지됨"
        try:
            api, hedge_mode = self.authenticate()
            api.symbol = self.job["order_symbol"]
            if not self.stop.is_set():
                core.run_symbols(api, hedge_mode, self.job["settings"], tuple(self.job["symbols"]),
                                 self.stop, self.updates, self.manual_orders, self.wake)
        except (RuntimeError, sqlite3.Error) as exc:
            print(f"중단 · {exc}", flush=True)
            result = "실행 중단 · 로그를 확인하세요."
        except Exception as exc:
            print(f"중단 · 예상하지 못한 응답 또는 실행 오류가 발생했습니다. ({type(exc).__name__})", flush=True)
            result = "실행 중단 · 로그를 확인하세요."
        finally:
            self.stop.set()
            self.wake.set()
            bridge.clear_job(self.root)   # 직접 중지·오류 종료 시에는 다시 시작하지 않습니다.
            print(f"자동매매 종료 · {result}", flush=True)
            self.finished.set()
            self.set_status("done", result)
            wakelock.release()
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
