#ifndef NOTIFICATION_MANAGER_H
#define NOTIFICATION_MANAGER_H

#include <Arduino.h>
#include "config.h"

class NeatoSerial;
class SettingsManager;
class DataLogger;

class NotificationManager {
public:
    NotificationManager(NeatoSerial& neato, SettingsManager& settings, DataLogger& logger);

    void begin();
    void loop();

    // Send a test notification to the given topic (called from web server)
    void sendTestNotification(const String& topic);

private:
    NeatoSerial& neato;
    SettingsManager& settings;
    DataLogger& dataLogger;

    // Previous state for transition detection
    String prevUiState;
    bool prevHasError = false;
    int prevErrorCode = 200; // UI_ALERT_INVALID = no error

    // Adaptive polling timer
    unsigned long lastCheck = 0;
    unsigned long checkInterval = NOTIF_INTERVAL_IDLE_MS;

    // Pending state fetch tracking
    bool fetchPending = false;

    void checkTransitions();
    void sendNotification(const String& topic, const String& tags, const String& message);
    static bool isActiveState(const String& uiState);
};

#endif // NOTIFICATION_MANAGER_H
