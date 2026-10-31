package ua.airradar.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Після перезавантаження телефона або оновлення додатка знову запускає службу, якщо користувач підключив тему. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context ctx, Intent intent) {
        String topic = ctx.getSharedPreferences(PushService.PREFS, Context.MODE_PRIVATE).getString("topic", "");
        if (topic.isEmpty()) return;
        try {
            ctx.startForegroundService(new Intent(ctx, PushService.class));
        } catch (Exception e) {
            // Система не дозволила автозапуск: краще чесно попросити відкрити додаток, ніж мовчки лишитися без сигналу.
            PushService.show(ctx, "AirRadar не слідкує за тривогами", "Відкрийте додаток, щоб відновити сигнал після перезавантаження.", 4);
        }
    }
}
