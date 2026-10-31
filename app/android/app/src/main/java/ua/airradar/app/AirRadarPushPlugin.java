package ua.airradar.app;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import android.net.Uri;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/** Міст JS ↔ Android: дозвіл на сповіщення, старт/стоп фонової служби, виняток з економії батареї. */
@CapacitorPlugin(name = "AirRadarPush", permissions = {
        @Permission(strings = {Manifest.permission.POST_NOTIFICATIONS}, alias = "notifications")})
public class AirRadarPushPlugin extends Plugin {

    @PluginMethod
    public void start(PluginCall call) {
        String topic = call.getString("topic", "");
        if (!topic.matches("^[A-Za-z0-9_-]{6,64}$")) { call.reject("bad topic"); return; }
        if (Build.VERSION.SDK_INT >= 33 && getPermissionState("notifications") != PermissionState.GRANTED) {
            requestPermissionForAlias("notifications", call, "afterPermission");
            return;
        }
        launch(call, topic);
    }

    @PermissionCallback
    private void afterPermission(PluginCall call) {
        if (getPermissionState("notifications") == PermissionState.GRANTED) launch(call, call.getString("topic", ""));
        else { JSObject r = new JSObject(); r.put("status", "denied"); call.resolve(r); }
    }

    private void launch(PluginCall call, String topic) {
        Context ctx = getContext();
        SharedPreferences sp = ctx.getSharedPreferences(PushService.PREFS, Context.MODE_PRIVATE);
        if (!topic.equals(sp.getString("topic", ""))) sp.edit().putString("topic", topic).putLong("since", System.currentTimeMillis() / 1000).apply();
        PushService.createChannels(ctx);
        ctx.startForegroundService(new Intent(ctx, PushService.class));
        JSObject r = new JSObject();
        r.put("status", "ready");
        call.resolve(r);
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Context ctx = getContext();
        ctx.getSharedPreferences(PushService.PREFS, Context.MODE_PRIVATE).edit().remove("topic").apply();
        ctx.stopService(new Intent(ctx, PushService.class));
        call.resolve();
    }

    @PluginMethod
    public void test(PluginCall call) {
        PushService.show(getContext(), "🧪 ТЕСТ: НЕБЕЗПЕКА ПОРУЧ", "Це перевірка сигналу. Реальної загрози немає.", 5, true);
        call.resolve();
    }

    @PluginMethod
    public void batteryExemption(PluginCall call) {
        Context ctx = getContext();
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        JSObject r = new JSObject();
        boolean ignoring = pm.isIgnoringBatteryOptimizations(ctx.getPackageName());
        r.put("ignoring", ignoring);
        if (!ignoring && Boolean.TRUE.equals(call.getBoolean("request", false))) {
            Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + ctx.getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(i);
        }
        call.resolve(r);
    }
}
