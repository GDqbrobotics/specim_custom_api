# Specim Custom Camera API

Python control of Specim hyperspectral cameras (FX10/FX17/...) **without the
Specim Insight GUI** — trigger acquisitions, read/write camera parameters,
stream frame data.

## What actually talks to the camera (verified by binary analysis)
We disassembled the binaries in this exact
installation (Insight 1.4.0.83) and found the real mechanism:

```
┌──────────────┐  ctypes   ┌──────────────────┐  JSON-RPC 2.0 over TCP  ┌─────────────┐
│ Python API   │ ────────► │  eyeapi.dll      │ ──────────────────────► │ Specim      │
│ (this repo)  │           │  (Specim Eye SDK)│  port = 5000 + camera# │ camera      │
└──────────────┘           └──────────────────┘  (camera 1 → 5001)     │ (Eye service)│
                                                                       └─────────────┘
```

Key facts (with evidence):

| Fact | Evidence |
|---|---|
| `eyeapi.dll` is the official camera SDK; Insight uses it | Insight links it; it exports the `EYE_*` C API |
| The SDK's C API exports are C++-decorated on this build | The wrapper resolves those exported names to its `EYE_*` aliases during construction |
| Wire protocol is **JSON-RPC 2.0 over TCP** (qjsonrpc), not text lines | `QJsonRpcSocket`/`QTcpSocket` in the IAT; `qjsonrpc1.dll` shipped alongside |
| Methods are `eye{N}.getFloat / setFloat / getInt / setInt / getString / setString / getBool / setBool / isReadable / isWriteable / getFloatMax ...` where **N = 1-based device number** and the **feature name is a JSON parameter** | Format strings `eye%1.getBool` etc. in `eyeapi.dll`; e.g. `eye1.getFloat("Camera.ExposureTime")` |
| **Camera 1 listens on TCP port 5001** (base port 5000 + 1-based index) | Base port constant in `s_eye_config` (5000) in `eyeapi.dll`; connect code computes `5001 + (index-1)`; debug string `"Try open: <host> port 5001"`; the 5000 everywhere else is the 5000 ms `waitForConnected()` timeout |
| Feature names are `Camera.*` / `Acquisition.*` / `System.*` (capitalized) | Feature table embedded in `insight.exe` |
| Acquisition is started/stopped via the `Acquisition.Start` / `Acquisition.Stop` features (set to 1); status via `Acquisition.Acquiring` | Same feature table; used by Insight's capture UI |
| Frame data can be streamed to the PC (`EYE_Add_Data_Callback`, `EYE_Wait`) or saved to file (`EYE_OpenFile`/`EYE_CloseFile`); camera-side files (`.raw/.hdr`) are retrieved via FTP, port from `EYE_GetFtpPort` | Exported functions in `eyeapi.dll` |
| Insight must **not** be running for this to work | `eyeapi.dll` embeds its own Qt event-loop thread (`s_qapp_loop`) — it is standalone |

The "Cortex Engine" mentioned in the old draft is *not* a camera IPC server:
it is Insight's internal Python-scripting bridge (`cortex.pyd`, `engine.get /
engine.set` used by the scripts in `../scripts/`), which only exists while
Insight is running.

### Useful FX17 feature names (from the Insight feature table)

```
Camera.Model / Camera.SerialNumber          string
Camera.ExposureTime                         float (µs)
Camera.FrameRate                            float (lines/s)
Camera.Count                                int    (lines to record)
Camera.LinesPerFrame, Camera.Image.Width/Height  int
Camera.Wavelength.Start / .Stop             float (nm)
Camera.Binning.Spatial / .Spectral          int
Camera.Trigger.Mode                         string
Camera.Shutter.IsOpen                       bool
Camera.FrameQueue.Length                    int
Acquisition.Start / .Stop                   set to 1
Acquisition.Acquiring                       bool (read)
Acquisition.FrameCounter                    int
System.DeviceName / System.FirmwareVersion  string
System.Fileserver.Start / .Stop             FTP on/off
```

The full annotated list is in `specim_eye_api.py` (`KNOWN_FEATURES`).

## Files

| File | Purpose |
|---|---|
| `specim_eye_api.py` | **Use this.** ctypes wrapper around `eyeapi.dll` (+ optional raw JSON-RPC fallback client) |
| `test_api.py` | Test suite: `python test_api.py` (add `--acquire` to actually trigger a capture) |

## Requirements

- Python 3.8+ (Windows, on the machine where the camera is attached)
- The Specim Insight installation (this folder — it provides `eyeapi.dll`
  and the Qt DLLs it depends on)
- The Specim camera driver installed and camera powered on
- No Insight process running, no numpy needed

### DLL dependencies

For the `eyeapi.dll` load used by `SpecimCameraAPI`, keep these five DLLs in
the Insight installation directory alongside `eyeapi.dll`:

```
eyeapi.dll
qjsonrpc1.dll
Qt5Core.dll
Qt5Network.dll
Qt5WebSockets.dll
```

This is the transitive PE import dependency closure confirmed for Insight
1.4.0.83; the constructor also loaded all five under Wine. The binaries also
import the Microsoft Visual C++ runtime (`MSVCP140.dll`, `MSVCP140_1.dll`,
`VCRUNTIME140.dll`, and `VCRUNTIME140_1.dll`), these Windows system DLLs:
`ADVAPI32.dll`, `CRYPT32.dll`, `DNSAPI.dll`, `IPHLPAPI.dll`, `KERNEL32.dll`,
`MPR.dll`, `NETAPI32.dll`, `ole32.dll`, `SHELL32.dll`, `USER32.dll`,
`USERENV.dll`, `VERSION.dll`, `WINMM.dll`, and `WS2_32.dll`; and Universal CRT
API-set DLLs for `convert`, `environment`, `filesystem`, `heap`, `math`,
`runtime`, `stdio`, `string`, `time`, and `utility` (all
`api-ms-win-crt-*-l1-1-0.dll`). On Windows, provide the x64 Microsoft Visual
C++ 2015–2022 Redistributable and use the operating system's own system DLLs;
do not copy Windows system DLLs into the Insight directory. The API does not
import `python38.dll`.

The Qt platform plugin (`platforms/qwindows.dll`) was not part of the static
import closure and was not loaded during the constructor test. This does not
establish whether later SDK initialization needs it.

## Quick start

```python
from specim_eye_api import SpecimCameraAPI

cam = SpecimCameraAPI()      # finds eyeapi.dll one folder up (Insight root)
cam.connect()
h = cam.h(0)                 # first camera

print(cam.get(h, "Camera.Model"))
print(cam.get(h, "Camera.SerialNumber"))

# configure
cam.set_float(h, "Camera.ExposureTime", 4.0)   # µs (your pistachios capture used 4)
cam.set_int(h, "Camera.Count", 998)            # lines to record

# trigger acquisition and wait for it to finish
cam.start(h)                      # Acquisition.Start = 1
cam.wait_acquisition_done(h)      # poll Acquisition.Acquiring
cam.stop(h)                       # Acquisition.Stop = 1

# ... or one-liner:
cam.acquire(h, exposure_us=4.0, count=998)

# optional: real-time frame data in Python
cam.add_data_callback(lambda data, s1, s2: print(len(data), "bytes"))

# optional: retrieve the files the camera wrote (.raw/.hdr) via FTP
port = cam.ftp_port()

cam.disconnect()
```

## Triggering from outside (automation)

For a photogrammetry turntable the typical loop is:

```python
cam = SpecimCameraAPI(); cam.connect(); h = cam.h(0)

for i, angle in enumerate(range(0, 360, 5)):
    move_stage(angle)                      # your stage controller
    wait_for_stage_settling()
    cam.acquire(h, exposure_us=4.0, count=998)
    save_capture(i)                        # e.g. download via FTP (cam.ftp_port())
```

## Testing

```
python test_api.py          # read-only checks (model, firmware, settings, FTP port)
python test_api.py --acquire   # additionally triggers a 1000-line capture
```

## Troubleshooting

- **`eyeapi.dll not found`** — pass `SpecimCameraAPI(dll_dir=r"C:\Program Files\Specim\Specim Insight\1_4_0_83")`.
- **No handles / `EYE_OpenConnectedDevices` returns 0** — camera not connected /
  driver service not running. On the host machine check that Insight can see
  the camera (it will, if the driver is installed).
- **Feature read fails** — use `cam.is_readable(h, "Camera.ExposureTime")` to
  confirm the feature exists on this firmware; names can differ slightly
  between camera models (FX10 vs FX17 vs V10).
- **Port conflicts / multiple cameras** — camera N uses port 5000+N; check with
  `netstat -an | findstr 5001` while connected.
- Want to see what the SDK sends: `cam.set_logging(True)`.

## License

Provided for integration with Specim camera systems; `eyeapi.dll` and the
camera driver remain Specim property (see `../licenses/`).
