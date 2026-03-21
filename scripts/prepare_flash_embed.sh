#!/bin/sh
#
# Populates tools/flash/embed/ from PlatformIO build output.
# Reads flash offsets from idedata.json so nothing is hardcoded.
#
# Usage:
#   scripts/prepare_flash_embed.sh <pio-build-dir>
#
# Example:
#   scripts/prepare_flash_embed.sh .pio/build/c3-release

set -eu

EMBED_DIR="flash/embed"

# Always start clean
rm -f "$EMBED_DIR"/*.bin "$EMBED_DIR"/*.json

BUILD_DIR="${1:?Usage: $0 <pio-build-dir>}"
IDEDATA="$BUILD_DIR/idedata.json"

if [ ! -f "$IDEDATA" ]; then
    echo "Error: $IDEDATA not found. Run pio build first." >&2
    exit 1
fi

# Extract offsets and paths from PlatformIO metadata
BL_OFFSET=$(jq -r '.extra.flash_images[0].offset' "$IDEDATA")
BL_PATH=$(jq -r '.extra.flash_images[0].path' "$IDEDATA")
PT_OFFSET=$(jq -r '.extra.flash_images[1].offset' "$IDEDATA")
PT_PATH=$(jq -r '.extra.flash_images[1].path' "$IDEDATA")
OD_OFFSET=$(jq -r '.extra.flash_images[2].offset' "$IDEDATA")
OD_PATH=$(jq -r '.extra.flash_images[2].path' "$IDEDATA")
APP_OFFSET=$(jq -r '.extra.application_offset' "$IDEDATA")

# Copy binaries
cp "$BL_PATH" "$EMBED_DIR/bootloader.bin"
cp "$PT_PATH" "$EMBED_DIR/partitions.bin"
cp "$OD_PATH" "$EMBED_DIR/boot_app0.bin"
cp "$BUILD_DIR/firmware.bin" "$EMBED_DIR/firmware.bin"

# Generate offsets.json
jq -n \
    --arg bl "$BL_OFFSET" \
    --arg pt "$PT_OFFSET" \
    --arg od "$OD_OFFSET" \
    --arg app "$APP_OFFSET" \
    '{bootloader:$bl,partitions:$pt,otadata:$od,app:$app}' \
    > "$EMBED_DIR/offsets.json"

echo "Embed dir populated from $BUILD_DIR:"
echo "  bootloader.bin  at $BL_OFFSET"
echo "  partitions.bin  at $PT_OFFSET"
echo "  boot_app0.bin   at $OD_OFFSET"
echo "  firmware.bin    at $APP_OFFSET"
