#include "firmware_manager.h"

FirmwareManager::FirmwareManager(AsyncWebServer& server) : server(server) {}

void FirmwareManager::begin() {
    // GET /api/firmware/version — current firmware version
    server.on("/api/firmware/version", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->send(200, "application/json", R"({"version":")" FIRMWARE_VERSION R"("})");
    });

    // POST /api/firmware/update?hash=<md5> — single-request firmware upload
    server.on(
            "/api/firmware/update", HTTP_POST,
            // Response handler (called after upload completes)
            [this](AsyncWebServerRequest *request) {
                bool ok = !Update.hasError();
                if (loggerCallback) {
                    loggerCallback("end", String(R"("ok":)") + (ok ? "true" : "false"));
                }

                AsyncWebServerResponse *response =
                        request->beginResponse(ok ? 200 : 400, "text/plain", ok ? "OK" : updateError.c_str());
                response->addHeader("Connection", "close");
                request->send(response);

                if (ok) {
                    rebootRequestMs = millis();
                    rebootPending = true;
                } else {
                    updateInProgress = false;
                }
            },
            // Upload handler (called per chunk)
            [this](AsyncWebServerRequest *request, String filename, size_t index, uint8_t *data, size_t len,
                   bool final) {
                // First chunk: initialize update session
                if (!index) {
                    currentProgress = 0;
                    updateError = "";

                    // Set MD5 hash for integrity check if provided
                    if (request->hasParam("hash")) {
                        String hash = request->getParam("hash")->value();
                        if (!Update.setMD5(hash.c_str())) {
                            updateError = "MD5 parameter invalid";
                            LOG("FW", "%s", updateError.c_str());
                            return;
                        }
                        LOG("FW", "MD5 hash set: %s", hash.c_str());
                    }

                    LOG("FW", "Update started");
                    updateInProgress = true;
                    if (loggerCallback) {
                        loggerCallback("start", "");
                    }

                    if (!Update.begin(UPDATE_SIZE_UNKNOWN, U_FLASH)) {
                        StreamString err;
                        Update.printError(err);
                        updateError = err.c_str();
                        LOG("FW", "Failed to start: %s", updateError.c_str());
                        updateInProgress = false;
                        return;
                    }
                }

                // Skip writing if a previous chunk already failed
                if (updateError.length()) {
                    return;
                }

                if (len) {
                    if (Update.write(data, len) != len) {
                        StreamString err;
                        Update.printError(err);
                        updateError = err.c_str();
                        LOG("FW", "Write error: %s", updateError.c_str());
                        return;
                    }
                    currentProgress += len;

                    // Report progress at most once per second
                    static unsigned long lastProgressMs = 0;
                    unsigned long now = millis();
                    if (now - lastProgressMs >= 1000) {
                        auto percent = static_cast<uint8_t>(
                                request->contentLength() > 0 ? static_cast<float>(currentProgress) * 100.0f /
                                                                       static_cast<float>(request->contentLength())
                                                             : 0);
                        LOG("FW", "Progress: %u%% (%zu/%zu bytes)", percent, currentProgress, request->contentLength());
                        if (loggerCallback) {
                            loggerCallback("progress", "\"percent\":" + String(percent));
                        }
                        lastProgressMs = now;
                    }
                }

                if (final) {
                    if (!Update.end(true)) {
                        StreamString err;
                        Update.printError(err);
                        updateError = err.c_str();
                        LOG("FW", "Finalize error: %s", updateError.c_str());
                    } else {
                        LOG("FW", "Update successful (%zu bytes)", currentProgress);
                    }
                }
            });

    LOG("FW", "Firmware routes registered");
}

void FirmwareManager::loop() {
    if (rebootPending && millis() - rebootRequestMs > 2000) {
        LOG("FW", "Rebooting...");
        ESP.restart();
    }
}
