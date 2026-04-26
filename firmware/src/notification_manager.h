#ifndef NOTIFICATION_MANAGER_H
#define NOTIFICATION_MANAGER_H

#include <Arduino.h>
#include "config.h"
#include "loop_task.h"

class NeatoSerial;
class SettingsManager;
class DataLogger;
class CleaningHistory;

class NotificationManager : public LoopTask {
public:
    NotificationManager(NeatoSerial& neato, SettingsManager& settings, DataLogger& logger,
                        CleaningHistory& history);

    void begin();

    // Send a test notification to the given topic (called from web server)
    void sendTestNotification(const String& topic);

private:
    void tick() override; // Called by LoopTask; skipped while fetchPending

    NeatoSerial& neato;
    SettingsManager& settings;
    DataLogger& dataLogger;
    CleaningHistory& history;

    // Previous state for transition detection
    String prevUiState;
    String prevRobotState;
    bool prevHasError = false;
    int prevErrorCode = 200; // UI_ALERT_INVALID = no error

    // Track whether the robot was cleaning before entering docking state
    bool wasCleaningBeforeDock = false;

    // Pending state fetch tracking
    bool fetchPending = false;

    // Deferred "cleaning done" notification — captured at the cleaning->idle
    // transition and held until CleaningHistory::stopCollection finalizes the
    // session stats (sessionId increments) or a wall-clock timeout elapses.
    bool donePending = false;
    uint32_t doneTriggerSessionId = 0; // sessionId observed at trigger time
    unsigned long donePendingSinceMs = 0;
    String doneHostname; // captured hostname at trigger time
    String doneTopic;    // captured topic at trigger time

    void checkTransitions();
    void flushPendingDone();
    void sendDoneNotification(const String& topic, const String& hostname, bool withStats);
    void sendNotification(const String& topic, const String& tags, const String& message);
    static bool isActiveState(const String& uiState);
};

#endif // NOTIFICATION_MANAGER_H
