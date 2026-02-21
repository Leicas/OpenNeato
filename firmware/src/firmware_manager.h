#ifndef FIRMWARE_MANAGER_H
#define FIRMWARE_MANAGER_H

#include <Arduino.h>
#include <Update.h>
#include <StreamString.h>
#include "config.h"

class DataLogger;

class FirmwareManager {
public:
    explicit FirmwareManager(DataLogger& logger);

    void loop();

    // Version info
    const char *getFirmwareVersion() const { return FIRMWARE_VERSION; }
    String getChipModel() const { return ESP.getChipModel(); }

    // Update lifecycle
    bool beginUpdate(const String& md5Hash = "");
    bool writeChunk(uint8_t *data, size_t len);
    bool endUpdate();

    // State queries
    bool isInProgress() const { return updateInProgress; }
    size_t getProgress() const { return currentProgress; }
    const String& getError() const { return updateError; }

private:
    DataLogger& dataLogger;

    bool validateChip(uint8_t *data, size_t len);

    bool updateInProgress = false;
    bool chipValidated = false;
    bool rebootPending = false;
    unsigned long rebootRequestMs = 0;
    size_t currentProgress = 0;
    String updateError;
};

#endif // FIRMWARE_MANAGER_H
