package ua.airradar.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.widget.RemoteViews;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

/**
 * Віджет «Поточна ситуація»: показує останнє сповіщення AirRadar і час його отримання, не відкриваючи додаток.
 * Дані — лише те, що вже прийшло в службу PushService (офіційні/перевірені тексти сервера); віджет нічого не вигадує.
 */
public class StatusWidget extends AppWidgetProvider {
    static final String PREFS = "airradar_widget";

    static void save(Context ctx, String title, String text) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("title", title).putString("text", text).putLong("ts", System.currentTimeMillis()).apply();
        refresh(ctx);
    }

    static void refresh(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        int[] ids = mgr.getAppWidgetIds(new ComponentName(ctx, StatusWidget.class));
        for (int id : ids) mgr.updateAppWidget(id, build(ctx));
    }

    static RemoteViews build(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R_layout());
        long ts = sp.getLong("ts", 0);
        String title = sp.getString("title", "AirRadar AI");
        String text = sp.getString("text", "Сповіщень ще не було. Орієнтуйтесь на сирену.");
        SimpleDateFormat f = new SimpleDateFormat("HH:mm", Locale.US);
        f.setTimeZone(TimeZone.getTimeZone("Europe/Kyiv"));
        v.setTextViewText(ctx.getResources().getIdentifier("widget_title", "id", ctx.getPackageName()), title);
        v.setTextViewText(ctx.getResources().getIdentifier("widget_text", "id", ctx.getPackageName()), text);
        v.setTextViewText(ctx.getResources().getIdentifier("widget_time", "id", ctx.getPackageName()),
                ts == 0 ? "Не замінює офіційні повідомлення" : "Оновлено " + f.format(new Date(ts)) + " · не замінює офіційні повідомлення");
        v.setOnClickPendingIntent(ctx.getResources().getIdentifier("widget_root", "id", ctx.getPackageName()),
                PendingIntent.getActivity(ctx, 3, new Intent(ctx, MainActivity.class), PendingIntent.FLAG_IMMUTABLE));
        return v;
    }

    private static int R_layout() { return ua.airradar.app.R.layout.widget_status; }

    @Override public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        for (int id : ids) mgr.updateAppWidget(id, build(ctx));
    }
}
