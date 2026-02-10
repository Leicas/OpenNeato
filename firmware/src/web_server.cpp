#include "web_server.h"
#include "web_assets.h"
#include "neato_serial.h"

// Default serializer for structs with toFields()
template<typename T>
static std::function<String(const T&)> jsonFields() {
    return [](const T& data) { return fieldsToJson(data.toFields()); };
}

WebServer::WebServer(AsyncWebServer& server, NeatoSerial& neato) : server(server), neato(neato) {}

void WebServer::sendGzipAsset(AsyncWebServerRequest *request, const uint8_t *data, size_t len,
                              const char *contentType) {
    AsyncWebServerResponse *response = request->beginResponse(200, contentType, data, len);
    response->addHeader("Content-Encoding", "gzip");
    request->send(response);
}

void WebServer::sendError(AsyncWebServerRequest *request, int code, const String& msg) {
    request->send(code, "application/json", R"({"error":")" + msg + R"("})");
}

void WebServer::sendOk(AsyncWebServerRequest *request) {
    request->send(200, "application/json", R"({"ok":true})");
}

void WebServer::registerActionRoute(const char *path, bool (NeatoSerial::*method)(std::function<void(bool)>)) {
    server.on(path, HTTP_POST, [this, method](AsyncWebServerRequest *request) {
        if (!(neato.*method)([request](bool ok) {
                if (!ok) {
                    sendError(request, 504, "timeout");
                    return;
                }
                sendOk(request);
            })) {
            sendError(request, 503, "queue full");
        }
    });
}

void WebServer::begin() {
    // Serve SPA index
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
        sendGzipAsset(request, INDEX_HTML_GZ, INDEX_HTML_GZ_LEN, INDEX_HTML_CONTENT_TYPE);
    });

    // Serve app.js
    server.on("/app.js", HTTP_GET, [](AsyncWebServerRequest *request) {
        sendGzipAsset(request, APP_JS_GZ, APP_JS_GZ_LEN, APP_JS_CONTENT_TYPE);
    });

    registerApiRoutes();

    LOG("WEB", "Frontend and API routes registered");
}

void WebServer::registerApiRoutes() {
    // -- Sensor query endpoints ----------------------------------------------

    registerSensorRoute<VersionData>("/api/version", &NeatoSerial::getVersion, jsonFields<VersionData>());
    registerSensorRoute<ChargerData>("/api/charger", &NeatoSerial::getCharger, jsonFields<ChargerData>());
    registerSensorRoute<AnalogSensorData>("/api/sensors/analog", &NeatoSerial::getAnalogSensors,
                                          jsonFields<AnalogSensorData>());
    registerSensorRoute<DigitalSensorData>("/api/sensors/digital", &NeatoSerial::getDigitalSensors,
                                           jsonFields<DigitalSensorData>());
    registerSensorRoute<MotorData>("/api/motors", &NeatoSerial::getMotors, jsonFields<MotorData>());
    registerSensorRoute<RobotState>("/api/state", &NeatoSerial::getState, jsonFields<RobotState>());
    registerSensorRoute<ErrorData>("/api/error", &NeatoSerial::getErr, jsonFields<ErrorData>());
    registerSensorRoute<AccelData>("/api/accel", &NeatoSerial::getAccel, jsonFields<AccelData>());
    registerSensorRoute<ButtonData>("/api/buttons", &NeatoSerial::getButtons, jsonFields<ButtonData>());
    registerSensorRoute<LdsScanData>(
            "/api/lidar", &NeatoSerial::getLdsScan,
            std::function<String(const LdsScanData&)>([](const LdsScanData& data) { return data.toJson(); }));

    // -- Action endpoints ----------------------------------------------------

    registerActionRoute("/api/clean/house", &NeatoSerial::cleanHouse);
    registerActionRoute("/api/clean/spot", &NeatoSerial::cleanSpot);
    registerActionRoute("/api/clean/stop", &NeatoSerial::cleanStop);

    // Sound requires a parameter, handled inline
    server.on("/api/sound", HTTP_POST, [this](AsyncWebServerRequest *request) {
        int id = request->getParam("id")->value().toInt();
        if (!neato.playSound(static_cast<SoundId>(id), [request](bool ok) {
                if (!ok) {
                    sendError(request, 504, "timeout");
                    return;
                }
                sendOk(request);
            })) {
            sendError(request, 503, "queue full");
        }
    });

    LOG("WEB", "API routes registered");
}
