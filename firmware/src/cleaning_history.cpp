#include "cleaning_history.h"
#include "neato_serial.h"
#include "system_manager.h"
#include <SPIFFS.h>
#include <cmath>

CleaningHistory::CleaningHistory(NeatoSerial& neato, DataLogger& logger, SystemManager& sysMgr) :
    LoopTask(HISTORY_INTERVAL_MS), neato(neato), dataLogger(logger), systemManager(sysMgr) {
    TaskRegistry::add(this);
}

void CleaningHistory::tick() {
    // Run incremental compression when a session just finished
    if (compressing) {
        if (compressStep()) {
            // Compression done — remove raw source
            compressSrc.close();
            compressDst.close();
            SPIFFS.remove(compressSrcPath);
            LOG("HIST", "Compression done: %s", compressDstPath.c_str());
            compressing = false;
            setInterval(HISTORY_INTERVAL_MS);
        }
        return;
    }

    if (fetchPending)
        return;

    if (collecting) {
        collectSnapshot();
    } else {
        checkState();
        enforceLimits();
    }
}

// -- State watching (idle mode) ----------------------------------------------

void CleaningHistory::checkState() {
    fetchPending = true;
    neato.getState([this](bool ok, const RobotState& state) {
        fetchPending = false;
        if (!ok)
            return;

        bool wasCleaning = isCleaningState(prevUiState);
        bool nowCleaning = isCleaningState(state.uiState);

        if (!wasCleaning && nowCleaning) {
            startCollection(state.uiState);
        }

        prevUiState = state.uiState;
    });
}

bool CleaningHistory::isCleaningState(const String& uiState) {
    return uiState.indexOf("HOUSECLEANINGRUNNING") >= 0 || uiState.indexOf("SPOTCLEANINGRUNNING") >= 0 ||
           uiState.indexOf("MANUALCLEANING") >= 0;
}

bool CleaningHistory::isDockingState(const String& uiState) {
    return uiState.indexOf("DOCKING") >= 0;
}

String CleaningHistory::cleanModeFromState(const String& uiState) {
    if (uiState.indexOf("HOUSECLEANING") >= 0)
        return "house";
    if (uiState.indexOf("SPOTCLEANING") >= 0)
        return "spot";
    if (uiState.indexOf("MANUALCLEANING") >= 0)
        return "manual";
    return "unknown";
}

void CleaningHistory::resetSession() {
    snapshotCount = 0;
    rechargeCount = 0;
    totalDistance = 0.0f;
    totalRotation = 0.0f;
    maxDistFromOrigin = 0.0f;
    errorsDuringClean = 0;
    prevHadError = false;
    hasPrevPose = false;
    prevX = 0.0f;
    prevY = 0.0f;
    prevTheta = 0.0f;
    originX = 0.0f;
    originY = 0.0f;
    visitedCells.clear();
    cleanMode = "";
    sessionStartTime = 0;
    batteryStart = -1;
    recharging = false;
}

void CleaningHistory::startCollection(const String& uiState) {
    collecting = true;
    resetSession();

    cleanMode = cleanModeFromState(uiState);
    sessionStartTime = systemManager.now();

    // Create session file: /history/<epoch>.jsonl
    activeFilePath = String(HISTORY_DIR) + "/" + String(static_cast<long>(sessionStartTime)) + ".jsonl";
    activeFile = SPIFFS.open(activeFilePath, FILE_WRITE);
    if (!activeFile) {
        LOG("HIST", "Failed to create session file: %s", activeFilePath.c_str());
        collecting = false;
        return;
    }

    // Fetch battery level for session metadata, then write header
    neato.getCharger([this](bool ok, const ChargerData& charger) {
        if (ok) {
            batteryStart = charger.fuelPercent;
        }
        writeSessionHeader();
    });

    LOG("HIST", "Collection started (mode: %s, file: %s)", cleanMode.c_str(), activeFilePath.c_str());
    dataLogger.logGenericEvent("history_start", {{"mode", cleanMode, FIELD_STRING}});
}

void CleaningHistory::stopCollection() {
    // Fetch final battery level for summary
    neato.getCharger([this](bool ok, const ChargerData& charger) {
        int batteryEnd = ok ? charger.fuelPercent : -1;

        writeSessionSummary();

        // Close the raw file
        if (activeFile) {
            activeFile.close();
        }

        collecting = false;
        recharging = false;

        float areaCovered = static_cast<float>(visitedCells.size()) * HISTORY_AREA_CELL_M * HISTORY_AREA_CELL_M;

        LOG("HIST", "Collection stopped (%u snapshots, %.1fm², %d recharges)", snapshotCount, areaCovered,
            rechargeCount);
        dataLogger.logGenericEvent("history_stop", {{"snapshots", String(snapshotCount), FIELD_INT},
                                                    {"area_m2", String(areaCovered, 1), FIELD_FLOAT},
                                                    {"recharges", String(rechargeCount), FIELD_INT},
                                                    {"battery_end", String(batteryEnd), FIELD_INT}});

        // Start non-blocking compression: raw .jsonl -> .jsonl.hs
        compressSrcPath = activeFilePath;
        compressDstPath = activeFilePath + ".hs";
        compressSrc = SPIFFS.open(compressSrcPath, FILE_READ);
        compressDst = SPIFFS.open(compressDstPath, FILE_WRITE);
        if (compressSrc && compressDst) {
            heatshrink_encoder_reset(&compressEncoder);
            compressInputDone = false;
            compressing = true;
            setInterval(HISTORY_COMPRESS_INTERVAL_MS);
            LOG("HIST", "Starting compression: %s -> %s", compressSrcPath.c_str(), compressDstPath.c_str());
        } else {
            // Compression failed — keep raw file
            if (compressSrc)
                compressSrc.close();
            if (compressDst)
                compressDst.close();
            LOG("HIST", "Compression setup failed, keeping raw file");
        }
    });
}

// -- Incremental compression (called from tick) ------------------------------

bool CleaningHistory::compressStep() {
    static const size_t CHUNK_SIZE = 512;
    uint8_t inBuf[CHUNK_SIZE];
    uint8_t outBuf[CHUNK_SIZE];

    if (!compressInputDone) {
        int bytesRead = compressSrc.read(inBuf, CHUNK_SIZE);
        if (bytesRead <= 0) {
            compressInputDone = true;
        } else {
            size_t offset = 0;
            while (offset < static_cast<size_t>(bytesRead)) {
                size_t sunk = 0;
                HSE_sink_res sres =
                        heatshrink_encoder_sink(&compressEncoder, inBuf + offset, bytesRead - offset, &sunk);
                if (sres < 0) {
                    LOG("HIST", "Heatshrink sink error");
                    compressSrc.close();
                    compressDst.close();
                    SPIFFS.remove(compressDstPath);
                    compressing = false;
                    return true;
                }
                offset += sunk;

                size_t outSz = 0;
                HSE_poll_res pres;
                do {
                    pres = heatshrink_encoder_poll(&compressEncoder, outBuf, CHUNK_SIZE, &outSz);
                    if (pres < 0) {
                        LOG("HIST", "Heatshrink poll error");
                        compressSrc.close();
                        compressDst.close();
                        SPIFFS.remove(compressDstPath);
                        compressing = false;
                        return true;
                    }
                    if (outSz > 0) {
                        compressDst.write(outBuf, outSz);
                    }
                } while (pres == HSER_POLL_MORE);
            }
        }
        return false;
    }

    // Input exhausted — finish encoding
    HSE_finish_res fres = heatshrink_encoder_finish(&compressEncoder);
    if (fres < 0) {
        LOG("HIST", "Heatshrink finish error");
        compressSrc.close();
        compressDst.close();
        SPIFFS.remove(compressDstPath);
        compressing = false;
        return true;
    }

    size_t outSz = 0;
    HSE_poll_res pres;
    do {
        pres = heatshrink_encoder_poll(&compressEncoder, outBuf, CHUNK_SIZE, &outSz);
        if (pres < 0) {
            LOG("HIST", "Heatshrink poll error during finish");
            compressSrc.close();
            compressDst.close();
            SPIFFS.remove(compressDstPath);
            compressing = false;
            return true;
        }
        if (outSz > 0) {
            compressDst.write(outBuf, outSz);
        }
    } while (pres == HSER_POLL_MORE);

    return (fres == HSER_FINISH_DONE);
}

// -- Session header/summary --------------------------------------------------

void CleaningHistory::writeSessionHeader() {
    String line = "{\"type\":\"session\",\"mode\":\"" + cleanMode + "\"";
    if (sessionStartTime > 0) {
        line += ",\"time\":" + String(static_cast<long>(sessionStartTime));
    }
    if (batteryStart >= 0) {
        line += ",\"battery\":" + String(batteryStart);
    }
    line += "}";
    writeLine(line);
}

void CleaningHistory::writeSessionSummary() {
    time_t endTime = systemManager.now();
    long duration =
            (sessionStartTime > 0 && endTime > sessionStartTime) ? static_cast<long>(endTime - sessionStartTime) : 0;
    float areaCovered = static_cast<float>(visitedCells.size()) * HISTORY_AREA_CELL_M * HISTORY_AREA_CELL_M;
    String line = "{\"type\":\"summary\"";
    if (endTime > 0) {
        line += ",\"time\":" + String(static_cast<long>(endTime));
    }
    line += ",\"duration\":" + String(duration);
    line += ",\"mode\":\"" + cleanMode + "\"";
    line += ",\"recharges\":" + String(rechargeCount);
    line += ",\"snapshots\":" + String(static_cast<int>(snapshotCount));
    line += ",\"distanceTraveled\":" + String(totalDistance, 2);
    line += ",\"maxDistFromOrigin\":" + String(maxDistFromOrigin, 2);
    line += ",\"totalRotation\":" + String(totalRotation, 1);
    line += ",\"areaCovered\":" + String(areaCovered, 2);
    line += ",\"errorsDuringClean\":" + String(errorsDuringClean);
    if (batteryStart >= 0) {
        line += ",\"battery\":" + String(batteryStart);
    }
    line += "}";
    writeLine(line);
}

// -- Direct file writing -----------------------------------------------------

void CleaningHistory::writeLine(const String& line) {
    if (!activeFile)
        return;
    activeFile.println(line);
    activeFile.flush();
}

// -- Snapshot collection (active mode) ---------------------------------------

static bool parsePose(const String& raw, float& x, float& y, float& theta, float& time) {
    int xPos = raw.indexOf("X=");
    int yPos = raw.indexOf("Y=");
    int tPos = raw.indexOf("Theta=");
    int tmPos = raw.indexOf("Time=");
    if (xPos < 0 || yPos < 0 || tPos < 0 || tmPos < 0)
        return false;

    x = raw.substring(xPos + 2).toFloat();
    y = raw.substring(yPos + 2).toFloat();
    theta = raw.substring(tPos + 6).toFloat();
    time = raw.substring(tmPos + 5).toFloat();
    return true;
}

void CleaningHistory::collectSnapshot() {
    fetchPending = true;

    neato.getState([this](bool stateOk, const RobotState& state) {
        if (stateOk) {
            prevUiState = state.uiState;

            bool isDocking = isDockingState(state.uiState);
            bool isCleaning = isCleaningState(state.uiState);
            bool isChargingMidClean = state.robotState.indexOf("Charging_Cleaning") >= 0;

            if (isDocking && isChargingMidClean) {
                if (!recharging) {
                    recharging = true;
                    rechargeCount++;
                    LOG("HIST", "Recharge #%d detected — pausing collection", rechargeCount);
                    dataLogger.logGenericEvent("history_recharge_start", {{"count", String(rechargeCount), FIELD_INT}});

                    if (hasPrevPose) {
                        writeLine("{\"type\":\"recharge\",\"x\":" + String(prevX, 3) + ",\"y\":" + String(prevY, 3) +
                                  "}");
                    }
                }
                fetchPending = false;
                return;
            }

            if (recharging && isCleaning) {
                recharging = false;
                LOG("HIST", "Recharge done — resuming collection");
                dataLogger.logGenericEvent("history_recharge_end", {});
            }

            if (!isCleaning && !isDocking) {
                fetchPending = false;
                stopCollection();
                return;
            }
        }

        neato.getErr([this](bool errOk, const ErrorData& err) {
            if (errOk) {
                if (err.hasError && !prevHadError) {
                    errorsDuringClean++;
                }
                prevHadError = err.hasError;
            }

            if (recharging) {
                fetchPending = false;
                return;
            }

            neato.getRobotPos(true, [this](bool posOk, const RobotPosData& pos) {
                fetchPending = false;
                if (!posOk)
                    return;

                float x, y, theta, time;
                if (!parsePose(pos.raw, x, y, theta, time)) {
                    LOG("HIST", "Failed to parse pose");
                    return;
                }

                writeSnapshot(x, y, theta, time);
            });
        });
    });
}

void CleaningHistory::updateAccumulators(float x, float y, float theta) {
    if (!hasPrevPose) {
        originX = x;
        originY = y;
        prevX = x;
        prevY = y;
        prevTheta = theta;
        hasPrevPose = true;
    } else {
        float dx = x - prevX;
        float dy = y - prevY;
        totalDistance += sqrtf(dx * dx + dy * dy);

        float dTheta = theta - prevTheta;
        if (dTheta > 180.0f)
            dTheta -= 360.0f;
        if (dTheta < -180.0f)
            dTheta += 360.0f;
        totalRotation += fabsf(dTheta);

        prevX = x;
        prevY = y;
        prevTheta = theta;
    }

    float dox = x - originX;
    float doy = y - originY;
    float distFromOrigin = sqrtf(dox * dox + doy * doy);
    if (distFromOrigin > maxDistFromOrigin) {
        maxDistFromOrigin = distFromOrigin;
    }

    int ix = static_cast<int>(floorf(x / HISTORY_AREA_CELL_M));
    int iy = static_cast<int>(floorf(y / HISTORY_AREA_CELL_M));
    uint32_t cellKey = (static_cast<uint32_t>(ix & 0xFFFF) << 16) | static_cast<uint32_t>(iy & 0xFFFF);
    visitedCells.insert(cellKey);
}

void CleaningHistory::writeSnapshot(float x, float y, float theta, float time) {
    String line = "{\"x\":" + String(x, 3) + ",\"y\":" + String(y, 3) + ",\"t\":" + String(theta, 1) +
                  ",\"ts\":" + String(time, 1) + "}";

    updateAccumulators(x, y, theta);
    writeLine(line);
    snapshotCount++;

    if (snapshotCount % 10 == 0) {
        LOG("HIST", "Snapshot #%u (%.1fm traveled, pose: %.2f,%.2f,%.0f)", snapshotCount, totalDistance, x, y, theta);
    }
}

// -- Storage enforcement (mirrors DataLogger::enforceLimits) -----------------

void CleaningHistory::enforceLimits() {
    // Count session files, sum directory size, and find the oldest in one pass
    int fileCount = 0;
    size_t histDirBytes = 0;
    String oldest;
    File root = SPIFFS.open(HISTORY_DIR);
    if (!root || !root.isDirectory())
        return;

    File entry = root.openNextFile();
    while (entry) {
        String name = String(entry.name());
        histDirBytes += entry.size();
        if (name.endsWith(".jsonl") || name.endsWith(".jsonl.hs")) {
            fileCount++;
            if (oldest.isEmpty() || name < oldest) {
                oldest = name;
            }
        }
        entry = root.openNextFile();
    }

    if (oldest.isEmpty())
        return;

    // History budget: total SPIFFS cap minus non-history data, floored at minimum reserve
    size_t total = SPIFFS.totalBytes();
    size_t globalCap = (total * HISTORY_MAX_SPIFFS_PERCENT) / 100;
    size_t nonHistBytes = SPIFFS.usedBytes() > histDirBytes ? SPIFFS.usedBytes() - histDirBytes : 0;
    size_t available = globalCap > nonHistBytes ? globalCap - nonHistBytes : 0;
    size_t minReserved = (total * HISTORY_MIN_SPIFFS_PERCENT) / 100;
    size_t histBudget = available > minReserved ? available : minReserved;

    if (histDirBytes > histBudget || fileCount > HISTORY_MAX_FILES) {
        String fullPath = String(HISTORY_DIR) + "/" + oldest;
        LOG("HIST", "Limit: deleting %s (files=%d, histBytes=%u/%u)", fullPath.c_str(), fileCount, histDirBytes,
            histBudget);
        SPIFFS.remove(fullPath);
    }
}

// -- File management (for API) -----------------------------------------------

void CleaningHistory::readFirstLastLines(const String& path, bool compressed, String& firstLine, String& lastLine) {
    firstLine = "";
    lastLine = "";

    if (compressed) {
        // Decompress fully into a String (history files are tiny, 2-5KB)
        File f = SPIFFS.open(path, FILE_READ);
        if (!f)
            return;
        CompressedLogReader reader(std::move(f));
        String content;
        uint8_t buf[256];
        size_t n;
        while ((n = reader.read(buf, sizeof(buf))) > 0) {
            content += String(reinterpret_cast<const char *>(buf), n);
        }
        // Split by newline, take first and last non-empty lines
        int first = content.indexOf('\n');
        if (first < 0) {
            firstLine = content;
            firstLine.trim();
            return;
        }
        firstLine = content.substring(0, first);
        firstLine.trim();
        // Find last non-empty line by scanning backward
        int end = content.length() - 1;
        while (end >= 0 && (content[end] == '\n' || content[end] == '\r'))
            end--;
        if (end < 0)
            return;
        int lastNl = content.lastIndexOf('\n', end);
        if (lastNl < 0)
            return; // Only one line
        lastLine = content.substring(lastNl + 1, end + 1);
        lastLine.trim();
    } else {
        // Plain .jsonl — read first line directly, seek backward for last line
        File f = SPIFFS.open(path, FILE_READ);
        if (!f)
            return;
        // Read first line
        firstLine = f.readStringUntil('\n');
        firstLine.trim();
        // Seek backward from end to find last line
        size_t fileSize = f.size();
        if (fileSize < 2) {
            f.close();
            return;
        }
        int pos = static_cast<int>(fileSize) - 2; // Skip trailing newline
        while (pos >= 0) {
            f.seek(pos);
            char c = static_cast<char>(f.read());
            if (c == '\n') {
                break;
            }
            pos--;
        }
        // pos is at the newline before last line, or -1 if only one line
        if (pos < 0) {
            f.close();
            return; // Only one line
        }
        f.seek(pos + 1);
        lastLine = f.readStringUntil('\n');
        lastLine.trim();
        f.close();
    }
}

std::vector<HistorySessionInfo> CleaningHistory::listSessions() {
    std::vector<HistorySessionInfo> result;

    File root = SPIFFS.open(HISTORY_DIR);
    if (!root || !root.isDirectory())
        return result;

    File entry = root.openNextFile();
    while (entry) {
        String fullPath = String(entry.path());
        String name = fullPath;
        int lastSlash = name.lastIndexOf('/');
        if (lastSlash >= 0)
            name = name.substring(lastSlash + 1);

        if (name.endsWith(".jsonl") || name.endsWith(".jsonl.hs")) {
            // During compression both raw .jsonl and partial .jsonl.hs exist —
            // skip both until compression finishes and the raw source is deleted
            if (compressing && (fullPath == compressSrcPath || fullPath == compressDstPath)) {
                entry = root.openNextFile();
                continue;
            }

            HistorySessionInfo info;
            info.name = name;
            info.size = entry.size();
            info.compressed = name.endsWith(".hs");

            if (collecting && fullPath == activeFilePath) {
                // Active recording session — build from in-memory state
                info.recording = true;
                String sessionJson = "{\"type\":\"session\",\"mode\":\"" + cleanMode + "\"";
                if (sessionStartTime > 0)
                    sessionJson += ",\"time\":" + String(static_cast<long>(sessionStartTime));
                if (batteryStart >= 0)
                    sessionJson += ",\"battery\":" + String(batteryStart);
                sessionJson += "}";
                info.session = sessionJson;
                // No summary for active session
            } else {
                // Completed file on disk — read first/last lines
                String firstLine, lastLine;
                readFirstLastLines(fullPath, info.compressed, firstLine, lastLine);
                info.session = firstLine;
                // Only set summary if last line is actually a summary
                if (lastLine.indexOf("\"type\":\"summary\"") >= 0) {
                    info.summary = lastLine;
                }
            }

            result.push_back(info);
        }
        entry = root.openNextFile();
    }

    return result;
}

std::shared_ptr<LogReader> CleaningHistory::readSession(const String& filename) {
    String path = String(HISTORY_DIR) + "/" + filename;

    // Refuse to serve files involved in compression (partial .hs is corrupt)
    if (compressing && (path == compressSrcPath || path == compressDstPath))
        return nullptr;

    if (!SPIFFS.exists(path))
        return nullptr;

    File f = SPIFFS.open(path, FILE_READ);
    if (!f)
        return nullptr;

    if (filename.endsWith(".hs")) {
        return std::make_shared<CompressedLogReader>(std::move(f));
    }
    return std::make_shared<PlainLogReader>(std::move(f));
}

bool CleaningHistory::deleteSession(const String& filename) {
    String path = String(HISTORY_DIR) + "/" + filename;
    if (!SPIFFS.exists(path))
        return false;
    return SPIFFS.remove(path);
}

void CleaningHistory::deleteAllSessions() {
    File root = SPIFFS.open(HISTORY_DIR);
    if (!root || !root.isDirectory())
        return;

    std::vector<String> paths;
    File entry = root.openNextFile();
    while (entry) {
        paths.push_back(String(entry.path()));
        entry = root.openNextFile();
    }

    for (const auto& p: paths) {
        SPIFFS.remove(p);
    }
    LOG("HIST", "Deleted %u session files", paths.size());
}
