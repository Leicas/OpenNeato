#ifndef WEB_SERVER_H
#define WEB_SERVER_H

#include <ESPAsyncWebServer.h>
#include <functional>
#include "config.h"
#include "neato_commands.h"

class NeatoSerial;

class WebServer {
public:
    WebServer(AsyncWebServer& server, NeatoSerial& neato);
    void begin();

private:
    AsyncWebServer& server;
    NeatoSerial& neato;

    void registerApiRoutes();
    static void sendGzipAsset(AsyncWebServerRequest *request, const uint8_t *data, size_t len, const char *contentType);
    static void sendError(AsyncWebServerRequest *request, int code, const String& msg);
    static void sendOk(AsyncWebServerRequest *request);

    // Register a GET endpoint that queries a typed Neato response and returns JSON
    template<typename T>
    void registerSensorRoute(const char *path, bool (NeatoSerial::*method)(std::function<void(bool, const T&)>),
                             std::function<String(const T&)> serialize);

    // Register a POST endpoint that sends an action command and returns {"ok":true}
    void registerActionRoute(const char *path, bool (NeatoSerial::*method)(std::function<void(bool)>));
};

// -- Template implementation (must be in header) -----------------------------

template<typename T>
void WebServer::registerSensorRoute(const char *path, bool (NeatoSerial::*method)(std::function<void(bool, const T&)>),
                                    std::function<String(const T&)> serialize) {
    server.on(path, HTTP_GET, [this, method, serialize](AsyncWebServerRequest *request) {
        if (!(neato.*method)([request, serialize](bool ok, const T& data) {
                if (!ok) {
                    sendError(request, 504, "timeout");
                    return;
                }
                request->send(200, "application/json", serialize(data));
            })) {
            sendError(request, 503, "queue full");
        }
    });
}

#endif // WEB_SERVER_H
