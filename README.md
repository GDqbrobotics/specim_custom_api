# Specim Custom Camera API

Python control of the **SpecimIQ** hyperspectral camera **without the
SpecimIQ Studio GUI** — discovery, connection, trigger acquisitions, read/
write camera parameters, stream frame data, and retrieve datasets over FTP.

## What actually talks to the camera (verified by binary analysis)

This was reverse-engineered from the SpecimIQ Studio installation in
`..` (build string `2019.05.29.2` in `SpecimIQStudio.exe`), specifically
`eyeapi.dll` (the SDK, byte-identical to `libs/eyeapi2.dll`,
MD5 `815b1d5299a80f6f35cbf3f423970dce` — note: *not* the SpecimInsight
`libs/eyeapi.dll`, MD5 `59f378b74c507b5d4ed6877e34afe345`).

```
┌──────────────┐  ctypes   ┌──────────────────┐  JSON-RPC 2.0 over TCP   ┌─────────────┐
│ Python API   │ ────────► │  eyeapi.dll      │ ───────────────────────► │ SpecimIQ    │
│ (this repo)  │           │  (Specim Eye SDK)│  port = 5000 + camera#   │ camera      │
└──────────────┘           └──────────────────┘  (camera 1 → 5001)      │ (Eye service)│
                                 │
                                 └── UDP discovery: port 54546, multicast ff08::1
```

Key facts (with evidence):

| Fact | Evidence |
|---|---|
| `eyeapi.dll` (IQ) is the official camera SDK; `SpecimIQStudio.exe` links it | PE import table of the exe |
| The SDK's C API exports are **C++-mangled** in this build (no plain `EYE_*` names in the export table of *either* the IQ or the Insight DLL) | PE export table: 140 exports, all `?...@@...` names, e.g. `?EYE_Load@@YAHXZ`, `?EYE_GetFloat@@YAH_JPEB_WPEAN@Z`. The wrapper resolves those to `EYE_*` aliases during construction |
| Wire protocol is **JSON-RPC 2.0 over TCP** (qjsonrpc) | `QJsonRpcSocket`/`QTcpSocket` in the IAT; `qjsonrpc.dll` (IQ) / `qjsonrpc1.dll` (Insight) shipped alongside; format strings `eye%1.getFloat` etc. |
| Methods are `eye{N}.getFloat / setFloat / getInt / setInt / getString / setString / getBool / setBool / isReadable / isWriteable / getFloatMax / getIntMax / ...` where **N = 1-based device number** and the **feature name is a JSON parameter** | Format strings in `eyeapi.dll`; e.g. `eye1.getFloat("Camera.ExposureTime")` |
| **Camera 1 listens on TCP port 5001** (base port 5000 + 1-based index) | Connect code: `r8d = 0x1389 (5001); add r8w, idx`; `5000` elsewhere is the 5000 ms `waitForConnected()` timeout and the global `s_conn_timeout` |
| **Discovery: UDP port 54546, joins multicast `ff08::1`**, receives JSON datagrams with keys `type` (`"iq"`), `services` (`"rpc"`), `addresses` (`ADDR:<ipv4>`, `MAC:<..>`, `IPV6ADDR:<..>`); USB cameras present as **`192.168.7.2` on a virtual `usb0` interface** | `QUdpSocket::bind(54546)` + `joinMulticastGroup` in the IQ DLL; strings `"192.168.7.2"`, `"usb0"`, `"fe80::ec00:00FF:FE00:0000"`, `"ipv6addr good:"`; `QEyeSystem::isUSBAddress` / `isUSBDevice` |
| Discovery is **asynchronous** (global static `DiscoveryListener`, runs in the SDK's own Qt thread). First `EYE_OpenConnectedDevices(..., True)` starts it; later calls return immediately with the number of opened devices in the out-parameter | `?m_discoveryListener@QEyeSystem@@2VDiscoveryListener@@A` static export; disassembly of `?EYE_OpenConnectedDevices@@YAHPEAH_N@Z` and `?openConnectedDevices@QEyeSystem@@QEAAHH@Z` |
| **FTP file server on port 5010** (fixed), user **`ftpuser`** / password **`password`** — used to list/download datasets (`.raw`/`.hdr`/...) from the camera's SD card | `?getFtpPort@QEyeSystem@@QEAAHXZ` = `mov eax, 0x1392; ret` (5010); exe strings `ftpuser`, `password`, `QFtp::connectToHost/login/get/list` |
| IQ adds **notifications / applications / calibration / datasets** RPC methods | Strings `eye%1notif.getNotifications`, `readDatasets`, `readInstalledApplications`, `sendCalibration`, `copyToCalibrationFolder`, `installApplication`, `openFile`, `closeFile`, `frameReceived(QByteArray)` |
| The IQ camera also speaks the **legacy PX ASCII protocol** (Insight/FX compatible) | `pfAsciiProtocolHelper` class with `CreateGetCommand/CreateSetCommand/ParseReturnMessage` and the legacy property table (IDs 188, 796-798, 800-801, names `Average.Value`, `BadPixelCorrection.*`, `Crosshairs.*`, `DataOutMode`, `DigitalGain`, ...) |
| Insight/Studio must **not** be running for this to work | `eyeapi.dll` embeds its own Qt event-loop thread (`s_qapp_loop`, `s_thr_qapp_loop`) — it is standalone |
| The Studio GUI itself constructs `QEyeSystem(false, false, parent)` (no discovery flag) and polls `checkConnectedDevicesChanged()` + `openConnectedDevices(100ms)` from a timer — i.e. the *global* discovery listener does the finding, not the instance flag | Disassembly of `SpecimIQStudio.exe` (call sites at `0x1400fd79f` and `0x140102bc4`) |

The "Cortex Engine" mentioned in the old draft is *not* a camera IPC server:
it is Insight's internal Python-scripting bridge (`cortex.pyd`,
`engine.get / engine.set`), which only exists while Insight is running.

### Useful SpecimIQ feature names (159 total in the IQ feature table)

```
Camera.Model / Camera.SerialNumber          string
Camera.IsConnected                          bool
Camera.ExposureTime                         float (µs)
Camera.ExposureTime.Auto                    int
Camera.FrameRate                            float
Camera.Count                                int    (frames to record)
Camera.LinesPerFrame, Camera.Image.Width/Height/SizeBytes  int
Camera.Wavelength.Start / .Stop             float (nm)
Camera.Binning.Spatial / .Spectral          int
Camera.Trigger.Mode                         string
Camera.MROI.Enable / .Left / .Top / .Width / .Height  int
Camera.Shutter.IsOpen                       bool
Camera.FrameQueue.Length                    int
Acquisition.Start / .Stop                   set to 1
Acquisition.Acquiring                       bool (read)
Acquisition.Error                           string
Acquisition.FrameCounter / .Reset           int
System.DeviceName / System.FirmwareVersion  string
System.NumScans                             int
System.FreeSpace / .FreeSpaceInCaptures     string
System.Fileserver.Start / .Stop             FTP on/off
System.WlanOn / .WlanDhcpServer / .WlanStaticIp   WiFi
System.BatteryCapacity                      int
AsciiConnection.Start / .Stop               legacy ASCII port on/off
```

The full annotated list is in `specim_eye_api.py` (`KNOWN_FEATURES`, 120
entries with types).

## Files

| File | Purpose |
|---|---|
| `specim_eye_api.py` | **Use this.** ctypes wrapper around the IQ `eyeapi.dll` (loaded as `eyeapi2.dll`) + pure-Python UDP discovery + raw JSON-RPC client |
| `acquire.py` | Automation pipeline: discover → connect → identify → capture N frames via the data callback → stop |
| `test_api.py` | Test suite: `python test_api.py` (see below) |
| `wine_test/` | Smoke test that loads the IQ DLL under Wine on Linux (`run.sh`) |
| `libs/eyeapi2.dll` | **IQ SDK** (identical to `../eyeapi.dll`) — the one the wrapper loads |
| `libs/eyeapi.dll` | SpecimInsight SDK (kept for reference; *different* binary) |
| `libs/qjsonrpc.dll` | IQ JSON-RPC library (required next to `eyeapi2.dll`) |
| `libs/qjsonrpc1.dll` | Insight JSON-RPC library |

## Requirements

- Python 3.8+ on the machine where the camera is attached (Windows natively,
  or Linux/Wine — see `wine_test/`)
- The SpecimIQ Studio installation next to `libs/` (provides the Qt DLLs and
  the SDK; on Windows just point `dll_dir=` at the install root)
- Camera powered on and connected:
  - **USB**: appears as a network endpoint at `192.168.7.2` (virtual `usb0`
    interface; on Windows the USB Ethernet gadget gets 192.168.7.x
    automatically)
  - **WiFi/LAN**: camera's IP, found via UDP discovery
- The Microsoft Visual C++ 2015–2022 Redistributable (x64) on Windows
- No Insight/Studio process running

### DLL dependencies (IQ, verified)

For `eyeapi2.dll` (IQ) to load, keep these alongside it:

```
eyeapi2.dll   (== ../eyeapi.dll)
qjsonrpc.dll  (IQ version, MD5 00f414cbc4b2ab5ce81d684fb2fbb564)
Qt5Core.dll
Qt5Network.dll
```

Plus the MSVC runtime (`MSVCP140.dll`, `VCRUNTIME140.dll` — system-installed
via the VC++ redistributable on Windows; built into Wine ≥ 6 on Linux).

## Quick start

```python
from specim_eye_api import SpecimCameraAPI, discover_cameras

# optional: see what discovery finds (pure Python, no DLL)
for cam in discover_cameras(timeout=5.0):
    print(cam)          # {'type': 'iq', 'addresses': ['ADDR:192.168.7.2', ...]}

cam = SpecimCameraAPI()          # dll_dir defaults to libs/ (IQ eyeapi2.dll)
cam.connect()                    # starts async UDP discovery, polls up to 20 s
h = cam.h(0)                     # first camera handle

print(cam.get(h, "Camera.Model"))
print(cam.get(h, "Camera.SerialNumber"))

# configure
cam.set_float(h, "Camera.ExposureTime", 4.0)   # µs
cam.set_int(h, "Camera.Count", 1)              # frames to record

# trigger acquisition and wait for it to finish
cam.start(h)                      # Acquisition.Start = 1
cam.wait_acquisition_done(h)      # poll Acquisition.Acquiring
cam.stop(h)                       # Acquisition.Stop = 1

# ... or one-liner:
cam.acquire(h, exposure_us=4.0, count=1)

# real-time frame data in Python
frames = []
cam.add_data_callback(lambda data, s1, s2: frames.append(bytes(data)))

# retrieve files the camera wrote (.raw/.hdr) via FTP
port = cam.ftp_port()             # 5010, user ftpuser / password password

cam.disconnect()
```

## Triggering from outside (automation / photogrammetry turntable)

```python
cam = SpecimCameraAPI(); cam.connect(); h = cam.h(0)

for i, angle in enumerate(range(0, 360, 5)):
    move_stage(angle)                      # your stage controller
    wait_for_stage_settling()
    cam.acquire(h, exposure_us=4.0, count=1)
    download_dataset(i, cam.ftp_port())    # FTP: ftpuser/password @ 5010
```

Or run the ready-made pipeline: `python acquire.py --count 10 --out captures/`.

## Testing

```
python test_api.py                          # read-only DLL-backed checks
python test_api.py --no-dll                 # test raw JSON-RPC
python test_api.py --no-dll --host 169.254.222.101  # connect to a given IP
python test_api.py --debug                  # include eyeapi diagnostic logging
python test_api.py --acquire                # trigger a capture through the DLL
python test_api.py --no-dll --acquire       # trigger capture through JSON-RPC
python test_api.py --probe 169.254.222.101 # diagnostics (see below)
```

### `--probe` (use when you get "connection refused")

`--probe HOST` does three things and prints the result of each:
1. **Listens on UDP 54546** (multicast `ff08::1`, exactly like the SDK) for a
   few seconds. If the camera is alive on the network you'll see its JSON
   announcement and its *current* IP — even when TCP is refused.
2. **TCP-probes ports 5000–5020** on `HOST` and tells you which are open.
   The RPC port is `5001` for the first camera.
3. **If 5001 is open**, sends `eye1.testAlive` and prints the reply.

This separates "camera not reachable at all" from "reachable but RPC port
5001 is closed".

### USB camera address (Windows)

Over USB the camera's virtual Ethernet usually gets an APIPA address like
`169.254.222.101` (not `192.168.7.x` — that's the Linux `usb0` case). You do
**not** need to declare the address: `SpecimCameraAPI().connect()` discovers it
automatically via UDP. To drive the raw `--no-dll` path, find the address with
`--probe` (or `ipconfig`) and pass it as `--host`.

On Linux without a camera, the DLL can still be smoke-tested under Wine:

```
cd wine_test && ./run.sh
# expected: "OK loaded eyeapi2.dll", all symbols resolve,
#           EYE_GetFtpPort returns 5010 once opened, 0 devices (no camera)
```

## Troubleshooting

- **`eyeapi.dll not found`** — pass
  `SpecimCameraAPI(dll_dir=r"C:\...\SpecimIQ")` (the Studio install root).
- **`no camera found after 20s of discovery`** — camera not powered / not
  connected. USB: check `ip addr` for a `usb0`-like interface in the
  192.168.7.x range (or Windows: `ipconfig` for 192.168.7.1). WiFi:
  `python -c "from specim_eye_api import discover_cameras; print(discover_cameras(5))"`
  to see who announces itself.
- **Feature read fails** — use `cam.is_readable(h, "...")` to confirm the
  feature exists on this firmware; names can differ between camera models.
- **Port conflicts / multiple cameras** — camera N uses RPC port 5000+N
  (5001, 5002, ...); FTP is always 5010 on the *first* camera in this build
  (`getFtpPort` is hardcoded). Check with `netstat -an | findstr 5001`.
- **`GetFtpPort` returns 0** — call `connect()` first (the SDK singleton must
  exist); it then returns 5010.
- Want to see what the SDK sends: `cam.set_logging(True)`.
- **`QEventLoop: Cannot be used without QApplication` / `startTimer: ...`
  then hang** (with `--debug`) — the SDK's RPC path runs on the *calling*
  thread and needs a `QCoreApplication` there. `SpecimCameraAPI.__init__`
  now creates one automatically (see `_install_qapp`). You should see
  `[specim] QCoreApplication created on main thread (0x...)` before
  connecting. If you instead see `[specim] QApp setup skipped (...)`, the
  bundled `Qt5Core.dll` was missing/unloadable — copy it next to the other
  DLLs.
- **`[WinError 10061] ... expressément refusée` (connection refused)** — the
  camera host is reachable but its RPC port is closed *right now*. Run
  `python test_api.py --probe <ip>` to see (a) whether the camera is still
  announcing on UDP and (b) which ports are open. If it announces but 5001 is
  closed, the camera's RPC service is off for this state — power-cycle the
  camera, make sure SpecimIQ Studio is closed, re-plug USB, wait ~10 s, and
  re-run. (The DLL path retries discovery and is generally more tolerant.)

## License

Provided for integration with Specim camera systems; `eyeapi.dll` and the
camera firmware remain Specim property (see `../licenses/`).
