// openneato-flash flashes OpenNeato firmware to an ESP32 and opens
// a serial monitor for first-time WiFi configuration.
//
// The complete flash images and their offsets are embedded in this binary.
// Just plug in the device and run:
//
//	openneato-flash
package main

import (
	"flag"
	"fmt"
	"os"
)

func main() {
	port := flag.String("port", "", "Serial port (auto-detected if not set)")
	listPorts := flag.Bool("list", false, "List available serial ports")
	noMonitor := flag.Bool("no-monitor", false, "Skip serial monitor after flashing")
	monitorOnly := flag.Bool("monitor", false, "Open serial monitor without flashing")

	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "openneato-flash - Flash and configure OpenNeato firmware\n\n")
		fmt.Fprintf(os.Stderr, "Options:\n")
		flag.PrintDefaults()
	}

	flag.Parse()

	if *listPorts {
		ports, err := detectPorts()
		if err != nil {
			fatal("Failed to list ports: %v", err)
		}
		if len(ports) == 0 {
			fmt.Println("No USB serial ports found.")
			return
		}
		fmt.Println("Available serial ports:")
		for _, p := range ports {
			marker := " "
			if p.IsESP {
				marker = "*"
			}
			fmt.Printf("  %s %s  (%s)\n", marker, p.Name, p.Description)
		}
		return
	}

	// Find serial port
	portName, err := findPort(*port)
	if err != nil {
		fatal("%v", err)
	}
	fmt.Printf("Using port: %s\n", portName)

	// Flash (unless monitor-only)
	if !*monitorOnly {
		esptoolPath, err := findEsptool()
		if err != nil {
			fmt.Println("esptool not found in PATH.")
			fmt.Print("Download it from GitHub? [Y/n] ")
			var answer string
			_, _ = fmt.Scanln(&answer)
			if answer != "" && answer != "y" && answer != "Y" {
				fatal("Install esptool manually: pip install esptool")
			}
			esptoolPath, err = downloadEsptool()
			if err != nil {
				fatal("Failed to download esptool: %v", err)
			}
		}

		if err := flashFirmware(portName, esptoolPath); err != nil {
			fatal("%v", err)
		}
		fmt.Println("Flash complete!")
	}

	// Monitor
	if !*noMonitor {
		if !*monitorOnly {
			fmt.Println("\nOpening serial monitor for WiFi setup...")
		}
		if err := openMonitor(portName); err != nil {
			fatal("Monitor error: %v", err)
		}
	}
}

func fatal(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "Error: "+format+"\n", args...)
	os.Exit(1)
}
