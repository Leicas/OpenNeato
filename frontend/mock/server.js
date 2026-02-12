// Mock API server for Neato web UI development
// Mimics all firmware REST endpoints with realistic stateful responses
// Runs as a Vite plugin — hooks into Vite's dev server middleware

// --- Helpers ---

const jsonResponse = (res, data, status = 200) => {
    const body = JSON.stringify(data);
    res.writeHead(status, {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
    });
    res.end(body);
};

const sendOk = (res) => jsonResponse(res, { ok: true });
const sendError = (res, msg, status = 500) =>
    jsonResponse(res, { error: msg }, status);

const readBody = (req) =>
    new Promise((resolve) => {
        let body = "";
        req.on("data", (chunk) => (body += chunk));
        req.on("end", () => resolve(body));
    });

const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
const randf = (min, max, decimals = 2) =>
    parseFloat((Math.random() * (max - min) + min).toFixed(decimals));

// --- Robot state ---

const state = {
    // Battery / charger
    fuelPercent: 85,
    vBattV: 14.58,
    chargingActive: false,
    extPwrPresent: false,

    // Cleaning
    cleaning: false,
    spotCleaning: false,
    uiState: "UIMGR_STATE_IDLE",
    robotState: "ST_C_Idle",

    // Error
    hasError: false,
    errorCode: 200,
    errorMessage: "",

    // Test mode
    testMode: false,

    // Timezone
    tz: "UTC0",

    // Wheel odometry (accumulates when cleaning)
    leftWheelPos: 0,
    rightWheelPos: 0,
};

// Boot time for uptime calculation
const bootTime = Date.now();

// --- Error code lookup ---

const ERROR_MESSAGES = {
    207: "I had to reset my system. Please press START to clean",
    222: "Please put my Dirt Bin back in",
    224: "My Brush is overheated. Please wait while I cool down",
    226: "I am unable to navigate. Please clear my path",
    228: "My Bumper is stuck. Please free it",
    229: "Please put me down on the floor",
    231: "My Left Wheel is stuck. Please free it from debris",
    232: "My Right Wheel is stuck. Please free it from debris",
    234: "My Brush is stuck. Please free it from debris",
    236: "My Vacuum is stuck. Please visit web support",
    238: "My Battery has a critical error. Please visit web support",
    245: "Please Dust me off so that I can see",
};

// --- State simulation timer ---

setInterval(() => {
    // Battery drain / charge
    if (state.extPwrPresent && state.chargingActive) {
        state.fuelPercent = Math.min(100, state.fuelPercent + 0.1);
        state.vBattV = 12.0 + (state.fuelPercent / 100) * 4.6;
    } else if (state.cleaning || state.spotCleaning) {
        state.fuelPercent = Math.max(0, state.fuelPercent - 0.05);
        state.vBattV = 12.0 + (state.fuelPercent / 100) * 4.6;
    } else {
        // Idle drain — very slow
        state.fuelPercent = Math.max(0, state.fuelPercent - 0.002);
        state.vBattV = 12.0 + (state.fuelPercent / 100) * 4.6;
    }

    // Wheel position accumulates when cleaning
    if (state.cleaning || state.spotCleaning) {
        state.leftWheelPos += rand(80, 120);
        state.rightWheelPos += rand(80, 120);
    }
}, 2000);

// --- LIDAR synthetic room generator ---

const generateLidarScan = () => {
    const points = [];
    for (let angle = 0; angle < 360; angle++) {
        const rad = (angle * Math.PI) / 180;

        // Simple rectangular room ~4m x 3m, robot near center
        const cos = Math.cos(rad);
        const sin = Math.sin(rad);

        // Distance to room walls from center
        let dist;
        if (Math.abs(cos) > Math.abs(sin)) {
            dist = Math.abs(2000 / cos); // 2m to left/right walls
        } else {
            dist = Math.abs(1500 / sin); // 1.5m to front/back walls
        }

        // Cap at reasonable max and add noise
        dist = Math.min(dist, 5000);
        dist = Math.round(dist + rand(-30, 30));

        // Some angles get errors (simulating LIDAR artifacts)
        const hasError = Math.random() < 0.05;

        points.push({
            angle,
            dist: hasError ? 0 : Math.max(0, dist),
            intensity: hasError ? 0 : rand(1200, 1600),
            error: hasError ? rand(1, 3) : 0,
        });
    }

    const validPoints = points.filter((p) => p.error === 0).length;
    return { rotationSpeed: randf(4.8, 5.2), validPoints, points };
};

// --- Mock log files ---

const mockLogs = [
    { name: "current.jsonl", size: 8192, compressed: false },
    { name: "1700000000.jsonl.hs", size: 4096, compressed: true },
    { name: "1699990000.jsonl.hs", size: 3584, compressed: true },
];

const mockLogContent = [
    '{"ts":1700000100,"type":"boot","msg":"startup","reason":"power_on"}',
    '{"ts":1700000101,"type":"wifi","msg":"connected","rssi":-52}',
    '{"ts":1700000102,"type":"ntp","msg":"synced","source":"pool.ntp.org"}',
    '{"ts":1700000200,"type":"command","cmd":"GetCharger","status":"ok","ms":85,"q":0,"bytes":312}',
    '{"ts":1700000202,"type":"command","cmd":"GetState","status":"ok","ms":42,"q":0,"bytes":95}',
    '{"ts":1700000210,"type":"request","method":"GET","path":"/api/charger","status":200,"ms":92}',
].join("\n");

// --- Derive UI/robot state from current state ---

const deriveStates = () => {
    if (state.testMode) {
        state.uiState = "UIMGR_STATE_TESTMODE";
        state.robotState = "ST_C_TestMode";
    } else if (state.cleaning) {
        state.uiState = "UIMGR_STATE_HOUSECLEANINGRUNNING";
        state.robotState = "ST_C_HouseCleaning";
    } else if (state.spotCleaning) {
        state.uiState = "UIMGR_STATE_SPOTCLEANINGRUNNING";
        state.robotState = "ST_C_SpotCleaning";
    } else {
        state.uiState = "UIMGR_STATE_IDLE";
        state.robotState = "ST_C_Idle";
    }
};

// --- Route handlers ---

const routes = {
    // Sensor routes
    "GET /api/version": (req, res) => {
        jsonResponse(res, {
            modelName: "BotVacD5",
            serialNumber: "OPS01234AA,0000001,D",
            softwareVersion: "4.5.3-142",
            ldsVersion: "V2.6.15295",
            ldsSerial: "KSH-V5F4",
            mainBoardVersion: "15.0",
        });
    },

    "GET /api/charger": (req, res) => {
        const fuel = Math.round(state.fuelPercent);
        const dischargeMAH = Math.round(
            ((100 - state.fuelPercent) / 100) * 2800
        );
        jsonResponse(res, {
            fuelPercent: fuel,
            batteryOverTemp: false,
            chargingActive: state.chargingActive,
            chargingEnabled: true,
            confidOnFuel: fuel > 20,
            onReservedFuel: fuel < 10,
            emptyFuel: fuel === 0,
            batteryFailure: false,
            extPwrPresent: state.extPwrPresent,
            vBattV: parseFloat(state.vBattV.toFixed(2)),
            vExtV: state.extPwrPresent ? 22.3 : 0.0,
            chargerMAH: state.chargingActive ? rand(100, 2000) : 0,
            dischargeMAH,
        });
    },

    "GET /api/sensors/analog": (req, res) => {
        const battMV = Math.round(state.vBattV * 1000);
        const cleaning = state.cleaning || state.spotCleaning;
        jsonResponse(res, {
            batteryVoltage: battMV,
            batteryCurrent: cleaning ? rand(-800, -400) : rand(-250, -100),
            batteryTemp: rand(21000, 25000),
            externalVoltage: state.extPwrPresent ? 22300 : 0,
            accelX: rand(-20, 20),
            accelY: rand(-20, 20),
            accelZ: rand(950, 970),
            vacuumCurrent: cleaning ? rand(300, 600) : 0,
            sideBrushCurrent: cleaning ? rand(100, 300) : 0,
            magSensorLeft: 0,
            magSensorRight: 0,
            wallSensor: cleaning ? rand(20, 400) : rand(200, 300),
            dropSensorLeft: rand(15, 25),
            dropSensorRight: rand(15, 25),
        });
    },

    "GET /api/sensors/digital": (req, res) => {
        jsonResponse(res, {
            dcJackIn: state.extPwrPresent,
            dustbinIn: true,
            leftWheelExtended: false,
            rightWheelExtended: false,
            lSideBit: false,
            lFrontBit: false,
            lLdsBit: false,
            rSideBit: false,
            rFrontBit: false,
            rLdsBit: false,
        });
    },

    "GET /api/motors": (req, res) => {
        const cleaning = state.cleaning || state.spotCleaning;
        jsonResponse(res, {
            brushRPM: cleaning ? rand(1100, 1300) : 0,
            brushMA: cleaning ? rand(200, 400) : 0,
            vacuumRPM: cleaning ? rand(2200, 2600) : 0,
            vacuumMA: cleaning ? rand(400, 700) : 0,
            leftWheelRPM: cleaning ? rand(60, 120) : 0,
            leftWheelLoad: cleaning ? rand(10, 40) : 0,
            leftWheelPositionMM: state.leftWheelPos,
            leftWheelSpeed: cleaning ? rand(150, 300) : 0,
            rightWheelRPM: cleaning ? rand(60, 120) : 0,
            rightWheelLoad: cleaning ? rand(10, 40) : 0,
            rightWheelPositionMM: state.rightWheelPos,
            rightWheelSpeed: cleaning ? rand(150, 300) : 0,
            sideBrushMA: cleaning ? rand(50, 200) : 0,
            laserRPM: cleaning ? rand(290, 310) : 0,
        });
    },

    "GET /api/state": (req, res) => {
        deriveStates();
        jsonResponse(res, {
            uiState: state.uiState,
            robotState: state.robotState,
        });
    },

    "GET /api/error": (req, res) => {
        jsonResponse(res, {
            hasError: state.hasError,
            errorCode: state.errorCode,
            errorMessage: state.errorMessage,
        });
    },

    "GET /api/accel": (req, res) => {
        jsonResponse(res, {
            pitchDeg: randf(-2, 2),
            rollDeg: randf(-2, 2),
            xInG: randf(-0.05, 0.05, 4),
            yInG: randf(-0.05, 0.05, 4),
            zInG: randf(0.95, 1.0, 4),
            sumInG: randf(0.96, 1.01, 4),
        });
    },

    "GET /api/buttons": (req, res) => {
        jsonResponse(res, {
            softKey: false,
            scrollUp: false,
            start: false,
            back: false,
            scrollDown: false,
        });
    },

    "GET /api/lidar": (req, res) => {
        jsonResponse(res, generateLidarScan());
    },

    // Action routes
    "POST /api/clean/house": (req, res) => {
        state.cleaning = true;
        state.spotCleaning = false;
        deriveStates();
        sendOk(res);
    },

    "POST /api/clean/spot": (req, res) => {
        state.spotCleaning = true;
        state.cleaning = false;
        deriveStates();
        sendOk(res);
    },

    "POST /api/clean/stop": (req, res) => {
        state.cleaning = false;
        state.spotCleaning = false;
        deriveStates();
        sendOk(res);
    },

    "POST /api/sound": (req, res) => {
        // Accept and ignore — just acknowledge
        sendOk(res);
    },

    // Log routes
    "GET /api/logs": (req, res) => {
        jsonResponse(res, mockLogs);
    },

    "DELETE /api/logs": (req, res) => {
        sendOk(res);
    },

    // System routes
    "GET /api/system": (req, res) => {
        jsonResponse(res, {
            heap: rand(160000, 200000),
            heapTotal: 327680,
            uptime: Date.now() - bootTime,
            rssi: rand(-65, -40),
            spiffsUsed: rand(10000, 50000),
            spiffsTotal: 262144,
            ntpSynced: true,
            time: Math.floor(Date.now() / 1000),
            timeSource: "ntp",
            tz: state.tz,
        });
    },

    "GET /api/timezone": (req, res) => {
        jsonResponse(res, { tz: state.tz });
    },

    "GET /api/firmware/version": (req, res) => {
        jsonResponse(res, { version: "0.0.0-dev" });
    },

    // --- Mock control endpoints ---

    "POST /api/mock/battery": (req, res, query) => {
        const pct = parseInt(query.percent, 10);
        if (isNaN(pct) || pct < 0 || pct > 100) {
            return sendError(res, "percent must be 0-100", 400);
        }
        state.fuelPercent = pct;
        state.vBattV = 12.0 + (pct / 100) * 4.6;
        jsonResponse(res, {
            fuelPercent: pct,
            vBattV: parseFloat(state.vBattV.toFixed(2)),
        });
    },

    "POST /api/mock/dock": (req, res, query) => {
        const connected = query.connected === "true";
        state.extPwrPresent = connected;
        state.chargingActive = connected;
        if (!connected) state.chargingActive = false;
        jsonResponse(res, {
            extPwrPresent: state.extPwrPresent,
            chargingActive: state.chargingActive,
        });
    },

    "POST /api/mock/error": (req, res, query) => {
        if (query.clear === "true") {
            state.hasError = false;
            state.errorCode = 200;
            state.errorMessage = "";
        } else {
            const code = parseInt(query.code, 10);
            if (isNaN(code)) {
                return sendError(res, "code required", 400);
            }
            state.hasError = true;
            state.errorCode = code;
            state.errorMessage = ERROR_MESSAGES[code] || `Error ${code}`;
        }
        jsonResponse(res, {
            hasError: state.hasError,
            errorCode: state.errorCode,
            errorMessage: state.errorMessage,
        });
    },

    "POST /api/mock/state": (req, res, query) => {
        if (query.ui) state.uiState = query.ui;
        if (query.robot) state.robotState = query.robot;
        jsonResponse(res, {
            uiState: state.uiState,
            robotState: state.robotState,
        });
    },
};

// --- Core request handler ---

const handleRequest = async (req, res) => {
    const parsed = new URL(req.url, "http://localhost");
    const path = parsed.pathname;
    const query = Object.fromEntries(parsed.searchParams);

    // Match log file routes: GET/DELETE /api/logs/{filename}
    const logFileMatch = path.match(/^\/api\/logs\/(.+)$/);
    if (logFileMatch) {
        const filename = logFileMatch[1];
        if (req.method === "GET") {
            res.writeHead(200, {
                "Content-Type": "application/x-ndjson",
                "Content-Disposition": `attachment; filename="${filename.replace(/\.hs$/, "")}"`,
            });
            return res.end(mockLogContent);
        }
        if (req.method === "DELETE") {
            return sendOk(res);
        }
        return sendError(res, "method not allowed", 405);
    }

    // PUT /api/timezone
    if (req.method === "PUT" && path === "/api/timezone") {
        const body = await readBody(req);
        try {
            const data = JSON.parse(body);
            if (!data.tz) return sendError(res, "missing tz field", 400);
            state.tz = data.tz;
            return jsonResponse(res, { tz: state.tz });
        } catch {
            return sendError(res, "invalid JSON", 400);
        }
    }

    // Standard route lookup
    const key = `${req.method} ${path}`;
    const handler = routes[key];
    if (handler) {
        return handler(req, res, query);
    }

    // Not an API route — return false so Vite can handle it
    return false;
};

// --- Vite plugin ---
// Hooks into Vite's dev server middleware so /api/* requests are handled
// in-process — single port, single `npm run dev`

function mockApiPlugin() {
    return {
        name: "mock-api",
        configureServer(server) {
            server.middlewares.use(async (req, res, next) => {
                // Only intercept /api/* requests
                if (!req.url.startsWith("/api")) return next();

                // Latency simulation (50-200ms)
                await new Promise((r) => setTimeout(r, rand(50, 200)));

                const handled = await handleRequest(req, res);
                if (handled === false) next();
            });
        },
    };
}

module.exports = { mockApiPlugin };
