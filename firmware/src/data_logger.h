#ifndef DATA_LOGGER_H
#define DATA_LOGGER_H

#include <Arduino.h>
#include <ESPAsyncWebServer.h>
#include <SPIFFS.h>
#include <functional>
#include <vector>
#include "config.h"

class NeatoSerial;


// Log file metadata for API listing
struct LogFileInfo {
    String name;
    size_t size = 0;
    bool compressed = false;
};

class DataLogger {
public:
    DataLogger(NeatoSerial& neato);

    void begin();
    void loop();

    // -- Public logging methods (called by other modules) --------------------

    void logEvent(const String& type, const String& jsonPayload);
    void logError(const String& source, const String& message);
    void logRequest(WebRequestMethodComposite method, const String& path, int status, unsigned long ms);
    void logWifi(const String& event, const String& jsonPayload);
    void logOta(const String& event, const String& jsonPayload);
    void logNtp(const String& event, const String& jsonPayload);

    // -- Log file management (for API) --------------------------------------

    std::vector<LogFileInfo> listLogs() const;
    bool readLog(const String& filename, std::function<void(File&)> reader) const;
    bool decompressLog(const String& filename, std::function<void(const uint8_t *, size_t)> writer) const;
    bool deleteLog(const String& filename);
    void deleteAllLogs();

    // -- System health (live, for GET /api/system) --------------------------

    String systemHealthJson() const;

    // -- Timezone -----------------------------------------------------------

    String getTimezone() const;
    void setTimezone(const String& tz);

    // -- NTP status ---------------------------------------------------------

    bool isNtpSynced() const { return ntpSynced; }
    void triggerRobotTimeSync();

private:
    NeatoSerial& neato;

    // Time management
    bool ntpSynced = false;
    bool robotTimeFetched = false;
    time_t robotTimeBase = 0; // Epoch from robot clock at boot
    unsigned long robotTimeMillis = 0; // millis() when robot time was read
    unsigned long lastRobotSync = 0;

    // SPIFFS state
    bool spiffsReady = false;

    // Get current epoch (best available source)
    time_t now() const;

    // Log writing
    void writeLine(const String& jsonLine);
    void rotateIfNeeded();
    void enforceSpaceLimit();

    // Boot tasks
    void archiveLeftoverLog();
    void logBootEvent();
    void fetchRobotTime();
    void syncRobotTime();

    // Compression
    bool compressFile(const String& srcPath, const String& dstPath);

    // NeatoSerial logger hook (enhanced with status, queue depth, response size)
    void onCommand(const String& cmd, CommandStatus status, unsigned long ms, const String& raw, int queueDepth,
                   size_t respBytes);

    // Helper: escape a string for JSON
    static String jsonEscape(const String& s);
};

#endif // DATA_LOGGER_H
