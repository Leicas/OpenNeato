"""
PlatformIO pre-build script: inject environment variables into build/upload config.

Supported variables:
    FIRMWARE_VERSION  — injected as -DFIRMWARE_VERSION build flag
                        (fallback in config.h: "0.0.0-dev")
    NEATO_HOST        — sets OTA upload target host (required for OTA env)

Set BUILD_FRONTEND=1 to run the frontend build (npm run build) before
compiling firmware, ensuring web_assets.h is up to date.

Usage:
    FIRMWARE_VERSION=1.2.3 pio run -e Debug
    NEATO_HOST=10.10.10.15 pio run -e OTA -t upload
    BUILD_FRONTEND=1 pio run -e Debug                 # builds frontend + firmware
    BUILD_FRONTEND=1 NEATO_HOST=10.10.10.15 pio run -e OTA -t upload
"""

import os
import subprocess
import sys

Import("env")

# -- Frontend build (Full environments) ----------------------------------------

if os.environ.get("BUILD_FRONTEND"):
    frontend_dir = os.path.join(env["PROJECT_DIR"], "frontend")
    print("Building frontend (npm run build)...")
    result = subprocess.run(["npm", "run", "build"], cwd=frontend_dir)
    if result.returncode != 0:
        sys.exit("Error: Frontend build failed")
    print("Frontend build complete")

# -- FIRMWARE_VERSION ----------------------------------------------------------

version = os.environ.get("FIRMWARE_VERSION")
if version:
    env.Append(CPPDEFINES=[("FIRMWARE_VERSION", '\\"%s\\"' % version)])

# -- NEATO_HOST (OTA upload) ---------------------------------------------------

host = os.environ.get("NEATO_HOST")
if host:
    env.Replace(UPLOAD_PORT=host)
elif env["PIOENV"] == "OTA":
    sys.exit("Error: NEATO_HOST is required for OTA uploads. "
             "Usage: NEATO_HOST=<ip> pio run -e OTA -t upload")
