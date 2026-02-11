#ifndef FIRMWARE_MANAGER_H
#define FIRMWARE_MANAGER_H

#include <ESPAsyncWebServer.h>
#include <StreamString.h>
#include <Update.h>
#include <functional>
#include "config.h"

class FirmwareManager {
public:
    FirmwareManager(AsyncWebServer& server);

    void begin();

    void loop();

    bool isInProgress() const { return updateInProgress; }

    // Logger callback: (event, json_payload)
    using LogCallback = std::function<void(const String&, const String&)>;
    void setLogger(LogCallback logger) { loggerCallback = logger; }

private:
    AsyncWebServer& server;
    bool updateInProgress = false;
    bool rebootPending = false;
    unsigned long rebootRequestMs = 0;
    size_t currentProgress = 0;
    String updateError;
    LogCallback loggerCallback;
};

#endif // FIRMWARE_MANAGER_H
