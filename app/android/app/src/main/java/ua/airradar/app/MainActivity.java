package ua.airradar.app;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(AirRadarPushPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
