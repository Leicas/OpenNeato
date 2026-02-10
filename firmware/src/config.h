#ifndef CONFIG_H
#define CONFIG_H

// WiFi Configuration
#define HOSTNAME "Neato"

// Pin Configuration (ESP32-C3 Boot button is GPIO9)
#define RESET_BUTTON_PIN 9

// Neato UART pins (ESP32-C3 hardware UART on free GPIOs)
#define NEATO_TX_PIN 4
#define NEATO_RX_PIN 5
#define NEATO_BAUD_RATE 115200

// Neato command queue timing (milliseconds)
#define NEATO_CMD_TIMEOUT_MS 3000
#define NEATO_LDS_TIMEOUT_MS 8000
#define NEATO_INTER_CMD_DELAY_MS 50
#define NEATO_QUEUE_MAX_SIZE 16
#define NEATO_RESPONSE_TERMINATOR 0x1A // Ctrl-Z

// Timing intervals (milliseconds)
#define WIFI_RECONNECT_INTERVAL 5000
#define RESET_BUTTON_HOLD_TIME 5000 // Hold for 5 seconds to reset

// Logging
#define ENABLE_LOGGING

#ifdef ENABLE_LOGGING
#define LOG(tag, fmt, ...) Serial.printf("[%s] " fmt "\n", tag, ##__VA_ARGS__)
#else
#define LOG(tag, fmt, ...)
#endif

#endif // CONFIG_H
