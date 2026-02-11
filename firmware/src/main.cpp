#include <Arduino.h>
#include <WiFi.h>
#include <esp_ota_ops.h>
#include "config.h"
#include "wifi_manager.h"
#include "firmware_manager.h"
#include "web_server.h"
#include "neato_serial.h"
#include "data_logger.h"

// Global objects
AsyncWebServer server(80);
NeatoSerial neatoSerial;
WiFiManager wifiManager;
FirmwareManager firmwareManager(server);
DataLogger dataLogger(neatoSerial);
WebServer webServer(server, neatoSerial, dataLogger);

void setup() {
    Serial.begin(115200);
    delay(1000); // Wait for serial to be ready
    LOG("BOOT", "");
    LOG("BOOT", "========================================");
    LOG("BOOT", "ESP32-C3 Neato starting...");
    LOG("BOOT", "========================================");

    // Setup reset button
    pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);
    delay(100); // Small delay for pin to stabilize

    // Read and log button state for debugging
    int buttonState = digitalRead(RESET_BUTTON_PIN);
    LOG("BOOT", "Button pin %d state: %s", RESET_BUTTON_PIN, buttonState == LOW ? "PRESSED" : "RELEASED");

    // Check if button is held during boot for factory reset
    if (buttonState == LOW) {
        LOG("BOOT", "Reset button detected on boot!");
        LOG("BOOT", "Hold for 5 seconds to reset WiFi credentials...");

        unsigned long pressStart = millis();
        bool resetConfirmed = false;
        int countdown = 5;

        while (digitalRead(RESET_BUTTON_PIN) == LOW) {
            unsigned long elapsed = millis() - pressStart;
            int currentCountdown = 5 - static_cast<int>(elapsed / 1000);

            if (currentCountdown != countdown) {
                countdown = currentCountdown;
                LOG("BOOT", "Resetting in %d seconds...", countdown);
            }

            if (elapsed >= RESET_BUTTON_HOLD_TIME) {
                LOG("BOOT", "RESETTING WiFi credentials!");
                wifiManager.reset();
                resetConfirmed = true;
                break;
            }
            delay(100);
        }

        if (!resetConfirmed) {
            LOG("BOOT", "Reset cancelled - button released too early");
        }
    }

    // Initialize Neato UART
    LOG("BOOT", "Initializing Neato serial...");
    neatoSerial.begin();

    // Initialize WiFi with provisioning
    LOG("BOOT", "Initializing WiFi...");
    wifiManager.begin();

    // Initialize web server and OTA only if WiFi is connected
    if (wifiManager.isConnected()) {
        LOG("BOOT", "Initializing web server...");
        webServer.begin();
        LOG("BOOT", "Initializing firmware updater...");
        firmwareManager.begin();
        LOG("BOOT", "Starting HTTP server...");
        server.begin();

        // Mark firmware as valid — cancels auto-rollback on next reboot
        esp_ota_mark_app_valid_cancel_rollback();
        LOG("BOOT", "Firmware marked valid");
    } else {
        LOG("BOOT", "Skipping web server (no WiFi connection)");
        LOG("BOOT", "Configure WiFi through serial menu");
    }

    // Initialize data logger (SPIFFS, NTP, serial command hook)
    LOG("BOOT", "Initializing data logger...");
    dataLogger.begin();

    // Wire firmware update events to data logger
    firmwareManager.setLogger([](const String& event, const String& payload) { dataLogger.logOta(event, payload); });

    // Wire WiFi events to data logger
    WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
        switch (event) {
            case ARDUINO_EVENT_WIFI_STA_CONNECTED:
                dataLogger.logWifi("connected", "");
                break;
            case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
                dataLogger.logWifi("disconnected", "\"reason\":" + String(info.wifi_sta_disconnected.reason));
                break;
            case ARDUINO_EVENT_WIFI_STA_GOT_IP:
                dataLogger.logWifi("got_ip", "\"ip\":\"" + WiFi.localIP().toString() + "\"");
                break;
            default:
                break;
        }
    });

    LOG("BOOT", "========================================");
    LOG("BOOT", "System initialization complete");
    LOG("BOOT", "========================================");

    // Show WiFi config menu if needed (after all boot messages)
    if (!wifiManager.isConnected()) {
        wifiManager.showMenu();
    } else {
        LOG("BOOT", "");
        LOG("BOOT", "Quick commands: [m]enu, [s]tatus");
    }
}

void loop() {
    // Handle WiFi configuration through serial
    wifiManager.handleSerialInput();

    // Check for button press (runtime reset)
    static unsigned long buttonPressStart = 0;
    static bool buttonWasPressed = false;

    if (digitalRead(RESET_BUTTON_PIN) == LOW) {
        if (!buttonWasPressed) {
            // Button just pressed
            buttonPressStart = millis();
            buttonWasPressed = true;
            LOG("BUTTON", "Button pressed - hold for 5 seconds to reset");
        } else {
            // Button still held
            unsigned long holdTime = millis() - buttonPressStart;
            if (holdTime >= RESET_BUTTON_HOLD_TIME) {
                LOG("BUTTON", "RESETTING WiFi credentials!");
                wifiManager.reset();
            }
        }
    } else {
        if (buttonWasPressed) {
            LOG("BUTTON", "Button released");
            buttonWasPressed = false;
        }
    }

    // Note: Serial commands are now handled by wifiManager.handleSerialInput()

    // Firmware update handling (only if connected)
    if (wifiManager.isConnected()) {
        firmwareManager.loop();

        // Skip other operations during firmware update
        if (firmwareManager.isInProgress()) {
            return;
        }
    }

    // Pump Neato serial command queue
    neatoSerial.loop();

    // Data logger housekeeping (NTP detection, robot time sync)
    dataLogger.loop();
}
