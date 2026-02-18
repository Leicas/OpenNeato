#include "firmware_manager.h"
#include <esp_chip_info.h>

// ESP32 image extended header byte 12 contains the chip ID.
// The esp_chip_info model enum uses the same values (CHIP_ESP32=1, CHIP_ESP32S2=2,
// CHIP_ESP32C3=5, CHIP_ESP32S3=9, etc.), so we compare directly.
bool FirmwareManager::validateChip(uint8_t *data, size_t len) {
    if (len < 16) {
        return true; // Not enough data yet, defer validation
    }
    esp_chip_info_t info;
    esp_chip_info(&info);
    auto binChipId = static_cast<uint8_t>(data[12]);
    auto expected = static_cast<uint8_t>(info.model);
    if (binChipId != expected) {
        updateError = "Firmware chip mismatch: file targets a different ESP32 variant";
        LOG("FW", "Chip mismatch: binary has chip ID %u, expected %u", binChipId, expected);
        return false;
    }
    LOG("FW", "Chip ID validated: %u", binChipId);
    return true;
}

bool FirmwareManager::beginUpdate(const String& md5Hash) {
    currentProgress = 0;
    chipValidated = false;
    updateError = "";

    if (!md5Hash.isEmpty()) {
        if (!Update.setMD5(md5Hash.c_str())) {
            updateError = "MD5 parameter invalid";
            LOG("FW", "%s", updateError.c_str());
            return false;
        }
        LOG("FW", "MD5 hash set: %s", md5Hash.c_str());
    }

    LOG("FW", "Update started");
    updateInProgress = true;
    if (loggerCallback) {
        loggerCallback("start", {});
    }

    if (!Update.begin(UPDATE_SIZE_UNKNOWN, U_FLASH)) {
        StreamString err;
        Update.printError(err);
        updateError = err.c_str();
        LOG("FW", "Failed to start: %s", updateError.c_str());
        updateInProgress = false;
        return false;
    }

    return true;
}

bool FirmwareManager::writeChunk(uint8_t *data, size_t len) {
    if (updateError.length()) {
        return false;
    }

    // Validate chip ID from image header on first chunk
    if (!chipValidated && len >= 16) {
        if (!validateChip(data, len)) {
            Update.abort();
            updateInProgress = false;
            return false;
        }
        chipValidated = true;
    }

    if (len && Update.write(data, len) != len) {
        StreamString err;
        Update.printError(err);
        updateError = err.c_str();
        LOG("FW", "Write error: %s", updateError.c_str());
        return false;
    }

    currentProgress += len;
    return true;
}

bool FirmwareManager::endUpdate() {
    if (!Update.end(true)) {
        StreamString err;
        Update.printError(err);
        updateError = err.c_str();
        LOG("FW", "Finalize error: %s", updateError.c_str());
        return false;
    }

    LOG("FW", "Update successful (%zu bytes)", currentProgress);

    if (loggerCallback) {
        loggerCallback("end", {{"ok", "true", FIELD_BOOL}});
    }

    rebootRequestMs = millis();
    rebootPending = true;
    return true;
}

void FirmwareManager::loop() {
    if (rebootPending && millis() - rebootRequestMs > 2000) {
        LOG("FW", "Rebooting...");
        ESP.restart();
    }
}
