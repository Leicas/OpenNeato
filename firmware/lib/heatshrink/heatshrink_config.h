#ifndef HEATSHRINK_CONFIG_H
#define HEATSHRINK_CONFIG_H

// Static allocation only — no malloc/free on ESP32-C3
#define HEATSHRINK_DYNAMIC_ALLOC 0

// Use 32-bit optimized variant (faster on RISC-V/Xtensa 32-bit architectures)
#define HEATSHRINK_32BIT 1

// Static configuration for encoder/decoder
#define HEATSHRINK_STATIC_INPUT_BUFFER_SIZE 256
#define HEATSHRINK_STATIC_WINDOW_BITS 10 // 1 KB window
#define HEATSHRINK_STATIC_LOOKAHEAD_BITS 5 // 32-byte lookahead

// No debug logging
#define HEATSHRINK_DEBUGGING_LOGS 0

// No index (saves RAM, required for 32-bit search functions)
#define HEATSHRINK_USE_INDEX 0

#endif // HEATSHRINK_CONFIG_H
