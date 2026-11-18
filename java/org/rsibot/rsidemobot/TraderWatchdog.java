package org.rsibot.rsidemobot;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import java.io.File;

/**
 * 백그라운드 자동매매 감시 장치.
 *
 * - 폰을 다시 켜거나 앱을 업데이트하면, 진행 중이던 자동매매(service/job.pickle)를 다시 시작합니다.
 * - 10분마다 깨어나 서비스 신호(service/status.json)가 끊겼으면 서비스를 다시 띄웁니다.
 * 사용자가 중지하면 job.pickle이 지워지므로 아무것도 하지 않고 감시도 끝납니다.
 */
public class TraderWatchdog extends BroadcastReceiver {
    private static final String TAG = "rsibot-watchdog";
    private static final String ACTION = "org.rsibot.rsidemobot.WATCHDOG";
    private static final long INTERVAL_MS = 10 * 60 * 1000L;
    private static final long STALE_MS = 90 * 1000L;
    private static final String TITLE = "BTC·ETH 데모 자동매매";
    private static final String TEXT = "자동매매 실행 중 · 누르면 앱이 열립니다";

    private static File serviceFile(Context ctx, String name) {
        return new File(new File(ctx.getFilesDir(), "service"), name);
    }

    private static boolean hasJob(Context ctx) {
        return serviceFile(ctx, "job.pickle").exists();
    }

    private static PendingIntent alarmIntent(Context ctx) {
        Intent intent = new Intent(ctx, TraderWatchdog.class).setAction(ACTION);
        return PendingIntent.getBroadcast(ctx, 7301, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    /** 다음 감시 시각을 예약합니다. 절전(Doze) 중에도 깨어납니다. */
    public static void schedule(Context context) {
        try {
            Context ctx = context.getApplicationContext();
            AlarmManager alarms = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
            if (alarms == null) {
                return;
            }
            long at = SystemClock.elapsedRealtime() + INTERVAL_MS;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                alarms.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, alarmIntent(ctx));
            } else {
                alarms.set(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, alarmIntent(ctx));
            }
        } catch (RuntimeException exc) {
            Log.w(TAG, "감시 예약 실패", exc);
        }
    }

    /** 진행 중인 자동매매가 있는데 서비스 신호가 끊겼으면 서비스를 다시 시작합니다. */
    public static void ensureRunning(Context context) {
        Context ctx = context.getApplicationContext();
        if (!hasJob(ctx)) {
            return;
        }
        File status = serviceFile(ctx, "status.json");
        if (status.exists() && System.currentTimeMillis() - status.lastModified() < STALE_MS) {
            return;  // 서비스가 정상적으로 신호를 보내는 중
        }
        try {
            Intent intent = ServiceTrader.getDefaultIntent(ctx, "", TITLE, TEXT, "");
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(intent);
            } else {
                ctx.startService(intent);
            }
            Log.i(TAG, "백그라운드 자동매매 서비스를 다시 시작했습니다.");
        } catch (RuntimeException exc) {
            Log.w(TAG, "서비스 재시작 실패 (다음 감시 때 다시 시도)", exc);
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!hasJob(context)) {
            return;  // 사용자가 중지했거나 자동매매가 끝났으면 감시도 끝냅니다.
        }
        ensureRunning(context);
        schedule(context);
    }
}
