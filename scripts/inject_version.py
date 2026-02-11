"""
PlatformIO pre-build script: inject FIRMWARE_VERSION into build flags.

Usage:
    FIRMWARE_VERSION=1.2.3 pio run -e Debug

If FIRMWARE_VERSION is not set, the fallback in config.h ("0.0.0-dev") applies.
"""

import os

Import("env")

version = os.environ.get("FIRMWARE_VERSION")
if version:
    env.Append(CPPDEFINES=[("FIRMWARE_VERSION", '\\"%s\\"' % version)])
