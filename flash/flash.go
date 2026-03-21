package main

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// Set via -ldflags at build time by GoReleaser.
// Defaults are for local development builds.
var (
	version        = "dev"
	esptoolVersion = "dev"
)

type flashOffsets struct {
	Bootloader string `json:"bootloader"`
	Partitions string `json:"partitions"`
	OTAData    string `json:"otadata"`
	App        string `json:"app"`
}

// progressReader wraps an io.Reader and prints download progress.
type progressReader struct {
	r       io.Reader
	total   int64
	current int64
	lastPct int
}

func (pr *progressReader) Read(p []byte) (int, error) {
	n, err := pr.r.Read(p)
	pr.current += int64(n)
	pct := int(float64(pr.current) / float64(pr.total) * 100)
	if pct != pr.lastPct {
		pr.lastPct = pct
		bar := pct / 2
		fmt.Printf("\r  [%-50s] %d%%", strings.Repeat("#", bar)+strings.Repeat(".", 50-bar), pct)
	}
	return n, err
}

// --- esptool management ---

// ensureEsptool returns the path to the pinned esptool version,
// downloading it if not already cached.
func ensureEsptool() (string, error) {
	cached := cachedEsptoolPath()
	if _, err := os.Stat(cached); err == nil {
		return cached, nil
	}

	return downloadEsptool()
}

func cachedEsptoolPath() string {
	cacheDir, _ := os.UserCacheDir()
	name := "esptool"
	if runtime.GOOS == "windows" {
		name = "esptool.exe"
	}
	return filepath.Join(cacheDir, "openneato", esptoolVersion, name)
}

func downloadEsptool() (string, error) {
	url, err := esptoolDownloadURL()
	if err != nil {
		return "", err
	}
	if url == "" {
		return "", fmt.Errorf("no esptool binary available for %s/%s", runtime.GOOS, runtime.GOARCH)
	}

	dest := cachedEsptoolPath()
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", err
	}

	return downloadAndExtract(url, dest, "esptool")
}

func resolveEsptoolVersion() (string, error) {
	if esptoolVersion != "dev" {
		return esptoolVersion, nil
	}
	// Dev builds: query GitHub API for latest esptool release tag
	resp, err := http.Get("https://api.github.com/repos/espressif/esptool/releases/latest") //nolint:gosec
	if err != nil {
		return "", fmt.Errorf("fetch latest esptool version: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	var release struct {
		TagName string `json:"tag_name"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&release); err != nil || release.TagName == "" {
		return "", fmt.Errorf("parse esptool release: %w", err)
	}
	return release.TagName, nil
}

func esptoolDownloadURL() (string, error) {
	v, err := resolveEsptoolVersion()
	if err != nil {
		return "", err
	}
	base := fmt.Sprintf("https://github.com/espressif/esptool/releases/download/%s/esptool-%s", v, v)
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "darwin/arm64":
		return base + "-macos-arm64.tar.gz", nil
	case "darwin/amd64":
		return base + "-macos-amd64.tar.gz", nil
	case "linux/amd64":
		return base + "-linux-amd64.tar.gz", nil
	case "linux/arm64":
		return base + "-linux-aarch64.tar.gz", nil
	case "windows/amd64":
		return base + "-windows-amd64.zip", nil
	default:
		return "", nil
	}
}

// --- Chip detection ---

// detectChip runs esptool to identify the connected chip type.
// Returns the lowercased chip name (e.g. "esp32-c3") matching release asset naming.
func detectChip(esptoolPath, portName string) (string, error) {
	cmd := exec.Command(esptoolPath, "-p", portName, "chip-id") //nolint:gosec
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("chip detection failed: %w\n%s", err, out)
	}

	// Parse "Detecting chip type... ESP32-C3" from esptool output
	const prefix = "Detecting chip type... "
	for _, line := range strings.Split(string(out), "\n") {
		if idx := strings.Index(line, prefix); idx >= 0 {
			chip := strings.TrimSpace(line[idx+len(prefix):])
			if chip != "" {
				return strings.ToLower(chip), nil
			}
		}
	}

	return "", fmt.Errorf("could not detect chip type from esptool output:\n%s", out)
}

// --- Firmware download ---

// downloadFirmwarePack downloads the board-specific firmware tarball from
// the latest GitHub release and extracts it into a temp directory.
// Returns the path to the temp directory containing the extracted files.
func downloadFirmwarePack(chip string) (string, error) {
	var url string
	if version == "dev" {
		url = fmt.Sprintf("https://github.com/renjfk/OpenNeato/releases/latest/download/openneato-%s-full.tar.gz", chip)
	} else {
		url = fmt.Sprintf("https://github.com/renjfk/OpenNeato/releases/download/v%s/openneato-%s-full.tar.gz", version, chip)
	}

	fmt.Printf("Downloading firmware %s for %s...\n", version, chip)

	tmp, err := os.MkdirTemp("", "openneato-firmware-*")
	if err != nil {
		return "", err
	}

	resp, err := http.Get(url) //nolint:gosec
	if err != nil {
		_ = os.RemoveAll(tmp)
		return "", fmt.Errorf("download firmware: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		_ = os.RemoveAll(tmp)
		return "", fmt.Errorf("download firmware: HTTP %d (is there a release for chip '%s'?)", resp.StatusCode, chip)
	}

	body := io.Reader(resp.Body)
	if resp.ContentLength > 0 {
		fmt.Printf("  (%.1f MB)\n", float64(resp.ContentLength)/1024/1024)
		body = &progressReader{r: resp.Body, total: resp.ContentLength}
	}

	if err := extractAllTarGz(body, tmp); err != nil {
		_ = os.RemoveAll(tmp)
		return "", fmt.Errorf("extract firmware: %w", err)
	}
	fmt.Printf("\r  [%-50s] 100%%\n", strings.Repeat("#", 50))

	return tmp, nil
}

// --- Flashing ---

// flashFirmware invokes esptool to flash the firmware pack at the given dir.
func flashFirmware(portName, esptoolPath, firmwareDir string) error {
	offsetsPath := filepath.Join(firmwareDir, "offsets.json")
	data, err := os.ReadFile(offsetsPath)
	if err != nil {
		return fmt.Errorf("read offsets.json: %w", err)
	}

	var offsets flashOffsets
	if err := json.Unmarshal(data, &offsets); err != nil {
		return fmt.Errorf("parse offsets: %w", err)
	}

	type image struct {
		offset string
		name   string
	}

	images := []image{
		{offsets.Bootloader, "bootloader.bin"},
		{offsets.Partitions, "partitions.bin"},
		{offsets.OTAData, "boot_app0.bin"},
		{offsets.App, "firmware.bin"},
	}

	args := []string{
		"-p", portName,
		"-b", "921600",
		"--before", "default-reset",
		"--after", "hard-reset",
		"write-flash", "-z",
	}

	for _, img := range images {
		path := filepath.Join(firmwareDir, img.name)
		info, err := os.Stat(path)
		if err != nil {
			return fmt.Errorf("missing %s in firmware pack", img.name)
		}
		fmt.Printf("  %s: %d bytes at %s\n", img.name, info.Size(), img.offset)
		args = append(args, img.offset, path)
	}

	fmt.Printf("Flashing via %s...\n", esptoolPath)

	cmd := exec.Command(esptoolPath, args...) //nolint:gosec
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("esptool failed: %w", err)
	}

	return nil
}

// --- Archive helpers ---

func downloadAndExtract(url, dest, binaryName string) (string, error) {
	resp, err := http.Get(url) //nolint:gosec
	if err != nil {
		return "", fmt.Errorf("download: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("download: HTTP %d", resp.StatusCode)
	}

	body := io.Reader(resp.Body)
	if resp.ContentLength > 0 {
		fmt.Printf("Downloading (%0.1f MB)...\n", float64(resp.ContentLength)/1024/1024)
		body = &progressReader{r: resp.Body, total: resp.ContentLength}
	}

	winName := binaryName + ".exe"
	if strings.HasSuffix(url, ".zip") {
		err = extractZipBinary(body, dest, winName)
	} else {
		err = extractTarGzBinary(body, dest, binaryName)
	}
	if err == nil {
		fmt.Printf("\r  [%-50s] 100%%\n", strings.Repeat("#", 50))
	} else {
		fmt.Println()
	}

	if err != nil {
		return "", err
	}
	return dest, nil
}

func extractTarGzBinary(r io.Reader, dest, binaryName string) error {
	gz, err := gzip.NewReader(r)
	if err != nil {
		return err
	}
	defer func() { _ = gz.Close() }()

	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if filepath.Base(hdr.Name) == binaryName && hdr.Typeflag == tar.TypeReg {
			f, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
			if err != nil {
				return err
			}
			_, err = io.Copy(f, tr) //nolint:gosec
			_ = f.Close()
			return err
		}
	}
	return fmt.Errorf("%s not found in archive", binaryName)
}

func extractZipBinary(r io.Reader, dest, binaryName string) error {
	tmp, err := os.CreateTemp("", "esptool-*.zip")
	if err != nil {
		return err
	}
	defer func() {
		_ = tmp.Close()
		_ = os.Remove(tmp.Name())
	}()

	size, err := io.Copy(tmp, r)
	if err != nil {
		return err
	}

	zr, err := zip.NewReader(tmp, size)
	if err != nil {
		return err
	}

	for _, f := range zr.File {
		if filepath.Base(f.Name) == binaryName {
			rc, err := f.Open()
			if err != nil {
				return err
			}
			out, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
			if err != nil {
				_ = rc.Close()
				return err
			}
			_, err = io.Copy(out, rc) //nolint:gosec
			_ = rc.Close()
			_ = out.Close()
			return err
		}
	}
	return fmt.Errorf("%s not found in archive", binaryName)
}

func extractAllTarGz(r io.Reader, destDir string) error {
	gz, err := gzip.NewReader(r)
	if err != nil {
		return err
	}
	defer func() { _ = gz.Close() }()

	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if hdr.Typeflag != tar.TypeReg {
			continue
		}
		dest := filepath.Join(destDir, filepath.Base(hdr.Name))
		f, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			return err
		}
		_, err = io.Copy(f, tr) //nolint:gosec
		_ = f.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// extractLocalPack extracts a local .tar.gz firmware pack into a temp directory.
func extractLocalPack(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer func() { _ = f.Close() }()

	tmp, err := os.MkdirTemp("", "openneato-firmware-*")
	if err != nil {
		return "", err
	}

	if err := extractAllTarGz(f, tmp); err != nil {
		_ = os.RemoveAll(tmp)
		return "", err
	}

	return tmp, nil
}
