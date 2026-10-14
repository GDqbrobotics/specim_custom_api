"""
Specim Eye API — Python client for Specim hyperspectral cameras (FX10/FX17/...)
===============================================================================

This is the *verified* replacement for the early draft `specim_camera_api.py`.

How the camera is actually controlled (reverse-engineered from this exact
installation, Specim Insight 1.4.0.83):

  Python  --ctypes-->  eyeapi.dll  --JSON-RPC over TCP-->  Camera (Eye service)

* `eyeapi.dll` is the official Specim "Eye" SDK. Specim Insight itself uses it;
  it is NOT required to run Insight, the DLL talks to the camera directly
  (it embeds its own Qt event loop thread, see `s_qapp_loop` in the DLL).

* The wire protocol is JSON-RPC 2.0 over a TCP socket (qjsonrpc1.dll /
  QJsonRpcSocket). Methods look like:

      {"jsonrpc":"2.0","id":1,"method":"eye1.getFloat",
       "params":["Camera.ExposureTime"]}

  where the device number (1-based) is baked into the method name and the
  feature name ("Camera.ExposureTime") is the first JSON parameter.

* Port scheme (found in `s_eye_config` in eyeapi.dll, base port = 5000,
  and the connect code computes `port = 5001 + (deviceIndex - 1)`; the DLL
  even logs "Try open: <host> port 5001"):

      camera 1 (first/only camera)  ->  TCP port 5001
      camera 2                      ->  TCP port 5002
      ...

  (The old draft guessed port 5000 — that value only appears as the 5000 ms
  waitForConnected() timeout and the base constant.)

* Feature names (from the feature table embedded in insight.exe). The FX17
  ones relevant to acquisition:

      Camera.Model, Camera.SerialNumber        (string)
      Camera.ExposureTime                      (float, µs)
      Camera.FrameRate                         (float, Hz / lines per s)
      Camera.Count                             (int, lines/frames to record)
      Camera.LinesPerFrame                     (int)
      Camera.Image.Width / Camera.Image.Height (int)
      Camera.Wavelength.Start / .Stop          (float, nm)
      Camera.Binning.Spatial / .Spectral       (int)
      Camera.Trigger.Mode                      (trigger source)
      Camera.Shutter.IsOpen                    (bool)
      Acquisition.Start / Acquisition.Stop     (set to 1 to (un)start)
      Acquisition.Acquiring                    (bool, readable)
      Acquisition.Error                        (readable)
      Acquisition.FrameCounter                 (int)
      Camera.FrameQueue.Length                 (int)
      System.DeviceName / System.FirmwareVersion (string)
      System.Fileserver.Start / .Stop          (FTP access to camera files)

  `EYE_GetFtpPort()` returns the FTP port used to download the .raw/.hdr
  files the camera writes (the same files Insight stores in the dataset).

Usage
-----
    from specim_eye_api import SpecimCameraAPI

    cam = SpecimCameraAPI()          # finds eyeapi.dll next to this file
    cam.connect()                    # EYE_Load + EYE_OpenConnectedDevices
    h = cam.h(0)                     # handle of first camera (1-based inside)

    print(cam.get(h, "Camera.Model"))
    print(cam.get(h, "System.DeviceName"))

    # expose + record a line-scan image
    cam.set_float(h, "Camera.ExposureTime", 4.0)     # µs
    cam.set_int(h, "Camera.Count", 1000)             # lines to capture
    cam.start(h)
    cam.wait_acquisition_done(h)
    cam.stop(h)

    # real-time data stream into Python (optional)
    cam.add_data_callback(lambda data, s1, s2: print(len(data)))

    cam.disconnect()

Notes
-----
* Return codes: 0 == success. The C API uses int status codes (the SDK has
  no exported last-error getter); anything non-zero is treated as an error
  here. Call `cam.set_logging(True)` for the DLL's internal qDebug output.
* Callbacks run on the DLL's Qt thread. Keep the returned handle object if
  you need to remove the callback later.
* Handles are integers (1-based device ids). Pass a 0-based *index* to
  `cam.h(index)` or use the raw handle from `cam.handles`.
"""

from __future__ import annotations

import ctypes as ct
import os
import sys
import time

# ---------------------------------------------------------------------------
# ctypes callback prototypes (match the mangled C++ signatures exported by
# eyeapi.dll):
#
#   ?EYE_Add_Data_Callback@@YAH_JP6AHPEAE00PEAX@Z2@Z
#       uint32 (*cb)(uint8* data, size_t, size_t, void* user)
#   ?EYE_Add_Ascii_Callback@@YAH_JP6AHPEAD0PEAX@Z2@Z
#       uint32 (*cb)(char* data, size_t len, void* user)
#   ?EYE_Add_Feature_Callback@@YAH_JPEB_WP6AHPEAXPEA_W2@Z2@Z
#       uint32 (*cb)(const wchar_t* feature, ???, void* user)  (approx.)
# ---------------------------------------------------------------------------
_DataCallback = ct.CFUNCTYPE(
    ct.c_uint32, ct.POINTER(ct.c_ubyte), ct.c_size_t, ct.c_size_t, ct.c_void_p
)
_AsciiCallback = ct.CFUNCTYPE(
    ct.c_uint32, ct.c_char_p, ct.c_size_t, ct.c_void_p
)
_FeatureCallback = ct.CFUNCTYPE(
    ct.c_uint32, ct.c_wchar_p, ct.c_void_p, ct.c_void_p
)


class EyeError(RuntimeError):
    """Raised when an EYE_* call returns a non-zero status."""


# Feature name -> (type, description). Type is one of
# "float", "int", "string", "bool". From the feature table in insight.exe.
KNOWN_FEATURES = {
    "Camera.Model": ("string", "camera model name"),
    "Camera.SerialNumber": ("string", "camera serial number"),
    "Camera.ExposureTime": ("float", "integration/exposure time (µs)"),
    "Camera.ExposureTime.Auto": ("int", "auto exposure on/off"),
    "Camera.FrameRate": ("float", "line/frame rate (Hz)"),
    "Camera.Count": ("int", "number of lines/frames to record"),
    "Camera.LinesPerFrame": ("int", "lines per frame (area scan)"),
    "Camera.Image.Width": ("int", "image width (pixels)"),
    "Camera.Image.Height": ("int", "image height (lines)"),
    "Camera.Image.SizePixels": ("int", "image size in pixels"),
    "Camera.Image.SizeBytes": ("int", "image size in bytes"),
    "Camera.Image.Transformation": ("string", "image transformation"),
    "Camera.Sensor.Width": ("int", "sensor width"),
    "Camera.Sensor.Height": ("int", "sensor height"),
    "Camera.Pixel.Width": ("float", "pixel width (µm)"),
    "Camera.Pixel.Height": ("float", "pixel height (µm)"),
    "Camera.BitDepth": ("int", "bit depth"),
    "Camera.ByteDepth": ("int", "byte depth"),
    "Camera.MaxDataNumber": ("int", "max data number (DN)"),
    "Camera.BlackLevel": ("float", "black level"),
    "Camera.Gain": ("float", "gain"),
    "Camera.FWHM": ("float", "full width half maximum"),
    "Camera.Wavelength.Start": ("float", "start wavelength (nm)"),
    "Camera.Wavelength.Stop": ("float", "stop wavelength (nm)"),
    "Camera.Binning.Spatial": ("int", "spatial binning"),
    "Camera.Binning.Spectral": ("int", "spectral binning"),
    "Camera.Binning.Average": ("int", "average binning"),
    "Camera.Binning.HardwareModes": ("string", "available HW binning modes"),
    "Camera.Trigger.Mode": ("string", "trigger mode (e.g. Internal/External)"),
    "Camera.Dataout.Mode": ("string", "data output mode"),
    "Camera.Shutter.IsOpen": ("bool", "shutter open?"),
    "Camera.ShutterSweepTime": ("float", "shutter sweep time"),
    "Camera.CloseShutter": ("int", "close shutter (set 1)"),
    "Camera.OpenShutter": ("int", "open shutter (set 1)"),
    "Camera.IsConnected": ("bool", "camera link up?"),
    "Camera.LightMeter": ("float", "light meter value"),
    "Camera.FrameQueue.Length": ("int", "frames waiting in camera queue"),
    "Acquisition.Start": ("int", "set 1 to start acquisition"),
    "Acquisition.Stop": ("int", "set 1 to stop acquisition"),
    "Acquisition.Acquiring": ("bool", "acquisition running?"),
    "Acquisition.AcquisitionMode": ("string", "acquisition mode"),
    "Acquisition.Error": ("string", "last acquisition error"),
    "Acquisition.FrameCounter": ("int", "frames captured so far"),
    "Acquisition.FrameCounter.Reset": ("int", "set 1 to reset counter"),
    "System.DeviceName": ("string", "device name"),
    "System.FirmwareVersion": ("string", "firmware version"),
    "System.SerialNumber": ("string", "system serial"),
    "System.CopyrightInfo": ("string", "copyright info"),
    "System.DateAndTime": ("string", "camera date/time"),
    "System.NumScans": ("int", "number of scans"),
    "System.Fileserver.Start": ("int", "start FTP file server"),
    "System.Fileserver.Stop": ("int", "stop FTP file server"),
}


class SpecimCameraAPI:
    """ctypes wrapper around eyeapi.dll (the Specim Eye SDK)."""

    MAX_DEVICES = 8

    def __init__(self, dll_dir: str | None = None):
        """
        Args:
            dll_dir: folder containing eyeapi.dll. Defaults to the parent of
                     this file (i.e. the Specim Insight install folder, since
                     this API folder lives inside it).
        """
        if dll_dir is None:
            dll_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.dll_dir = os.path.abspath(dll_dir)
        dll_path = os.path.join(self.dll_dir, "eyeapi.dll")
        if not os.path.exists(dll_path):
            raise FileNotFoundError(
                f"eyeapi.dll not found in {self.dll_dir}. Pass dll_dir=... "
                "pointing to the Specim Insight installation folder."
            )

        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            # Needed so Qt5Core.dll / qjsonrpc1.dll next to eyeapi.dll resolve
            self._dll_directory_handle = os.add_dll_directory(self.dll_dir)
        elif sys.platform == "win32":
            # Python versions before 3.8 do not provide add_dll_directory().
            set_dll_directory = ct.windll.kernel32.SetDllDirectoryW
            set_dll_directory.argtypes = [ct.c_wchar_p]
            set_dll_directory.restype = ct.c_int
            if not set_dll_directory(self.dll_dir):
                raise ct.WinError()

        self.dll = ct.WinDLL(dll_path)
        self._bind_exports()
        self._setup_prototypes()

        self.handles: list[int] = []
        self._cb_refs: list = []          # keep ctypes callbacks alive
        self._user_refs: list = []        # keep user objects alive
        self._connected = False

    def _bind_exports(self) -> None:
        """Give the SDK's decorated C++ exports the names used by this wrapper."""
        exports = {
            "EYE_AddDataToFileSync": "?EYE_AddDataToFileSync@@YAH_JPEAD00@Z",
            "EYE_Add_Ascii_Callback": "?EYE_Add_Ascii_Callback@@YAH_JP6AHPEAD0PEAX@Z2@Z",
            "EYE_Add_Data_Callback": "?EYE_Add_Data_Callback@@YAH_JP6AHPEAE00PEAX@Z2@Z",
            "EYE_Add_Feature_Callback": "?EYE_Add_Feature_Callback@@YAH_JPEB_WP6AHPEAXPEA_W2@Z2@Z",
            "EYE_CheckConnectedDevicesChanged": "?EYE_CheckConnectedDevicesChanged@@YA_NXZ",
            "EYE_CloseFile": "?EYE_CloseFile@@YAH_J@Z",
            "EYE_GetBool": "?EYE_GetBool@@YAH_JPEB_WPEA_N@Z",
            "EYE_GetDeviceName": "?EYE_GetDeviceName@@YAHPEA_WHH@Z",
            "EYE_GetFloat": "?EYE_GetFloat@@YAH_JPEB_WPEAN@Z",
            "EYE_GetFloatMax": "?EYE_GetFloatMax@@YAH_JPEB_WPEAN@Z",
            "EYE_GetFloatMin": "?EYE_GetFloatMin@@YAH_JPEB_WPEAN@Z",
            "EYE_GetFtpPort": "?EYE_GetFtpPort@@YAHXZ",
            "EYE_GetHandleByIndex": "?EYE_GetHandleByIndex@@YAHPEA_JH@Z",
            "EYE_GetHandleByNameAndSubdevice": "?EYE_GetHandleByNameAndSubdevice@@YAHPEA_JPEA_WH@Z",
            "EYE_GetInt": "?EYE_GetInt@@YAH_JPEB_WPEA_J@Z",
            "EYE_GetIntMax": "?EYE_GetIntMax@@YAH_JPEB_WPEA_J@Z",
            "EYE_GetIntMin": "?EYE_GetIntMin@@YAH_JPEB_WPEA_J@Z",
            "EYE_GetString": "?EYE_GetString@@YAH_JPEB_WPEA_WH@Z",
            "EYE_GetStringMaxLength": "?EYE_GetStringMaxLength@@YAH_JPEB_WPEAH@Z",
            "EYE_IsReadable": "?EYE_IsReadable@@YAH_JPEB_WPEA_N@Z",
            "EYE_IsWriteable": "?EYE_IsWriteable@@YAH_JPEB_WPEA_N@Z",
            "EYE_Load": "?EYE_Load@@YAHXZ",
            "EYE_OpenConnectedDevices": "?EYE_OpenConnectedDevices@@YAHPEAH_N@Z",
            "EYE_OpenFile": "?EYE_OpenFile@@YAH_JPEAD@Z",
            "EYE_Opened": "?EYE_Opened@@YAHXZ",
            "EYE_Remove_Ascii_Callback": "?EYE_Remove_Ascii_Callback@@YAH_J@Z",
            "EYE_Remove_Data_Callback": "?EYE_Remove_Data_Callback@@YAH_J@Z",
            "EYE_SendCommand": "?EYE_SendCommand@@YAH_JPEB_W@Z",
            "EYE_SetBool": "?EYE_SetBool@@YAH_JPEB_W_N@Z",
            "EYE_SetFloat": "?EYE_SetFloat@@YAH_JPEB_WN@Z",
            "EYE_SetInt": "?EYE_SetInt@@YAH_JPEB_W0@Z",
            "EYE_SetLogging": "?EYE_SetLogging@@YAX_N@Z",
            "EYE_SetString": "?EYE_SetString@@YAH_JPEB_WPEA_W@Z",
            "EYE_Unload": "?EYE_Unload@@YAHXZ",
            "EYE_Wait": "?EYE_Wait@@YAH_JPEAEPEA_J20@Z",
        }
        for name, decorated_name in exports.items():
            try:
                function = getattr(self.dll, name)
            except AttributeError:
                function = self.dll[decorated_name]
            setattr(self.dll, name, function)

    # ------------------------------------------------------------------ setup
    def _setup_prototypes(self) -> None:
        d = self.dll
        d.EYE_Load.restype = ct.c_int
        d.EYE_Unload.restype = ct.c_int
        d.EYE_Opened.restype = ct.c_int
        d.EYE_SetLogging.argtypes = [ct.c_bool]
        d.EYE_SetLogging.restype = None

        d.EYE_OpenConnectedDevices.restype = ct.c_int
        d.EYE_OpenConnectedDevices.argtypes = [ct.POINTER(ct.c_uint32), ct.c_bool]
        d.EYE_CheckConnectedDevicesChanged.restype = ct.c_bool

        d.EYE_GetHandleByIndex.restype = ct.c_int
        d.EYE_GetHandleByIndex.argtypes = [ct.POINTER(ct.c_uint64), ct.c_uint32]
        d.EYE_GetHandleByNameAndSubdevice.restype = ct.c_int
        d.EYE_GetHandleByNameAndSubdevice.argtypes = [
            ct.POINTER(ct.c_uint64), ct.c_wchar_p, ct.c_uint32
        ]
        d.EYE_GetDeviceName.restype = ct.c_int
        d.EYE_GetDeviceName.argtypes = [ct.POINTER(ct.c_wchar), ct.c_uint32, ct.c_uint32]
        d.EYE_GetFtpPort.restype = ct.c_int

        for name, out in [
            ("EYE_GetBool", ct.c_uint8),
            ("EYE_GetFloat", ct.c_float),
            ("EYE_GetFloatMax", ct.c_float),
            ("EYE_GetFloatMin", ct.c_float),
            ("EYE_GetInt", ct.c_uint64),
            ("EYE_GetIntMax", ct.c_uint64),
            ("EYE_GetIntMin", ct.c_uint64),
            ("EYE_IsReadable", ct.c_uint8),
            ("EYE_IsWriteable", ct.c_uint8),
        ]:
            f = getattr(d, name)
            f.restype = ct.c_int
            f.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.POINTER(out)]

        d.EYE_GetString.restype = ct.c_int
        d.EYE_GetString.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.POINTER(ct.c_wchar), ct.c_uint32]
        d.EYE_GetStringMaxLength.restype = ct.c_int
        d.EYE_GetStringMaxLength.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.POINTER(ct.c_uint32)]

        d.EYE_SetBool.restype = ct.c_int
        d.EYE_SetBool.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.c_bool]
        d.EYE_SetFloat.restype = ct.c_int
        d.EYE_SetFloat.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.c_float]
        d.EYE_SetInt.restype = ct.c_int
        d.EYE_SetInt.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.c_uint64]
        d.EYE_SetString.restype = ct.c_int
        d.EYE_SetString.argtypes = [ct.c_uint64, ct.c_wchar_p, ct.c_wchar_p]

        d.EYE_SendCommand.restype = ct.c_int
        d.EYE_SendCommand.argtypes = [ct.c_uint64, ct.c_wchar_p]

        d.EYE_Add_Data_Callback.restype = ct.c_int
        d.EYE_Add_Data_Callback.argtypes = [ct.c_uint64, _DataCallback, ct.c_void_p]
        d.EYE_Remove_Data_Callback.restype = ct.c_int
        d.EYE_Remove_Data_Callback.argtypes = [ct.c_uint64]
        d.EYE_Add_Ascii_Callback.restype = ct.c_int
        d.EYE_Add_Ascii_Callback.argtypes = [ct.c_uint64, _AsciiCallback, ct.c_void_p]
        d.EYE_Remove_Ascii_Callback.restype = ct.c_int
        d.EYE_Remove_Ascii_Callback.argtypes = [ct.c_uint64]
        d.EYE_Add_Feature_Callback.restype = ct.c_int
        d.EYE_Add_Feature_Callback.argtypes = [ct.c_uint64, _FeatureCallback, ct.c_void_p]

        d.EYE_Wait.restype = ct.c_int
        d.EYE_Wait.argtypes = [ct.c_uint64, ct.POINTER(ct.c_ubyte), ct.POINTER(ct.c_size_t), ct.c_size_t]

        d.EYE_OpenFile.restype = ct.c_int
        d.EYE_OpenFile.argtypes = [ct.c_uint64, ct.c_char_p]
        d.EYE_AddDataToFileSync.restype = ct.c_int
        d.EYE_AddDataToFileSync.argtypes = [ct.c_uint64, ct.c_char_p, ct.c_size_t, ct.c_size_t]
        d.EYE_CloseFile.restype = ct.c_int
        d.EYE_CloseFile.argtypes = [ct.c_uint64]

    # ------------------------------------------------------------- lifecycle
    def set_logging(self, enabled: bool = True) -> None:
        """Enable the SDK's internal qDebug() logging (to the console)."""
        self.dll.EYE_SetLogging(bool(enabled))

    def connect(self, max_devices: int = MAX_DEVICES) -> list[int]:
        """Initialize the SDK and open all connected cameras.

        Returns the list of device handles (int, 1-based). Raises EyeError if
        no camera is found.
        """
        d = self.dll
        rc = d.EYE_Load()
        if rc not in (0, None):
            raise EyeError(f"EYE_Load failed ({rc})")

        buf = (ct.c_uint32 * max_devices)()
        n = d.EYE_OpenConnectedDevices(buf, False)
        # The DLL stores the number of connected devices in *handles (verified
        # from openConnectedDevices@QEyeSystem, which returns the device count).
        count = n if n > 0 else 0
        self.handles = []
        for i in range(1, count + 1):
            h = ct.c_uint64()
            rc = d.EYE_GetHandleByIndex(ct.byref(h), i)
            self.handles.append(int(h.value) if rc == 0 and h.value else i)
        self._connected = bool(self.handles)
        return self.handles

    def disconnect(self) -> None:
        self.dll.EYE_Unload()
        self.handles = []
        self._cb_refs.clear()
        self._user_refs.clear()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def h(self, index: int = 0) -> int:
        """Return the handle for the 0-based device index."""
        if not (0 <= index < len(self.handles)):
            raise EyeError(
                f"device index {index} out of range "
                f"(connected: {len(self.handles)}). Call connect() first."
            )
        return self.handles[index]

    def device_name(self, handle: int | None = None, max_len: int = 256) -> str:
        h = self._h(handle)
        buf = (ct.c_wchar * max_len)()
        rc = self.dll.EYE_GetDeviceName(buf, max_len, ct.c_uint32(self._index_of(h)))
        if rc != 0:
            raise EyeError(f"EYE_GetDeviceName failed ({rc})")
        return buf.value or ""

    def ftp_port(self) -> int:
        port = self.dll.EYE_GetFtpPort()
        if port <= 0:
            raise EyeError(f"EYE_GetFtpPort failed ({port})")
        return port

    def devices_changed(self) -> bool:
        return bool(self.dll.EYE_CheckConnectedDevicesChanged())

    # ------------------------------------------------------------- get / set
    def _h(self, handle: int | None) -> int:
        return self.handles[0] if handle is None else int(handle)

    def _index_of(self, handle: int) -> int:
        """1-based device index for a handle (handles are 1-based ids)."""
        if int(handle) in self.handles:
            return self.handles.index(int(handle)) + 1
        return 1

    def _wf(self, feature: str) -> str:
        return feature

    def is_readable(self, handle: int | None, feature: str) -> bool:
        out = ct.c_uint8()
        rc = self.dll.EYE_IsReadable(self._h(handle), self._wf(feature), ct.byref(out))
        return rc == 0 and bool(out.value)

    def is_writeable(self, handle: int | None, feature: str) -> bool:
        out = ct.c_uint8()
        rc = self.dll.EYE_IsWriteable(self._h(handle), self._wf(feature), ct.byref(out))
        return rc == 0 and bool(out.value)

    def get(self, handle: int | None, feature: str):
        """Read a feature, auto-detecting its type (float/int/string/bool)."""
        h = self._h(handle)
        f = self._wf(feature)
        d = self.dll

        out = ct.c_float()
        if d.EYE_GetFloat(h, f, ct.byref(out)) == 0:
            return float(out.value)

        out = ct.c_uint64()
        if d.EYE_GetInt(h, f, ct.byref(out)) == 0:
            v = out.value
            return v - (1 << 64) if v >= (1 << 63) else v

        buf = (ct.c_wchar * 1024)()
        if d.EYE_GetString(h, f, buf, 1024) == 0:
            return buf.value

        out = ct.c_uint8()
        if d.EYE_GetBool(h, f, ct.byref(out)) == 0:
            return bool(out.value)

        raise EyeError(
            f"get({feature!r}) failed — unknown feature or type "
            "(is the camera connected? try is_readable / KNOWN_FEATURES)"
        )

    def set(self, handle: int | None, feature: str, value) -> None:
        """Write a feature, picking the C type from the Python type."""
        h = self._h(handle)
        f = self._wf(feature)
        if isinstance(value, bool):
            rc = self.dll.EYE_SetBool(h, f, value)
        elif isinstance(value, int):
            rc = self.dll.EYE_SetInt(h, f, value & 0xFFFFFFFFFFFFFFFF)
        elif isinstance(value, float):
            rc = self.dll.EYE_SetFloat(h, f, value)
        elif isinstance(value, str):
            rc = self.dll.EYE_SetString(h, f, value)
        else:
            raise TypeError(f"unsupported value type: {type(value)!r}")
        if rc != 0:
            raise EyeError(f"set({feature!r}, {value!r}) failed ({rc})")

    # convenience typed setters
    def set_float(self, handle, feature, value): self.set(handle, feature, float(value))
    def set_int(self, handle, feature, value): self.set(handle, feature, int(value))
    def set_string(self, handle, feature, value): self.set(handle, feature, str(value))
    def set_bool(self, handle, feature, value): self.set(handle, feature, bool(value))

    def get_float(self, handle, feature):
        out = ct.c_float()
        rc = self.dll.EYE_GetFloat(self._h(handle), self._wf(feature), ct.byref(out))
        if rc != 0: raise EyeError(f"get_float({feature!r}) failed ({rc})")
        return float(out.value)

    def get_int(self, handle, feature):
        out = ct.c_uint64()
        rc = self.dll.EYE_GetInt(self._h(handle), self._wf(feature), ct.byref(out))
        if rc != 0: raise EyeError(f"get_int({feature!r}) failed ({rc})")
        v = out.value
        return v - (1 << 64) if v >= (1 << 63) else v

    def get_string(self, handle, feature, max_len: int = 1024):
        buf = (ct.c_wchar * max_len)()
        rc = self.dll.EYE_GetString(self._h(handle), self._wf(feature), buf, max_len)
        if rc != 0: raise EyeError(f"get_string({feature!r}) failed ({rc})")
        return buf.value

    def get_bool(self, handle, feature):
        out = ct.c_uint8()
        rc = self.dll.EYE_GetBool(self._h(handle), self._wf(feature), ct.byref(out))
        if rc != 0: raise EyeError(f"get_bool({feature!r}) failed ({rc})")
        return bool(out.value)

    # ------------------------------------------------------------ acquisition
    def start(self, handle: int | None = None) -> None:
        """Start the acquisition (Acquisition.Start = 1)."""
        self.set_int(handle, "Acquisition.Start", 1)

    def stop(self, handle: int | None = None) -> None:
        """Stop the acquisition (Acquisition.Stop = 1)."""
        self.set_int(handle, "Acquisition.Stop", 1)

    @property
    def acquiring(self) -> bool:
        return self.get_bool(None, "Acquisition.Acquiring")

    def wait_acquisition_done(
        self,
        handle: int | None = None,
        timeout: float = 120.0,
        poll: float = 0.2,
    ) -> None:
        """Block until Acquisition.Acquiring becomes False (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.get_bool(handle, "Acquisition.Acquiring"):
                return
            time.sleep(poll)
        raise TimeoutError(f"acquisition still running after {timeout}s")

    def acquire(
        self,
        handle: int | None = None,
        exposure_us: float | None = None,
        count: int | None = None,
        wait: bool = True,
        timeout: float = 120.0,
    ) -> None:
        """One-shot convenience: configure (optional), start, wait, stop."""
        if exposure_us is not None:
            self.set_float(handle, "Camera.ExposureTime", exposure_us)
        if count is not None:
            self.set_int(handle, "Camera.Count", count)
        self.start(handle)
        if wait:
            self.wait_acquisition_done(handle, timeout=timeout)
        self.stop(handle)

    # -------------------------------------------------------------- callbacks
    def add_data_callback(self, callback, handle: int | None = None) -> None:
        """
        Register a real-time frame data callback.

        callback(data: bytes, size_a: int, size_b: int)  — the two sizes are
        passed by the firmware (typically total bytes and frame/line count).
        Runs on the SDK's Qt thread.
        """
        h = self._h(handle)
        self._user_refs.append(callback)  # keep alive

        def bridge(ptr, a, b, _user):
            if ptr:
                data = bytes(ptr[:a]) if a else b""
            try:
                callback(data, a, b)
            except Exception as exc:  # never raise into C
                print(f"[specim_eye_api] data callback error: {exc}")
            return 0

        f = _DataCallback(bridge)
        self._cb_refs.append(f)
        rc = self.dll.EYE_Add_Data_Callback(h, f, None)
        if rc != 0:
            raise EyeError(f"EYE_Add_Data_Callback failed ({rc})")

    def remove_data_callbacks(self, handle: int | None = None) -> None:
        rc = self.dll.EYE_Remove_Data_Callback(self._h(handle))
        if rc != 0:
            raise EyeError(f"EYE_Remove_Data_Callback failed ({rc})")

    def add_ascii_callback(self, callback, handle: int | None = None) -> None:
        """callback(text: str) — notifications from the camera ASCII stream."""
        h = self._h(handle)

        def bridge(ptr, length, _user):
            try:
                if ptr and length:
                    text = bytes(ptr[:length]).decode("ascii", "replace")
                    callback(text)
            except Exception as exc:
                print(f"[specim_eye_api] ascii callback error: {exc}")
            return 0

        f = _AsciiCallback(bridge)
        self._cb_refs.append(f)
        rc = self.dll.EYE_Add_Ascii_Callback(h, f, None)
        if rc != 0:
            raise EyeError(f"EYE_Add_Ascii_Callback failed ({rc})")

    def send_command(self, command: str, handle: int | None = None) -> None:
        """Send a raw ASCII command to the camera (e.g. legacy FX commands)."""
        rc = self.dll.EYE_SendCommand(self._h(handle), self._wf(command))
        if rc != 0:
            raise EyeError(f"EYE_SendCommand({command!r}) failed ({rc})")

    # ------------------------------------------------------------------- wait
    def wait_data(self, size: int = 1 << 20, timeout_ms: int = 5000,
                  handle: int | None = None) -> bytes:
        """Blocking wait for one chunk of camera data (EYE_Wait)."""
        h = self._h(handle)
        buf = (ct.c_ubyte * size)()
        n = ct.c_size_t(0)
        rc = self.dll.EYE_Wait(h, buf, ct.byref(n), timeout_ms)
        if rc != 0:
            raise EyeError(f"EYE_Wait failed ({rc})")
        return bytes(buf[:n.value])

    # ---------------------------------------------------------- file to disk
    def open_file(self, path: str, handle: int | None = None) -> None:
        """Tell the SDK to stream incoming frame data into `path`."""
        rc = self.dll.EYE_OpenFile(self._h(handle), os.fsencode(path))
        if rc != 0:
            raise EyeError(f"EYE_OpenFile({path!r}) failed ({rc})")

    def close_file(self, handle: int | None = None) -> None:
        rc = self.dll.EYE_CloseFile(self._h(handle))
        if rc != 0:
            raise EyeError(f"EYE_CloseFile failed ({rc})")

    # ------------------------------------------------------------- context mg
    def __enter__(self) -> "SpecimCameraAPI":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()


# ---------------------------------------------------------------------------
# Raw JSON-RPC fallback (no DLL)
# ---------------------------------------------------------------------------
class EyeJsonRpc:
    """
    Minimal raw JSON-RPC 2.0 client for the camera's Eye service, for cases
    where loading eyeapi.dll is not an option.

    The camera (device 1 = first camera) listens on TCP 127.0.0.1:5001
    (port = 5000 + device_index, 1-based). Messages are plain JSON objects,
    e.g.:

        request:  {"jsonrpc":"2.0","id":1,"method":"eye1.getFloat",
                   "params":["Camera.ExposureTime"]}
        response: {"jsonrpc":"2.0","id":1,"result":4.0}

    NOTE: this is a best-effort re-implementation of the wire protocol
    (verified from eyeapi.dll: QJsonRpcSocket, port constants, method name
    format). The ctypes API above is the supported path.
    """

    def __init__(self, host: str = "127.0.0.1", device: int = 1, timeout: float = 10.0):
        import socket
        self.host = host
        self.device = device          # 1-based
        self.port = 5000 + device
        self.timeout = timeout
        self._id = 0
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        import socket
        self._sock = socket.create_connection((self.host, self.port), self.timeout)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def _read_message(self) -> dict:
        """Read one JSON-RPC message (scan for balanced braces)."""
        import json
        depth = 0
        in_str = False
        esc = False
        start = None
        buf = b""
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("camera closed the RPC socket")
            buf += chunk
            i = 0
            while i < len(buf):
                c = chr(buf[i])
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == "{":
                        if depth == 0:
                            start = i
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0 and start is not None:
                            return json.loads(buf[start:i + 1].decode("utf-8"))
                i += 1

    def call(self, method: str, *params) -> "object":
        import json
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params:
            msg["params"] = list(params)
        self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        resp = self._read_message()
        if "error" in resp and resp["error"]:
            raise EyeError(f"RPC {method}: {resp['error']}")
        return resp.get("result")

    # feature helpers (method name embeds the 1-based device index)
    def get_float(self, feature: str):
        return self.call(f"eye{self.device}.getFloat", feature)

    def get_int(self, feature: str):
        return self.call(f"eye{self.device}.getInt", feature)

    def get_string(self, feature: str):
        return self.call(f"eye{self.device}.getString", feature)

    def get_bool(self, feature: str):
        return self.call(f"eye{self.device}.getBool", feature)

    def set_float(self, feature: str, value: float):
        return self.call(f"eye{self.device}.setFloat", feature, float(value))

    def set_int(self, feature: str, value: int):
        return self.call(f"eye{self.device}.setInt", feature, int(value))

    def set_string(self, feature: str, value: str):
        return self.call(f"eye{self.device}.setString", feature, str(value))

    def set_bool(self, feature: str, value: bool):
        return self.call(f"eye{self.device}.setBool", feature, bool(value))


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Specim Eye API — quick check")
    print("=" * 50)
    try:
        with SpecimCameraAPI() as cam:
            for h in cam.handles:
                print(f"handle {h}: {cam.device_name(h)!r}")
                try:
                    print("  model:", cam.get(h, "Camera.Model"))
                    print("  device:", cam.get(h, "System.DeviceName"))
                    print("  firmware:", cam.get(h, "System.FirmwareVersion"))
                    print("  exposure (µs):", cam.get(h, "Camera.ExposureTime"))
                    print("  FTP port:", cam.ftp_port())
                except EyeError as e:
                    print("  (feature read failed — camera not fully ready?)", e)
    except FileNotFoundError as e:
        print("ERROR:", e)
    except EyeError as e:
        print("ERROR:", e, "(is the camera connected and the driver installed?)")
