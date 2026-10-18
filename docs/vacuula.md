# Handing the robot over to a Vacuula server

[Vacuula](https://github.com/vacuula/vacuula) is a self-hosted replacement for Neato's cloud. The robot's own
WiFi stack connects to it the way it used to connect to Neato, which brings back everything the cloud API
exposed: the robot's SLAM floor maps, persistent maps, no-go lines, zones, the robot's own schedule and run
history. OpenNeato talks to the same mainboard over the debug UART and covers what the serial protocol exposes:
live sensors, manual driving, LIDAR, motor control. The two can run side by side.

Vacuula's own installer does the hand-over from a PC plugged into the robot's USB port. This page describes
doing the serial half through the OpenNeato bridge instead, so the robot never has to be opened again, and the
rest from a small script next to the Vacuula server.

## How the hand-over works

On a Botvac D3-D7 the whole thing is one QNX board. Vacuula's installer:

1. uploads a small `tar.gz` (its two shell scripts, the server's certificate, the server IP) with the robot's
   own `Upload Tarball` serial command; the robot parks it in temporary storage;
2. tells the robot to forget its WiFi, which brings up the robot's setup access point;
3. from a computer joined to that access point, calls the robot's provisioning API with the home WiFi
   credentials, the Vacuula hostnames and a boot hook that extracts and runs the uploaded script;
4. the script runs on the robot's next boot: it points the cloud client at the server, installs the certificate,
   lifts the model-based feature limits and reboots.

Steps 1 and 2 need the robot's serial port. The bridge already owns that port, so it does them. Steps 3 and 4
happen over WiFi and stay on the PC side.

## Requirements

- A Vacuula server on your LAN with a **fixed IPv4 address** and **port 443 free** (Gen3 robots are hard-wired
  to 443). `tools/vacuula/docker-compose.yml` runs the official image; Home Assistant OS users can install the
  Vacuula add-on instead.
- Robot firmware **4.5.3-189 or 4.6.0-72**. Vacuula's scripts patch the robot's cloud client at byte offsets
  that only exist for those builds; the bridge refuses other versions unless you pass `force`. `GET /api/version`
  shows what the robot runs (`4.5.3.189.0` means 4.5.3-189).
- A computer that can join the robot's setup WiFi network for a few minutes, and Python 3.9+ on it.
- The bridge must have identified the robot (`skeyReady` in the status below), since the network reset needs the
  SKey the bridge derives at boot.

## Running it

Start the server once:

```bash
docker compose -f tools/vacuula/docker-compose.yml up -d
```

Then, from the computer that will join the robot's setup network:

```bash
python tools/vacuula/provision.py --bridge neato.local --server 192.168.1.10 --ssid "MyWiFi"
```

The script checks the server and downloads its certificate, fetches Vacuula's Gen3 scripts from a pinned commit
of `vacuula/installer`, builds the archive, posts it to the bridge and reports the robot's answer. It then asks
you to join the robot's setup network, waits for the robot's provisioning API, sends the configuration and
follows the robot's progress. The verdict is the robot appearing as connected in the Vacuula server's robot
list; the robot's console lines are tailed through the bridge meanwhile as a hint, but the install script ends
by rebooting the robot, which also reboots the bridge (it is powered from the robot's debug port), so those
lines can stop early. Expect the whole run to take five to ten minutes.

`--no-reset` uploads only, `--skip-provision` stops after the reset, `--archive-out` keeps a copy of the
archive, `--scripts-dir` uses local copies of Vacuula's scripts, `--force` bypasses the model/firmware gate,
`--password-file` reads the WiFi password from a file (the prompt is easy to mis-paste). On Windows,
`tools/vacuula/run_handover.ps1` wraps the script and joins the robot's setup network with the PC's own
Wi-Fi adapter when asked, so Ethernet keeps the bridge reachable throughout.

**At the end the robot switches itself off.** Vacuula's install script finishes with a shutdown. Press the
robot's button to turn it on again; it then rejoins your WiFi and shows up on the Vacuula server within a
minute or two (the script keeps waiting for that for ten minutes).

One difference from Vacuula's own installer: their scripts write their first line to the robot's USB console
and die there when no USB cable is attached. The archive built here sends those writes to the debug UART
instead, which is also what lets the bridge show the whole install as it runs.

## Bridge endpoints

| Endpoint | What it does |
|----------|--------------|
| `GET /api/vacuula/status` | Robot model, firmware and serial, whether the firmware is one Vacuula knows, SKey readiness, upload phase and byte counts, whether the reset was sent, quiet-window state. |
| `POST /api/vacuula/upload?reset=1&force=0` | Body is the raw `tar.gz`. Streams it to the robot, then (with `reset=1`, the default) sends the network reset and opens the quiet window. Returns 202 once the transfer is queued; follow it through `/status`. |
| `POST /api/vacuula/reset-network` | Resend the WiFi reset on its own, for a retry. |
| `GET /api/vacuula/log?since=N` | Console lines the robot printed on the debug UART during the quiet window, with sequence numbers. |
| `POST /api/vacuula/quiet?ms=N` | (Re)start the quiet window; the script does this right before provisioning, since joining the setup network can take a while. |
| `POST /api/vacuula/resume` | End the quiet window early. |

The upload uses the robot's blast protocol: the bridge sends `Upload Tarball size N`, waits for the robot's
request byte, streams the archive in 1 KB chunks with a short pause between them, appends a 32-bit sum of the
bytes and waits for the robot's acknowledgement. Archives are capped at 64 KB and must be sent with a
`Content-Length` (no chunked encoding); a real one is a few kilobytes. The bridge refuses to start while a
manual clean is active, and a refused start (unsupported model or firmware, SKey not ready) discards the
received archive, so send it again with `force=1` if that is what you want.

## Things to know

- **Same power cycle.** The archive sits in the robot's temporary storage until the boot hook extracts it. If
  the robot is power-cycled between the upload and the provisioning, upload again.
- **Quiet window.** After the reset the bridge stops polling the robot for ten minutes so the install runs
  undisturbed, and it captures whatever the robot prints on the debug UART instead. Commands already queued are
  failed when the window opens and closes, so a scheduled clean cannot fire late into the install, and new
  commands fail fast instead of waiting. The web UI and the Home Assistant integration show the robot as
  unavailable meanwhile. The script re-arms the window for about twenty-two minutes right before joining the
  robot's setup network (its own timeouts for that network, the provisioning and the console tail, plus a
  margin for the reboot); `POST /api/vacuula/resume` ends it early.
- **Two controllers.** Once Vacuula is installed, both it and OpenNeato can start and stop cleaning. Decide
  which one owns scheduling: the bridge's scheduler and the robot's own schedule (managed by Vacuula) will both
  fire if you set both.
- **Console noise.** Vacuula's scripts echo their log lines to the robot's debug UART at every boot. The bridge's
  response parser keys on the Ctrl-Z terminator and resynchronises on mismatches, so expect an occasional failed
  poll right after the robot boots rather than a broken bridge.
- **Maps never arriving.** After an exploration the robot asks the server for an upload job and posts the map to
  the address the server names, which Vacuula derives from `host.docker.internal` or, failing that, from the host's
  outbound IP. On a multi-homed host that fallback can be an address the robot cannot reach, and the server log
  then shows `POST /maps/upload_jobs` repeating every minute with no `POST /` after it. Pin the address with
  `extra_hosts` in the compose file (see `tools/vacuula/docker-compose.yml`); the robot resolves the resulting
  `vacuula-local-<ip>.nip.io` name through the hosts entry the install script wrote.
- **Internet dependencies.** The robot still reaches `pool.ntp.org` for time and checks `8.8.8.8` for
  connectivity; see Vacuula's FAQ.
- **A robot that will not join.** If the robot reaches "joining WiFi" and then goes quiet with its WiFi
  reported off (`GetWifiStatus` through `/api/serial` shows `Enabled, no`, `Wifi Mode, off`, and the reset
  command answers "not allowed"), its WiFi stack is wedged. Restart the robot with `POST /api/power?action=restart`
  and run the script again; the saved credentials survive and the robot rejoins on its own.
- **Secrets on the console.** Vacuula's script prints the robot's settings database on the console, including
  the robot's cloud secret. The bridge keeps those lines in memory until its next network reset or reboot;
  the runner does not print them, but `GET /api/vacuula/log` does. Restart the bridge after a successful
  hand-over if that matters to you.
- **Licensing.** Vacuula is AGPL-3.0. The script downloads its shell scripts at run time and bundles them into
  the archive for your robot; nothing from Vacuula is vendored into this MIT repository.
