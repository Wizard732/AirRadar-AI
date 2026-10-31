package ua.airradar.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Фонова служба: тримає потік ntfy (https://ntfy.sh/<тема>/json) і перетворює повідомлення на системні сповіщення.
 * Тривога (priority 5) іде в канал «Тривога» з власним гучним звуком res/raw/alarm, максимальною важливістю і вібрацією.
 * Без Firebase і без акаунтів: з'єднання вихідне, сервер нічого не відкриває.
 */
public class PushService extends Service {
    static final String PREFS = "airradar_push";
    static final String CH_SERVICE = "airradar_service", CH_ALARM = "airradar_alarm", CH_DANGER = "airradar_danger", CH_WARN = "airradar_warn", CH_INFO = "airradar_info";
    static final String BASE = "https://ntfy.sh/";
    private volatile boolean running;
    private Thread worker;
    private PowerManager.WakeLock wake;

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        createChannels(this);
        Notification n = new Notification.Builder(this, CH_SERVICE)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("AirRadar стежить за тривогами")
                .setContentText("З'єднання активне")
                .setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE))
                .build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, n, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(1, n);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (!running) {
            running = true;
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "airradar:push");
            worker = new Thread(this::loop, "airradar-push");
            worker.setDaemon(true);
            worker.start();
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    private void loop() {
        long backoff = 2000;
        while (running) {
            SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
            String topic = sp.getString("topic", "");
            if (topic.isEmpty()) { sleep(5000); continue; }
            long since = sp.getLong("since", System.currentTimeMillis() / 1000 - 120);
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(BASE + topic + "/json?since=" + since).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(90000);   // ntfy шле keepalive щоразу ≤55 с
                c.setRequestProperty("User-Agent", "AirRadarApp/1.0");
                try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
                    backoff = 2000;
                    String line;
                    while (running && (line = r.readLine()) != null) handle(sp, line);
                }
            } catch (Exception e) {
                // мережа зникла/таймаут — перепідключаємось з наростаючою паузою
            } finally {
                if (c != null) c.disconnect();
            }
            sleep(backoff);
            backoff = Math.min(backoff * 2, 60000);
        }
    }

    private void handle(SharedPreferences sp, String line) {
        if (line.isEmpty() || line.charAt(0) != '{') return;
        try {
            JSONObject j = new JSONObject(line);
            if (!"message".equals(j.optString("event"))) return;
            long time = j.optLong("time", 0);
            if (time > 0) sp.edit().putLong("since", time + 1).apply();   // не показуємо те саме двічі після перепідключення
            int priority = j.optInt("priority", 3);
            boolean danger = false;   // тег «danger» ставить сервер лише для особистої небезпеки (ціль поруч із вами)
            org.json.JSONArray tags = j.optJSONArray("tags");
            for (int i = 0; tags != null && i < tags.length(); i++) if ("danger".equals(tags.optString(i))) danger = true;
            String ackUrl = "";   // кнопка «Отримав» (view-дія ntfy → бот фіксує підтвердження)
            org.json.JSONArray acts = j.optJSONArray("actions");
            for (int i = 0; acts != null && i < acts.length(); i++) {
                JSONObject a = acts.optJSONObject(i);
                if (a != null && "view".equals(a.optString("action")) && a.optString("url").startsWith("https://t.me/")) ackUrl = a.optString("url");
            }
            String title = j.optString("title", "AirRadar"), msg = j.optString("message", "");
            StatusWidget.save(this, title, msg);
            show(this, title, msg, priority, danger, ackUrl);
        } catch (Exception ignored) { }
    }

    static void show(Context ctx, String title, String text, int priority) {
        show(ctx, title, text, priority, false);
    }

    static void show(Context ctx, String title, String text, int priority, boolean danger) {
        show(ctx, title, text, priority, danger, "");
    }

    static void show(Context ctx, String title, String text, int priority, boolean danger, String ackUrl) {
        String ch = priority >= 5 ? (danger ? CH_DANGER : CH_ALARM) : priority == 4 ? CH_WARN : CH_INFO;
        Notification.Builder b = new Notification.Builder(ctx, ch)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setAutoCancel(true)
                .setCategory(priority >= 5 ? Notification.CATEGORY_ALARM : Notification.CATEGORY_MESSAGE)
                .setContentIntent(PendingIntent.getActivity(ctx, 0, new Intent(ctx, MainActivity.class), PendingIntent.FLAG_IMMUTABLE));
        if (ackUrl != null && !ackUrl.isEmpty()) {
            Intent ack = new Intent(Intent.ACTION_VIEW, Uri.parse(ackUrl));
            b.addAction(new Notification.Action.Builder(null, "Отримав", PendingIntent.getActivity(ctx, 2, ack, PendingIntent.FLAG_IMMUTABLE)).build());
        }
        if (priority >= 5) b.setFullScreenIntent(PendingIntent.getActivity(ctx, 1, new Intent(ctx, MainActivity.class), PendingIntent.FLAG_IMMUTABLE), false);
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        nm.notify((int) (System.currentTimeMillis() % Integer.MAX_VALUE), b.build());
    }

    static void createChannels(Context ctx) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        NotificationChannel svc = new NotificationChannel(CH_SERVICE, "Фонове з'єднання", NotificationManager.IMPORTANCE_MIN);
        svc.setDescription("Показує, що AirRadar слухає тривоги");
        nm.createNotificationChannel(svc);

        NotificationChannel alarm = new NotificationChannel(CH_ALARM, "Тривога", NotificationManager.IMPORTANCE_HIGH);
        alarm.setDescription("Повітряна тривога та небезпека поруч — гучний сигнал");
        alarm.setSound(Uri.parse("android.resource://" + ctx.getPackageName() + "/" + R.raw.alarm),
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        alarm.enableVibration(true);
        alarm.setVibrationPattern(new long[]{0, 500, 200, 500, 200, 800});
        alarm.enableLights(true);
        alarm.setBypassDnd(true);   // спрацює, якщо користувач дозволив у системі «Доступ до режиму Не турбувати»
        alarm.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(alarm);

        // «Небезпека поруч» (ціль курсом на вашу точку) — власний сигнал, відмінний від загальної повітряної тривоги.
        NotificationChannel danger = new NotificationChannel(CH_DANGER, "Небезпека поруч", NotificationManager.IMPORTANCE_HIGH);
        danger.setDescription("Ціль летить до вашої точки — гучний сигнал");
        danger.setSound(Uri.parse("android.resource://" + ctx.getPackageName() + "/" + R.raw.danger_beeps),
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        danger.enableVibration(true);
        danger.setVibrationPattern(new long[]{0, 300, 150, 300, 150, 300, 150, 600});
        danger.enableLights(true);
        danger.setBypassDnd(true);
        danger.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(danger);

        NotificationChannel warn = new NotificationChannel(CH_WARN, "Попередження", NotificationManager.IMPORTANCE_HIGH);
        warn.setDescription("Можливо до вашого міста");
        nm.createNotificationChannel(warn);

        NotificationChannel info = new NotificationChannel(CH_INFO, "Інформація", NotificationManager.IMPORTANCE_DEFAULT);
        info.setDescription("Відбій та інші повідомлення");
        nm.createNotificationChannel(info);
    }

    private static void sleep(long ms) { try { Thread.sleep(ms); } catch (InterruptedException ignored) { } }
}
