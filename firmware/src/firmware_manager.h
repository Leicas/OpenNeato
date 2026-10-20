#ifndef FIRMWARE_MANAGER_H
#define FIRMWARE_MANAGER_H

#include <Arduino.h>
#include <Update.h>
#include <StreamString.h>
#include "config.h"
#include "loop_task.h"

class DataLogger;

class FirmwareManager : public LoopTask {
public:
    explicit FirmwareManager(DataLogger& logger);

    // Version info
    const char *getFirmwareVersion() const { return FIRMWARE_VERSION; }
    const char *getChipModel() const { return CHIP_MODEL; }
    // Board ID: names this build's release file, openneato-<id>-firmware.bin. Boards that share a chip
    // ship different files (e.g. "esp32-c3" and "seeed-xiao-esp32c3"), so the chip alone cannot pick one.
    String getBoardId() const;
    const char *getBoardName() const { return BOARD_NAME; }
    const char *getBoardMaker() const { return BOARD_MAKER; }

    // Update lifecycle
    bool beginUpdate(const String& md5Hash = "");
    bool writeChunk(uint8_t *data, size_t len);
    bool endUpdate();

    // State queries
    bool isInProgress() const { return updateInProgress; }
    size_t getProgress() const { return currentProgress; }
    const String& getError() const { return updateError; }

private:
    void tick() override; // Runs every 250ms — checks rebootPending flag

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
