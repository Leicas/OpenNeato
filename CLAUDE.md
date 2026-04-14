# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

OpenNeato — open-source replacement for Neato's discontinued cloud/app. An ESP32 bridge talks to Botvac robots (D3-D7) over UART and serves a local web UI. No cloud, no external dependencies.

Also includes a Home Assistant custom integration (`custom_components/openneato/`) distributed via HACS.

Robot serial protocol reference: `docs/neato-serial-protocol.md`

## Architecture

Three independent components plus an HA integration:

| Component | Path | Language | Toolchain |
|-----------|------|----------|-----------|
| Firmware | `firmware/` (source in `firmware/src/`, libs in `firmware/lib/`) | C++ (Arduino) | PlatformIO (`platformio.ini` at root) |
| Frontend | `frontend/` | TypeScript, Preact | Vite, Biome |
| Flash tool | `flash/` | Go | Go build, GoReleaser |
| HA integration | `custom_components/openneato/` | Python | Home Assistant |

**Build coupling:** `npm run build` in `frontend/` runs lint + typecheck + vite build + `scripts/embed_frontend.js`, which gzips all dist files and generates `firmware/src/web_assets.h` (a C header with PROGMEM byte arrays). The firmware includes this header directly — a single OTA update ships both firmware and UI.

**Firmware architecture:** Non-blocking `loop()` in `main.cpp`. Managers are wired via dependency injection (constructor args). Managers that run unconditionally extend `LoopTask` and self-register via `TaskRegistry::add(this)` — `TaskRegistry::tickAll()` drives them from `loop()`. WiFi-gated managers (WiFiManager, FirmwareManager) are called explicitly in `loop()` with conditional logic.

**Frontend architecture:** Preact SPA with hash-based routing (`components/router.tsx`). Polling via `usePolling` hook. All API calls go through `src/api.ts` (thin fetch wrappers hitting `/api/*` endpoints). Dark theme by default, mobile-first.

**Mock server:** `frontend/mock/server.js` is a Vite plugin providing mock API responses during `npm run dev`. Uses a `SCENARIO` constant to switch between states. Reset `SCENARIO` to `"ok"` before committing.

## Build Commands

### Frontend

```bash
cd frontend
npm ci                  # Install deps (first time / lockfile changed)
npm run dev             # Vite dev server with mock API (localhost:5173)
npm run build           # Lint + typecheck + vite build + generate web_assets.h
npm run check           # Biome lint/format check only
npm run fix             # Auto-fix safe lint issues
npm run fix:unsafe      # Auto-fix including unsafe transforms
```

### Firmware

```bash
pio run -e c3-debug                        # Build (ESP32-C3)
pio run -e c3-debug -t upload              # Upload via USB
pio run -e c3-debug -t upload -t monitor   # Upload + serial monitor
pio run -e c3-release                      # Release build (no serial logging)
python scripts/check_format.py             # clang-format check
python scripts/check_format.py --fix       # Auto-fix formatting
pio check -e c3-debug                      # Static analysis (clang-tidy)
```

Board environments: `c3-*`, `c6-*`, `s3-*`, `esp32-*` (each with `-debug`, `-ota`, `-release`).

### Flash Tool

```bash
cd flash
go build -o openneato-flash .    # Build
golangci-lint run ./...          # Lint
```

### Verification Checklist

- **Firmware changes:** `pio run -e c3-debug` + `python scripts/check_format.py` + `pio check -e c3-debug` (zero defects)
- **Frontend changes:** `npm run check` + `npm run build` (in `frontend/`)
- **Flash tool changes:** `golangci-lint run ./...` + `go build` (in `flash/`)

## CI

GitHub Actions (`ci.yml`) runs on push to main and PRs:
- Firmware: builds + format check + clang-tidy for `c3-debug`, `s3-debug`, `esp32-debug`
- Frontend: `npm run check` + `npm run build`
- Flash tool: golangci-lint + `go build`

## Zero-Dependency Policy

**Firmware:** No external libraries beyond what's in `platformio.ini` (AsyncTCP, ESPAsyncWebServer). No JSON libraries — use the custom `json_fields.h/cpp`. No HTTP client, MQTT, or WebSocket libraries. ESP32-C3 has 320KB RAM and 1600KB per OTA slot.

**Frontend:** Only runtime dependency is Preact. No state management, CSS frameworks, routing, or HTTP wrapper libraries.

## Code Style

### Firmware (C++)
- `snake_case` files, PascalCase classes, camelCase methods, UPPER_SNAKE macros
- 4-space indent, K&R braces, 120-col (enforced by `.clang-format`)
- Arduino `String`, `//` comments only, `#ifndef` include guards
- No exceptions — return-value error handling, early returns

### Frontend (TypeScript)
- 4-space indent, double quotes, semicolons, 120-col (enforced by Biome)
- Named `interface`/`type` only — never inline object type literals

## Key Conventions

- **NVS:** Single `"neato"` namespace, opened once in `main.cpp`, passed by reference to managers. NVS writes are user-triggered only (settings save, WiFi provisioning).
- **Filesystem:** SPIFFS (not LittleFS). Buffer writes in RAM, never write to flash in a loop.
- **Data logging:** Controlled by `logLevel` setting (0=off, 1=info, 2=debug). Default is off. Both info and debug auto-revert to off after timeout. Use typed `DataLogger` helpers (`logRequest`, `logWifi`, `logOta`, etc.), not the private `logEvent`.
- **Serial commands:** All robot communication goes through `NeatoSerial`'s async queue with `AsyncCache` for response coalescing.
- **HACS:** The `brand/` directory inside `custom_components/openneato/` is required by HACS for integration icons — do not delete it.

## Release Process

Manual releases via AI-assisted workflow; see `RELEASE_PROCESS.md`. Version format is `major.minor` (no patch). Prereleases from PRs via `/prerelease` comment.

## Issue/Commit Conventions

Format: `type: brief description` (lowercase, under 60 chars, no period)
Types: `feat:`, `fix:`, `enhance:`, `chore:`, `docs:`, `build:`
