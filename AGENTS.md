# AGENTS.md — project-neato

Guidelines for AI agents working in this repository.

## Project Overview

ESP32-C3 bridge for Neato Botvac robot vacuums (D3, D5, D7 series). The ESP32-C3
communicates with the Botvac over UART (TX/RX pins) using Neato's serial command
protocol, and exposes a web UI over WiFi so users can monitor and control the robot
from a browser.

Built with PlatformIO + Arduino framework on espressif32 platform.

**Standalone system** — no Home Assistant, no cloud, no external dependencies.
The ESP32-C3 serves a SPA (single-page application) that communicates with the
firmware through REST API and WebSocket. Everything runs on the device itself.

## Project Vision

### Phase 1: Foundation (complete)
- WiFi provisioning via serial menu
- OTA firmware updates via web UI
- Basic infrastructure (async web server, NVS config storage)

### Phase 2: API layer and sensor integration (current)
- UART serial bridge to Neato Botvac
- Serial command queue (no overlapping commands, inter-command delay)
- REST API endpoints for commands and one-off reads
- Already have ESPAsyncWebServer, just register route handlers and return JSON
- WebSocket for real-time data push (sensor updates, state changes)
- Sensor reading endpoints: GetVersion, GetCharger, GetAnalogSensors,
  GetDigitalSensors, GetMotors, GetState, GetErr
- Polling loop for periodic sensor data (GetErr + GetState every 2s,
  GetCharger every 2min)
- Testable without UI — curl, Postman, or WebSocket client

### Phase 3: On-device analytics and diagnostics
- Comprehensive data collection without serial debug (robot is mobile)
- Enables embedding the board inside the robot for all subsequent development
- Structured log entries: system events, sensor readings, errors, commands
- Data categories:
  - System health: free heap, uptime, restart reason, WiFi RSSI
  - Serial comms: commands sent, responses, timeouts, parse errors
  - Sensor snapshots: periodic GetAnalogSensors, GetDigitalSensors, GetMotors
  - LIDAR scans: full GetLDSScan captures for offline map debugging
  - Cleaning sessions: start/stop times, duration, type, errors
  - OTA events: check/download/flash attempts, versions, outcomes
- Compressed storage using ESP32 ROM miniz (deflate), rotating log files
- Store in SPIFFS (256KB partition) with automatic rotation (drop oldest)
- API endpoint to browse, filter, and download logs from the web UI
- Critical for LIDAR/mapping development: replay scan data without live robot

### Phase 4: Web UI
- SPA shell embedded in firmware binary (PROGMEM) — done (stub)
- Build pipeline: compile frontend assets, gzip, generate C header, compile
  into firmware — done
- Basic layout and navigation structure
- Live sensor dashboard (battery, motors, bumpers, cliff sensors, etc.)
- Responsive design for mobile and desktop browsers
- Builds on stable, tested API — renders real data from day one

### Phase 5: Manual control
- Drive the robot manually from the web UI (forward, back, rotate)
- Start/stop/pause cleaning cycles
- SetMotor, SetLED commands
- TestMode enable/disable for direct motor control

### Phase 6: Safe OTA with auto-rollback
- Check GitHub Releases for newer firmware versions
- Web UI notification when an update is available
- One-click download and install from the browser
- Strict checksum validation before flashing (SHA-256 or MD5)
- ESP32 app rollback: new firmware must call `esp_ota_mark_app_valid_cancel_rollback()`
  after reaching a "successfully booted" checkpoint, otherwise the bootloader
  automatically rolls back to the previous partition on next reboot
- Dual OTA partition layout already in place (app0/app1, 1856KB each)

### Phase 7: LIDAR and mapping
- Read LIDAR distance data via GetLDSScan
- Render real-time 2D maps in the web UI
- Store and display historical maps
- Explore new areas by combining manual drive with live LIDAR feedback

### Neato Serial Protocol
- **Baud rate**: 115200
- **Line ending**: LF (`\n`)
- **Command format**: ASCII text commands, one per line
- **Response format**: Multi-line ASCII, terminated by Ctrl-Z (0x1A)
- **Response line endings**: `\r\n` (CRLF) between lines
- **Case sensitivity**: NOT case-sensitive, supports partial matching
- **Partial command matching**: Only type enough letters to make command unique
- **Command syntax**: Flexible format with `Cmd [Flag] [ParamName ParamValue]` pairs
  - Flags are boolean (presence = true)
  - ParamName/Value pairs can be in any order
  - ParamNames support partial matching
  - Can omit ParamNames if values are in correct sequence
- **Key commands**: `GetVersion`, `GetCharger`, `GetAnalogSensors`,
  `GetDigitalSensors`, `GetMotors`, `GetLDSScan`, `SetMotor`, `TestMode`,
  `SetLED`, `Clean`, `PlaySound`
- **TestMode**: Must be enabled (`TestMode On`) before direct motor commands
- **Response parsing**: CSV format with header row; row/column order not guaranteed
  across firmware versions; always parse by matching labels, not position

### Robot Debug Port Pinout (D3/D5/D7)
```
RX | 3.3V | TX | GND
```
Connect: Robot RX -> ESP TX, Robot TX -> ESP RX, Robot 3.3V -> ESP VCC,
Robot GND -> ESP GND. The robot provides 3.3V to power the ESP.

### Command Reference

**No TestMode required:**
- `Help [Cmd]` — List all commands or help for specific command
  - Without argument: prints list of all commands
  - With command name: prints help for that specific command
- `Clean [House|Spot|Stop]` — Cleaning control
  - `House` (Optional) — Equivalent to pressing 'Start' button once. Starts house cleaning (default)
  - `Spot` (Optional) — Starts a spot clean
  - `Stop` — Stop Cleaning
- `GetVersion` — Software/hardware version info
- `GetCharger` — Battery and charging data
- `GetAnalogSensors [raw] [stats]` — A2D analog sensor readings
- `GetDigitalSensors` — Digital sensor states
- `GetMotors [Brush] [Vacuum] [LeftWheel] [RightWheel] [Laser] [Charger]` — Motor diagnostic data
- `GetAccel` — Accelerometer readings
- `GetButtons` — UI button states
- `GetErr [Clear]` — Error and alert messages
- `GetLDSScan` — One full LIDAR scan (360 lines: AngleDeg,DistMM,Intensity,ErrorCode)
- `GetSchedule [Day N]` — Get cleaning schedule (24h format)
- `GetTime` — Current scheduler time
- `GetCalInfo` — Calibration info from system control block
- `GetLifeStatLog` — All life stat logs
- `GetSysLog` — System log data (unimplemented in XV-11)
- `GetWarranty` — Warranty validation codes (hex values)
- `PlaySound [SoundID N] [Stop]` — Play sound (0-20, see manual for IDs)
- `RestoreDefaults` — Restore user settings to default
- `SetDistanceCal [DropMinimum|DropMiddle|DropMaximum] [WallMinimum|WallMiddle|WallMaximum]` — Set distance sensor calibration values for min and max distances
  - DropMinimum: Take minimum distance drop sensor readings (mutually exclusive of DropMiddle and DropMax)
  - DropMiddle: Take middle distance drop sensor readings (mutually exclusive of DropMinimum and DropMax)
  - DropMaximum: Take maximum distance drop sensor readings (mutually exclusive of DropMinimum and DropMiddle)
  - WallMinimum: Take minimum distance wall sensor readings (mutually exclusive of WallMiddle and WallMax)
  - WallMiddle: Take middle distance wall sensor readings (mutually exclusive of WallMinimum and WallMax)
  - WallMaximum: Take maximum distance wall sensor readings (mutually exclusive of WallMinimum and WallMiddle)
  - Returns: `Label,Value RDropCalA2DMin,-1 RDropCalA2DMid,-1 RDropCalA2DMax,-1 LDropCalA2DMin,-1 LDropCalA2DMid,-1 LDropCalA2DMax,-1 WallCalA2DMin,-1 WallCalA2DMid,-1 WallCalA2DMax,-1`
- `SetFuelGauge [Percent N]` — Set fuel gauge level (0-100)
- `SetSchedule [Day N] [Hour N] [Min N] [House|None] [ON|OFF]` — Modify cleaning schedule
  - Day: 0=Sun, 6=Sat (required)
  - Hour: 0-23 (required)
  - Min: 0-59 (required)
  - House: Schedule to clean whole house (default, mutually exclusive with None)
  - None: Remove scheduled cleaning for specified day (time is ignored)
  - ON/OFF: Enable/disable scheduled cleanings (mutually exclusive)
- `SetTime [Day N] [Hour N] [Min N] [Sec N]` — Set scheduler clock
  - Day: 0=Sunday, 1=Monday, ... (required)
  - Hour: 0-23 (required)
  - Min: 0-59 (required)
  - Sec: 0-59 (optional, defaults to 0)
- `SetWallFollower [Enable|Disable]` — Enable/disable wall follower
- `TestMode On/Off` — Enable/disable test mode
- `DiagTest [TestsOff|DrivePath|DriveForever|MoveAndBump|DropTest|...]` — Execute test modes
- `Upload [dump|code|sound|LDS] [xmodem] [size N] [noburn] [readflash] [reboot]` — Upload new firmware
  - code/sound/LDS: Upload file type (mutually exclusive)
  - xmodem: Use xmodem protocol
  - size: Data size to upload
  - noburn: Test option - do NOT burn flash after upload
  - readflash: Test option - read flash at current region
  - reboot: Reset robot after upload

**TestMode required:**
- `SetMotor [LWheelDist <mm>] [RWheelDist <mm>] [Speed <mm/s>] [Accel <mm/s>]` — Drive wheels
  - Distance in millimeters (pos = forward, neg = backward)
  - Speed required only for wheel movements
  - Accel defaults to Speed value if not specified
- `SetMotor [RPM <rpm>] [Brush] [VacuumOn|VacuumOff] [VacuumSpeed <percent>]` — Control motors
  - RPM not used for wheels, applied to all other motors
  - Brush motor forward (mutually exclusive with wheels and vacuum)
  - VacuumSpeed in percent (1-100)
- `SetMotor [RWheelDisable|LWheelDisable|BrushDisable] [RWheelEnable|LWheelEnable|BrushEnable]` — Enable/disable motors
- `SetLED [BacklightOn|BacklightOff] [ButtonAmber|ButtonGreen|LEDRed|LEDGreen|ButtonAmberDim|ButtonGreenDim|ButtonOff]` — Control LEDs
  - BacklightOn/Off: LCD Backlight (mutually exclusive)
  - ButtonAmber/Green/Red/Green/Dim: Start Button (mutually exclusive)
  - ButtonOff: Start Button Off
- `SetLDSRotation On/Off` — Start/stop LIDAR rotation (mutually exclusive)
- `SetLCD [BGWhite|BGBlack] [HLine <row>] [VLine <col>] [HBars|VBars] [FGWhite|FGBlack] [Contrast <0-63>]` — Set LCD display
  - BGWhite/BGBlack: Fill background
  - HLine/VLine: Draw horizontal/vertical line at specified position
  - HBars/VBars: Draw alternating lines across screen
  - FGWhite/FGBlack: Foreground (line) color
  - Contrast: 0..63 value into NAND
- `SetSystemMode [Shutdown|Hibernate|Standby]` — Power control (mutually exclusive)

**Hidden commands (undocumented, found via reverse engineering):**
- `GetRobotPos Raw/Smooth` — Odometry/localized position
- `GetDatabase All/Factory/Robot/Runtime/Statistics/System/CleanStats`
- `GetActiveServices` — Running services
- `SetUIError setalert/clearalert/clearall/list` — UI error state machine
- `NewBattery` — Tell robot new battery installed

### Response Formats

**GetCharger** — CSV: `Label,Value`
```
Charger Variable Name, Value Label,Value FuelPercent,100 BatteryOverTemp,0
ChargingActive,0 ChargingEnabled,0 ConfidentOnFuel,0 OnReservedFuel,0 EmptyFuel,0
BatteryFailure,0 ExtPwrPresent,0 ThermistorPresent[0],0 ThermistorPresent[1],0
BattTempCAvg[0],103 BattTempCAvg[1],103 VBattV,0.21 VExtV,0.00 Charger_mAH,0
MaxPWM,65536 PWM,-858993460
```
Simplified format (newer firmware):
```
FuelPercent,53              # Battery %
BatteryOverTemp,0           # 0/1
ChargingActive,0            # 0/1
ChargingEnabled,1           # 0/1
ConfidentOnFuel,0           # 0/1
OnReservedFuel,0            # 0/1
EmptyFuel,0                 # 0/1
BatteryFailure,0            # 0/1
ExtPwrPresent,0             # 0/1 (on dock)
ThermistorPresent,0         # 0/1
BattTempCAvg,22             # Celsius
VBattV,14.58                # Volts
VExtV,0.00                  # External volts
Charger_mAH,0               # mAh charged
Discharge_mAH,238           # mAh discharged
```

**GetAnalogSensors** — CSV: `SensorName,Unit,Value` (trailing comma)
```
BatteryVoltage,mV,14585,
BatteryCurrent,mA,-238,     # Negative = discharging
BatteryTemperature,mC,22800, # Milli-Celsius (÷1000 for °C)
ExternalVoltage,mV,0,
AccelerometerX,mG,16,       # Milli-G
AccelerometerY,mG,2,
AccelerometerZ,mG,963,
VacuumCurrent,mA,0,
SideBrushCurrent,mA,0,
MagSensorLeft,VAL,0,        # Magnetic boundary strip
MagSensorRight,VAL,0,
WallSensor,mm,255,          # Distance to wall
DropSensorLeft,mm,19,       # Cliff sensor
DropSensorRight,mm,19,
```

**GetDigitalSensors** — CSV: `Name,Value` (all 0/1)
```
SNSR_DC_JACK_IS_IN          # Charging dock connected
SNSR_DUSTBIN_IS_IN          # Dustbin present
SNSR_LEFT_WHEEL_EXTENDED    # Left wheel lifted
SNSR_RIGHT_WHEEL_EXTENDED   # Right wheel lifted
LSIDEBIT, LFRONTBIT, LLDSBIT   # Left bumper sections
RSIDEBIT, RFRONTBIT, RLDSBIT   # Right bumper sections
```

**GetMotors** — CSV: `Parameter,Value`
```
Brush_RPM, Brush_mA, Vacuum_RPM, Vacuum_mA, SideBrush_mA
LeftWheel_RPM, LeftWheel_Load%, LeftWheel_PositionInMM, LeftWheel_Speed
RightWheel_RPM, RightWheel_Load%, RightWheel_PositionInMM, RightWheel_Speed
ROTATION_SPEED              # Decimal rotation speed
```
Wheel PositionInMM = odometry from origin, can be negative.

**GetLDSScan** — CSV: `AngleDeg,DistMM,Intensity,ErrorCode`
- 360 output lines of LDS Scan Angle, Distance code in MM, normalized spot intensity, and error code
- AngleDeg: 0-359 (integer)
- DistMM: millimeters (0 = no reading)
- Intensity: Normalized spot intensity
- ErrorCode: 0 = valid, non-zero = error
- Followed by 2 status variable pairs
- Example: `AngleInDegrees,DistInMM,Intensity,ErrorCodeHEX 0,221,1400,0 1,223,1396,0 ... 359,220,1421,0 ROTATION_SPEED (in Hz, Floating Point),5.00`

**GetState** — Two lines:
```
Current UI State is: UIMGR_STATE_STANDBY
Current Robot State is: ST_C_Standby
```

**GetVersion** — CSV: `Component,Major,Minor,Build`
```
Component,Major,Minor,Build
Product Model,XV-11,,
Serial Number,AAAnnnnnAA,0000000,D
Software,6,1,13328
LDS Software,V1.0.0,,
LDS Serial,XXX-YYY,,
MainBoard Vendor ID,1,,
MainBoard Serial Number,99,,
MainBoard Version,0,8,
Chassis Version,-1,,
UIPanel Version,-1,,
```
More recent versions may include:
```
ModelID,0,XV11, ConfigID,1,, Serial Number,AAAnnnnnAA,0000000,D
Software,2,1,15499 BatteryType,1,NIMH_12CELL, BlowerType,1,BLOWER_ORIG,
BrushSpeed,0,, BrushMotorType,1,BRUSH_MOTOR_ORIG, SideBrushType,1,SIDE_BRUSH_NONE,
WheelPodType,1,WHEEL_POD_ORIG, DropSensorType,1,DROP_SENSOR_ORIG,
MagSensorType,1,MAG_SENSOR_ORIG, WallSensorType,1,WALL_SENSOR_ORIG,
Locale,1,LOCALE_USA, LDS Software,V1.0.0,, LDS Serial,XXX-YYY,, LDS CPU,F2802x/cd00,,
MainBoard Vendor ID,1,, MainBoard Serial Number,99,, MainBoard Version,15,0,
ChassisRev,-1,, UIPanelRev,-1,,
```

**GetWarranty** — Three hex values, convert with `strtoul(hex, nullptr, 16)`

**GetErr [Clear]** — Returns error message if present, otherwise no message
- Error code 200 (`UI_ALERT_INVALID`) = no error (normal state)
- `Clear` flag dismisses the reported error
- **Complete error code list:**
  - 1: WDT, 2: SSEG LED, 3: BTN LED, 4: BACK LED, 5: FLASH
  - 10: BattNominal, 11: BattOverVolt, 12: BattUnderVolt, 13: BattOverTemp
  - 14: BattShutdownTemp, 15: BattUnderCurrent, 16: BattTimeout, 17: BattTempPeak
  - 18: BattFastCapacity, 19: BattMACapacity, 20: BattOnReserve, 21: BattEmpty
  - 22: BattMismatch, 23: BattLithiumAdapterFailure
  - 207: I had to reset my system. Please press START to clean
  - 217: Please unplug my Power Cable when you want me to clean
  - 218: Please unplug my USB Cable when you want me to clean
  - 219: Please set schedule to ON first
  - 220: Please set my clock first
  - 222: Please put my Dirt Bin back in
  - 223: Please check my Dirt Bin and Filter. Empty them as needed
  - 224: My Brush is overheated. Please wait while I cool down
  - 225: My Battery is overheated. Please wait while I cool down
  - 226: I am unable to navigate. Please clear my path
  - 227: Please return me to my base
  - 228: My Bumper is stuck. Please free it
  - 229: Please put me down on the floor
  - 230: I can't charge. Try moving the base station to a new location
  - 231: My Left Wheel is stuck. Please free it from debris
  - 232: My Right Wheel is stuck. Please free it from debris
  - 233: I have an RPS error. Please visit web support
  - 234: My Brush is stuck. Please free it from debris
  - 235: My Brush is overloaded. Please free it from debris
  - 236: My Vacuum is stuck. Please visit web support
  - 237: Please Check my filter and Dirt Bin
  - 238: My Battery has a critical error. Please visit web support
  - 239: My Brush has a critical error. Please visit web support
  - 240: My Schedule is now OFF
  - 241: I can't shut down while I am connected to power
  - 243: A Software update is available. Please visit web support
  - 244: My SCB was corrupted. I reinitialized it. Please visit web support
  - 245: Please Dust me off so that I can see

### Polling Intervals (from reference project)
- `GetErr` + `GetState`: every 2 seconds
- `GetCharger`: every 2 minutes
- Inter-command delay: 50ms between sequential commands
- TestMode -> SetSystemMode delay: 100ms

### UI States (UIMGR_STATE_*)
Key states: `POWERUP`, `STANDBY`, `IDLE`, `HOUSECLEANINGRUNNING`,
`HOUSECLEANINGPAUSED`, `SPOTCLEANINGRUNNING`, `SPOTCLEANINGPAUSED`,
`DOCKINGRUNNING`, `DOCKINGPAUSED`, `TESTMODE`, `MANUALDRIVING`

### Pause Workaround
```
Clean Stop
SetUIError setalert UI_ALERT_OLD_ERROR   (50ms delay)
SetUIError clearalert UI_ALERT_OLD_ERROR
```
Forces UI state machine to properly recognize paused state.

### Supported Robots
D3, D4, D5, D6, D7 confirmed. D70-D85 likely compatible.
D8/D9/D10 NOT supported (different board, password-locked serial).

### Known Limitations
- LIDAR scan responses are large; line-by-line reading recommended
- Serial commands must be queued (no overlapping)
- In TestMode, GetState always returns `UIMGR_STATE_TESTMODE`
- No known serial command to return to dock
- Commands cannot have leading spaces
- Communication parameters (Baud, start/stop bits, parity) are unimportant for USB
  (they apply only to real COM ports, not USB CDC)

### Additional Commands from XV-11 Manual

**DiagTest flags:**
- `TestsOff` — Stop diagnostic test and clear all diagnostic test modes
- `DrivePath [DrivePathDist <mm>]` — Robot travels straight by commanded distance as path
- `DriveForever [DriveForeverLeftDist <mm>] [DriveForeverRightDist <mm>] [DriveForeverSpeed <mm/s>]` — Robot drives continuously; ratio of left/right determines turn radius
- `MoveAndBump` — Executes canned series of motions, but will react to bumps
- `DropTest [Speed <mm/s>] [BrushSpeed <rpm>] [AutoCycle|OneShot]` — Drive forward until drop detected
  - `AutoCycle` — Robot drives backwards then forward until drop detected, repeating until test over
  - `OneShot` — Only executes test once
- `BrushOn` — Turn on brush during test (may conflict with motor commands)
- `VacuumOn` — Turn on vacuum during test (may conflict with motor commands)
- `LDSOn` — Turn on LDS during test (may conflict with motor commands)
- `AllMotorsOn` — Turn on brush, vacuum, and LDS during test (may conflict with motor commands)
- `DisablePickupDetect` — Ignore pickup (wheel suspension). By default, pickup detect is enabled and stops the test

**PlaySound IDs (0-20):**
- 0: Waking Up
- 1: Starting Cleaning
- 2: Cleaning Completed
- 3: Attention Needed
- 4: Backing up into base station
- 5: Base Station Docking Completed
- 6: Test Sound 1
- 7: Test Sound 2
- 8: Test Sound 3
- 9: Test Sound 4
- 10: Test Sound 5
- 11: Exploring
- 12: ShutDown
- 13: Picked Up
- 14: Going to sleep
- 15: Returning Home
- 16: User Canceled Cleaning
- 17: User Terminated Cleaning
- 18: Slipped Off Base While Charging
- 19: Alert
- 20: Thank You

**GetAccel** — Returns: `Label,Value PitchInDegrees, RollInDegrees, XInG, YInG, ZInG, SumInG`

**GetButtons** — Returns: `Button Name,Pressed` for BTN_SOFT_KEY, BTN_SCROLL_UP, BTN_START, BTN_BACK, BTN_SCROLL_DOWN
Example: `BTN_SOFT_KEY,0 BTN_SCROLL_UP,0 BTN_START,0 BTN_BACK,0 BTN_SCROLL_DOWN,0`

**GetCalInfo** — Returns calibration values:
```
Parameter,Value LDSOffset,0 XAccel,0 YAccel,0 ZAccel,0 RTCOffset,0 LCDContrast,43
RDropMin,-1 RDropMid,-1 RDropMax,-1 LDropMin,-1 LDropMid,-1 LDropMax,-1
WallMin,-1 WallMid,-1 WallMax,-1
```

**GetSchedule** — Returns schedule for all days or specific day:
```
Schedule is Enabled Sun 00:00 - None - Mon 00:00 - None - Tue 00:00 R
Wed 00:00 R Thu 00:00 R Fri 00:00 H Sat 00:00 H
```
(R = spot clean, H = house clean, None = no cleaning)

**GetTime** — Returns: `DayOfWeek HourOf24:Min:Sec` Example: `Sunday 13:57:09`

**GetLifeStatLog** — Returns multiple LifeStat logs from oldest to newest, non-zero entries only:
Format: `runID,statID,count,Min,Max,Sum,SumV*2`
Includes stats for A2D sensors, drop sensors, clean types, errors (brush overtemp, battery overtemp,
wheel stuck, LDS jammed, brush stuck, vacuum stuck, etc.), LDS errors (dot issues, calibration,
laser errors), alerts, and usage counters.

**GetSysLog** — Returns: `(Unimplemented) Sys Log Entries: Run, Stat, Min, Max, Sum, Count, Time(ms)`

**GetAnalogSensors raw** — Returns raw millivolt values:
```
SensorName,SignalVoltageInmV WallSensorInMM,0 BatteryVoltageInmV,2574 LeftDropInMM,3296
RightDropInMM,3296 RightMagSensor,0 LeftMagSensor,0 XTemp0InC,1759 XTemp1InC,1759
VacuumCurrentInmA,322 ChargeVoltInmV,0 NotConnected1,0 BatteryTemp1InC,1759
NotConnected2,0 CurrentInmA,992 NotConnected3,0 BatteryTemp0InC,1759
```

**GetAnalogSensors stats** — Returns statistics (Mean, Max, Min, Cnt, Dev):
```
SensorName,Mean,Max,Min,Cnt,Dev WallSensorInMM,0,0,0,50,0
BatteryVoltageInmV,2574,2574,2574,50,0 LeftDropInMM,3296,3296,3296,50,0
(stats for all sensors with mean, max, min, count, deviation)
```

**GetDigitalSensors** — Full sensor list:
```
SNSR_DC_JACK_CONNECT,0 SNSR_DUSTBIN_IS_IN,1 SNSR_LEFT_WHEEL_EXTENDED,0
SNSR_RIGHT_WHEEL_EXTENDED,0 LSIDEBIT,0 LFRONTBIT,0 RSIDEBIT,0 RFRONTBIT,0
```

**GetMotors** — Full motor diagnostic output (if no flags, all motors reported):
```
Parameter,Value Brush_MaxPWM,65536 Brush_PWM,-858993460 Brush_mVolts,1310
Brush_Encoder,0 Brush_RPM,-858993460 Vacuum_MaxPWM,65536 Vacuum_PWM,-858993460
Vacuum_CurrentInMA,52428 Vacuum_Encoder,0 Vacuum_RPM,52428
LeftWheel_MaxPWM,65536 LeftWheel_PWM,-858993460 LeftWheel_mVolts,1310
LeftWheel_Encoder,0 LeftWheel_PositionInMM,0 LeftWheel_RPM,-13108
RightWheel_MaxPWM,65536 RightWheel_PWM,-858993460 RightWheel_mVolts,1310
RightWheel_Encoder,0 RightWheel_PositionInMM,0 RightWheel_RPM,-13108
Laser_MaxPWM,65536 Laser_PWM,-858993460 Laser_mVolts,1310 Laser_Encoder,0
Laser_RPM,52428 Charger_MaxPWM,65536 Charger_PWM,-858993460 Charger_mAH,52428
```

## Frontend Stack

- **Framework**: Preact (~4 KB gzipped) — React-compatible API, minimal footprint for
  constrained firmware budget
- **Build tool**: Vite — tree-shaking, minification, gzip-ready output
- **Language**: TypeScript with JSX (TSX)
- **Pipeline**: `npm run build` → minified bundle → gzip → generate C header with
  embedded byte arrays → firmware compiles with assets baked in
- **Serving**: ESPAsyncWebServer serves embedded assets from PROGMEM with gzip
  Content-Encoding headers
- **OTA strategy**: Frontend assets are bundled into the firmware binary, so a single
  firmware OTA update covers both code and UI — no version mismatch possible, no
  separate SPIFFS upload needed
- **Size budget**: 1856 KB per OTA slot is shared between firmware and embedded assets;
  keeping the frontend small is still important (Preact helps here)
- **SPIFFS freed**: With frontend in firmware, the full SPIFFS partition is available
  for analytics logs and diagnostics (Phase 6)
## Architecture

Two top-level directories: `firmware/` for ESP32 code, `frontend/` for the web UI.
`platformio.ini` stays at the root so CLion/PlatformIO can load the project directly.

```
platformio.ini             # PIO config (src_dir = firmware/src)
firmware/
  src/
    config.h               # Global defines, macros, LOG macro, pin/timing constants
    main.cpp               # setup()/loop() entry point, global objects
    serial_menu.h/cpp      # Generic interactive serial menu system
    wifi_manager.h/cpp     # WiFi config, credential storage, network scanning
    ota_handler.h/cpp      # ElegantOTA wrapper over async web server
    web_server.h/cpp       # Serves embedded frontend assets from PROGMEM
    web_assets.h           # Auto-generated — gzipped frontend as byte arrays
    neato_commands.h/cpp   # Command enum, response structs, CSV parsers,
                           #   Field/toFields() system, generic JSON + menu serializers
    neato_serial.h/cpp     # UART command queue, state machine, typed convenience
                           #   methods (getCharger, cleanHouse, etc.)
  partition.csv            # Custom partition table (dual OTA slots, 1856KB each)
frontend/
  package.json             # Preact + Vite build config
  tsconfig.json            # TypeScript configuration
  vite.config.ts           # Vite build settings (deterministic output filenames)
  index.html               # SPA entry point
  src/
    main.tsx               # Preact render entry
    app.tsx                # Root component
  scripts/
    embed_frontend.js      # Gzips frontend dist, generates firmware/src/web_assets.h
```

## Build Commands

```bash
# Build (Debug env — serial upload)
pio run -e Debug

# Build and upload via USB serial
pio run -e Debug -t upload

# Upload and open serial monitor
pio run -e Debug -t upload -t monitor

# Serial monitor only
pio run -e Debug -t monitor

# OTA upload (device must be on network as neato.home)
pio run -e OTA -t upload

# Clean build artifacts
pio run -e Debug --target clean

# Static analysis (clang-tidy)
pio check -e Debug

# Format code (clang-format)
clang-format -i firmware/src/*.cpp firmware/src/*.h
```

**Monitor baud rate**: 115200

Verify changes by building successfully with `pio run -e Debug` and running
`pio check -e Debug` with zero defects. Code style is enforced by
`.clang-format` at the project root.

## Dependencies (all pinned)

- `ayushsharma82/ElegantOTA @ 3.1.7`
- `ESP32Async/AsyncTCP @ 3.4.10`
- `ESP32Async/ESPAsyncWebServer @ 3.9.6`
- Built-in: `Preferences @ 2.0.0`, `WiFi @ 2.0.0`

## Code Style

### File Naming
- `snake_case` for all filenames: `wifi_manager.cpp`, `serial_menu.h`

### Header Guards
- Traditional `#ifndef`/`#define`/`#endif` (not `#pragma once`)
- Pattern: `FILENAME_H` in `UPPER_SNAKE_CASE`
- Closing comment: `#endif // WIFI_MANAGER_H`

### Include Order
1. Framework/library headers with angle brackets: `<Arduino.h>`, `<WiFi.h>`, `<functional>`
2. Project headers with quotes: `"config.h"`, `"serial_menu.h"`
3. Source files include only their own header — no re-inclusion of transitive deps

### Naming Conventions
| Element              | Convention          | Examples                                    |
|----------------------|---------------------|---------------------------------------------|
| Classes/Structs      | `PascalCase`        | `WiFiMgr`, `OTAHandler`, `SerialMenu`, `MenuItem` |
| Enum types           | `PascalCase`        | `InputMode`                                 |
| Enum values          | `UPPER_SNAKE_CASE`  | `MENU_SELECTION`, `TEXT_INPUT`               |
| Methods              | `camelCase`         | `begin()`, `handleInput()`, `isConnected()`  |
| Member variables     | `camelCase` (no prefix) | `inConfigMode`, `selectedSSID`, `inputBuffer` |
| Local variables      | `camelCase`         | `buttonState`, `pressStart`, `attempts`      |
| Global objects       | `camelCase`         | `wifiManager`, `otaHandler`, `server`        |
| Macros/defines       | `UPPER_SNAKE_CASE`  | `HOSTNAME`, `RESET_BUTTON_PIN`, `LOG`        |

### Formatting
- **Indentation**: 4 spaces (no tabs)
- **Braces**: K&R style (opening brace on same line)
- **`const` correctness**: Applied to parameters (`const String&`), methods (`isActive() const`), and locals where appropriate
- **Reference/pointer attachment**: Attach to type: `const String &ssid`, `AsyncWebServer &server`
- **Inline trivial getters** in headers: `bool isActive() const { return active; }`
- **Non-trivial methods**: Declared in header, defined in `.cpp`

### Types
- Use Arduino `String` throughout (never `std::string`)
- Use `std::function` and `std::vector` from STL where needed (C++11)
- Use `static_cast<>` for conversions (not C-style casts)
- Use `unsigned long` for `millis()` timestamps
- Use `bool` for flags, `int` for counters/indexes, `size_t` for collection iteration
- Default-initialize member variables in the header: `bool active = false;`

### Comments
- `//` single-line comments only (no Doxygen or `/** */` blocks)
- Brief section labels: `// Menu actions`, `// Input state`, `// Global objects`
- Inline explanations where non-obvious: `delay(1000); // Wait for serial`

## Error Handling

- **No exceptions** — standard Arduino constraint
- **Return-value based**: Functions return `bool` for success/failure
- **Early returns**: Check preconditions and return immediately
- **Retry with limit**: `while (condition && attempts < 20) { delay(500); attempts++; }`
- **Bounds checking**: Validate indexes before array/vector access
- **Critical failure**: Call `ESP.restart()` after credential reset or OTA completion

## Logging

Defined in `config.h` as a compile-time conditional macro:

```cpp
#ifdef ENABLE_LOGGING
#define LOG(tag, fmt, ...) Serial.printf("[%s] " fmt "\n", tag, ##__VA_ARGS__)
#else
#define LOG(tag, fmt, ...)
#endif
```

- Tags are short UPPER_CASE strings: `"BOOT"`, `"WIFI"`, `"OTA"`, `"BUTTON"`
- Uses printf-style format specifiers: `%s`, `%d`, `%u`
- All user-facing serial output must go through `SerialMenu` helper methods
  (`printStatus`, `printError`, `printSuccess`, `printSection`, `printSeparator`,
  `printKeyValue`), not direct `Serial.print`/`Serial.println` calls

## Serial Menu Pattern

`SerialMenu` is the sole owner of serial display output. WiFi/OTA modules
must use SerialMenu methods for all user-facing output. The only acceptable
direct `Serial` calls outside of `serial_menu.cpp` are:
- `Serial.available()` / `Serial.read()` for input entry points
- `LOG()` macro for debug logging

## Class Design Patterns

- **Dependency injection**: Pass dependencies via constructor (`OTAHandler(AsyncWebServer&)`)
- **Composition over inheritance**: Classes own their collaborators as members
- **Callbacks via lambdas**: `std::function` with `[this]` capture for menu actions
- **State machine**: `InputMode` enum drives serial input behavior in `SerialMenu`
- **No inheritance or virtual methods** in this codebase

## Hardware Notes

- **Board**: ESP32-C3-DevKitM-1 (RISC-V single core, 160MHz, 320KB RAM, 4MB flash)
- **USB**: Native USB CDC (not UART bridge) — `ARDUINO_USB_MODE=1`, `ARDUINO_USB_CDC_ON_BOOT=1`
- **Reset button**: GPIO9 (BOOT button), active LOW with internal pull-up, hold 5s to reset credentials
- **Flash layout**: Dual OTA slots (1856KB each), 256KB SPIFFS, 20KB NVS
- **WiFi credentials**: Stored in NVS via `Preferences` library under namespace `"wifi"`
