package main

import (
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.bug.st/serial"
	"golang.org/x/term"
)

const monitorBaud = 115200

// openMonitor opens an interactive serial terminal.
// Escape: Ctrl-A then Ctrl-X to exit.
func openMonitor(portName string) error {
	mode := &serial.Mode{
		BaudRate: monitorBaud,
		Parity:   serial.NoParity,
		DataBits: 8,
		StopBits: serial.OneStopBit,
	}

	port, err := serial.Open(portName, mode)
	if err != nil {
		return fmt.Errorf("open %s: %w", portName, err)
	}
	defer func() { _ = port.Close() }()

	_ = port.SetReadTimeout(100 * time.Millisecond)

	// Put terminal in raw mode
	fd := int(os.Stdin.Fd())
	oldState, err := term.MakeRaw(fd)
	if err != nil {
		return fmt.Errorf("set raw terminal: %w", err)
	}
	defer func() { _ = term.Restore(fd, oldState) }()

	// Handle Ctrl-C gracefully
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	done := make(chan error, 2)

	fmt.Printf("--- Monitor on %s at %d baud ---\r\n", portName, monitorBaud)
	fmt.Printf("--- Ctrl-C to exit ---\r\n")

	// Serial -> stdout
	go func() {
		buf := make([]byte, 4096)
		synced := false // discard until first newline (ROM bootloader garbage)
		for {
			n, err := port.Read(buf)
			if n > 0 {
				for i := 0; i < n; i++ {
					b := buf[i]
					if !synced {
						if b == '\n' {
							synced = true
						}
						continue
					}
					switch {
					case b == '\n':
						_, _ = os.Stdout.Write([]byte{'\r', '\n'})
					case b == '\r', b == '\t', b >= 32 && b <= 126:
						_, _ = os.Stdout.Write([]byte{b})
					}
				}
			}
			if err != nil {
				if err != io.EOF {
					done <- err
				}
				return
			}
		}
	}()

	// Stdin -> serial
	go func() {
		buf := make([]byte, 256)
		for {
			n, err := os.Stdin.Read(buf)
			if err != nil {
				done <- err
				return
			}
			for i := 0; i < n; i++ {
				b := buf[i]

				// Ctrl-C: exit monitor
				if b == 0x03 {
					done <- nil
					return
				}

				// Map CR -> LF (Enter in raw mode sends CR, device expects LF)
				if b == '\r' {
					b = '\n'
				}

				_, _ = port.Write([]byte{b})
			}
		}
	}()

	select {
	case err := <-done:
		fmt.Printf("\r\n--- Disconnected ---\r\n")
		return err
	case <-sig:
		fmt.Printf("\r\n--- Interrupted ---\r\n")
		return nil
	}
}
