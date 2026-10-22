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
# "float", "int", "string", "bool".
# Extracted from the feature table in SpecimIQ Studio's eyeapi.dll (IQ SDK).
KNOWN_FEATURES = {
    # --- identity / connectivity -------------------------------------------
    "Camera.Model": ("string", "camera model name"),
    "Camera.SerialNumber": ("string", "camera serial number"),
    "Camera.IsConnected": ("bool", "camera link up?"),
    "Camera.Setup": ("string", "camera setup string"),
    "Camera.Resolution": ("string", "current resolution"),
    "Camera.BaudRate": ("int", "serial/ASCII baud rate"),
    "Camera.LinesPerFrame": ("int", "lines per frame (area scan)"),
    "Sensor.SerialNumber": ("string", "sensor serial number"),
    "Sensor.Detector.Type": ("string", "detector type"),
    # --- image -------------------------------------------------------------
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
    "Camera.LightMeter": ("float", "light meter value"),
    "Camera.Wavelength.Start": ("float", "start wavelength (nm)"),
    "Camera.Wavelength.Stop": ("float", "stop wavelength (nm)"),
    "Camera.Binning.Spatial": ("int", "spatial binning"),
    "Camera.Binning.Spectral": ("int", "spectral binning"),
    "Camera.Binning.Average": ("int", "average binning"),
    "Camera.Binning.HardwareModes": ("string", "available HW binning modes"),
    "Camera.MROI.Enable": ("int", "multi ROI enable"),
    "Camera.MROI.Clear": ("int", "clear MROI settings"),
    "Camera.MROI.Mode": ("string", "MROI mode"),
    "Camera.MROI.Regions": ("int", "MROI region count"),
    "Camera.MROI.Left": ("int", "MROI left"),
    "Camera.MROI.Top": ("int", "MROI top"),
    "Camera.MROI.Width": ("int", "MROI width"),
    "Camera.MROI.Height": ("int", "MROI height"),
    # --- shutter / trigger / acquisition -----------------------------------
    "Camera.Shutter.IsOpen": ("bool", "shutter open?"),
    "Camera.Shutter.IsConnected": ("bool", "shutter connected?"),
    "Camera.ShutterSweepTime": ("float", "shutter sweep time"),
    "Camera.CloseShutter": ("int", "close shutter (set 1)"),
    "Camera.OpenShutter": ("int", "open shutter (set 1)"),
    "Camera.Trigger.Mode": ("string", "trigger mode (e.g. Internal/External)"),
    "Camera.Dataout.Mode": ("string", "data output mode"),
    "Camera.FrameQueue.Length": ("int", "frames waiting in camera queue"),
    "Camera.Count": ("int", "number of frames to record"),
    "Camera.ExposureTime": ("float", "integration/exposure time (µs)"),
    "Camera.ExposureTime.Auto": ("int", "auto exposure on/off"),
    "Camera.FrameRate": ("float", "frame rate (Hz)"),
    "Acquisition.Start": ("int", "set 1 to start acquisition"),
    "Acquisition.Stop": ("int", "set 1 to stop acquisition"),
    "Acquisition.Acquiring": ("bool", "acquisition running?"),
    "Acquisition.AcquisitionMode": ("string", "acquisition mode"),
    "Acquisition.Error": ("string", "last acquisition error"),
    "Acquisition.FrameCounter": ("int", "frames captured so far"),
    "Acquisition.FrameCounter.Reset": ("int", "set 1 to reset counter"),
    "AcquisitionStart.TriggerMode": ("string", "trigger at acquisition start"),
    "AcquisitionEnd.TriggerMode": ("string", "trigger at acquisition end"),
    "FrameStart.TriggerMode": ("string", "frame start trigger"),
    "FrameBurstStart.TriggerMode": ("string", "burst start trigger"),
    "FrameBurstEnd.TriggerMode": ("string", "burst end trigger"),
    # --- processing ---------------------------------------------------------
    "Average.Value": ("int", "average (boxcar) length"),
    "AberCorrection.Enable": ("int", "aberration correction enable"),
    "AberCorrection.LoadFromFlash": ("int", "load aber correction from flash"),
    "BadPixelCorrection.Enable": ("int", "bad pixel correction enable"),
    "BadPixelCorrection.Calculate": ("int", "calculate bad pixel map"),
    "BadPixelCorrection.SaveToFlash": ("int", "save bad pixel map"),
    "BadPixelCorrection.HighThreshold": ("int", "bad pixel high threshold"),
    "BadPixelCorrection.LowThreshold": ("int", "bad pixel low threshold"),
    "RowBlanking.Enable": ("int", "row blanking enable"),
    "RowId.Enable": ("int", "row ID enable"),
    "MROI.Enable": ("int", "MROI enable (legacy)"),
    "MROI.Htot": ("int", "MROI H total (legacy)"),
    "MROI.Index": ("int", "MROI index (legacy)"),
    "Camera.LoadAberrationCorrection": ("int", "load aberration correction"),
    "Camera.LoadAberrationCorrectionProgress": ("int", "aberration load progress"),
    # --- system / storage ----------------------------------------------------
    "System.DeviceName": ("string", "device name"),
    "System.FirmwareVersion": ("string", "firmware version"),
    "System.SerialNumber": ("string", "system serial"),
    "System.CopyrightInfo": ("string", "copyright info"),
    "System.DateAndTime": ("string", "camera date/time"),
    "System.TimeAutomatic": ("int", "automatic time sync"),
    "System.NumScans": ("int", "number of scans"),
    "System.Fileserver.Start": ("int", "start FTP file server"),
    "System.Fileserver.Stop": ("int", "stop FTP file server"),
    "System.FreeSpace": ("string", "free space on card"),
    "System.FreeSpaceInCaptures": ("string", "free space in capture area"),
    "System.FormatCard": ("int", "format SD card (set 1)"),
    "System.FactoryReset": ("int", "factory reset (set 1)"),
    "System.CheckCalibration": ("int", "check calibration"),
    "System.CalibrationCheckInUse": ("int", "calibration check in use"),
    "System.ResetCalibrationData": ("int", "reset calibration data"),
    "System.ProfileName": ("string", "active profile name"),
    "System.PowerSavingOn": ("int", "power saving on"),
    "System.SelfDiagnostic": ("string", "self diagnostic"),
    "System.GpsActive": ("int", "GPS active"),
    "System.BatteryCapacity": ("int", "battery capacity"),
    # --- UI / display / network (IQ specific) --------------------------------
    "System.DisplayOn": ("int", "device display on"),
    "System.DisplayTimeout": ("int", "display timeout"),
    "System.UiPause.Start": ("int", "pause UI (set 1)"),
    "System.UiPause.Continue": ("int", "continue UI"),
    "System.UiPause.Stop": ("int", "stop UI pause"),
    "System.SystemSoundsOn": ("int", "system sounds"),
    "System.WlanOn": ("int", "WiFi on"),
    "System.WlanDhcpClient": ("int", "WiFi DHCP client"),
    "System.WlanDhcpServer": ("int", "WiFi DHCP server"),
    "System.WlanStaticIp": ("string", "WiFi static IP"),
    "System.Remote.Enabled": ("int", "remote access enabled"),
    "System.Remote.Password": ("string", "remote password"),
    "System.RemoteModeOn": ("int", "remote mode on"),
    "System.TimeLapseOn": ("int", "time lapse on"),
    "System.TimeLapseInterval": ("int", "time lapse interval"),
    "System.TimeLapseOccurrence": ("int", "time lapse occurrences"),
    # --- ASCII connection (legacy PX protocol) -------------------------------
    "AsciiConnection.Start": ("int", "start ASCII connection"),
    "AsciiConnection.Stop": ("int", "stop ASCII connection"),
    "AsciiConnection.SwitchToAcquisition": ("int", "switch ASCII to acquisition"),
    "Camera.AsciiConnection.Start": ("int", "start ASCII connection (camera scope)"),
    "Camera.AsciiConnection.Stop": ("int", "stop ASCII connection (camera scope)"),
}


class SpecimCameraAPI:
    """ctypes wrapper around eyeapi.dll (the Specim Eye SDK)."""

    MAX_DEVICES = 8

    def __init__(self, dll_dir: str | None = None):
        """
        Args:
            dll_dir: folder containing eyeapi.dll. Defaults to the folder libs in this directory.
        """
        if dll_dir is None:
            dll_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
        self.dll_dir = os.path.abspath(dll_dir)
        dll_path = os.path.join(self.dll_dir, "eyeapi2.dll")
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

        self._qapp = None
        self.dll = ct.WinDLL(dll_path)
        self._install_qapp()   # must happen before the first EYE_* call
        self._bind_exports()
        self._setup_prototypes()

        self.handles: list[int] = []
        self._cb_refs: list = []          # keep ctypes callbacks alive
        self._user_refs: list = []        # keep user objects alive
        self._connected = False

    def _install_qapp(self) -> None:
        """Create a QCoreApplication on the calling thread (main thread).

        Why: the SDK's RPC path (TCP connect, testAlive, response handling)
        runs on the *calling* thread and uses QEventLoop/QTimer. Without a
        QCoreApplication on that thread, QEventLoop::exec() returns
        immediately and timers never fire, so the first RPC hangs forever
        (you see "QEventLoop: Cannot be used without QApplication" and
        "QObject::startTimer: Timers can only be used with threads started
        with QThread" with --debug).

        The SDK has a worker std::thread that creates its own QCoreApplication
        only when QCoreApplication::self is null; creating ours first makes
        that worker skip its own instance (Qt allows one QCoreApplication per
        process) and lets all Qt objects live on the main thread, exactly like
        inside SpecimIQ Studio (a Qt app). Must be called on the same thread
        that will call the EYE_* functions.
        """
        qtcore_path = os.path.join(self.dll_dir, "Qt5Core.dll")
        if not os.path.exists(qtcore_path) or not hasattr(ct, "windll"):
            return  # no Windows Qt5Core here; rely on the SDK's own fallback
        try:
            qtcore = ct.CDLL(qtcore_path)
            # Locate QCoreApplication::self inside *this* loaded copy of
            # Qt5Core (the very handle we loaded, not a same-named module).
            k32 = ct.windll.kernel32
            k32.GetProcAddress.restype = ct.c_void_p
            k32.GetProcAddress.argtypes = [ct.c_void_p, ct.c_char_p]
            self_var = k32.GetProcAddress(
                qtcore._handle, b"?self@QCoreApplication@@0PEAV1@EA")
            if not self_var:
                return  # not exported; rely on the SDK's worker-thread app
            # If a QCoreApplication already exists (previous instance or a
            # host Qt app), do NOT create a second one (Qt allows one per
            # process): the exported static QCoreApplication::self holds it.
            if ct.c_void_p.from_address(self_var).value:
                print("[specim] QCoreApplication already exists; reusing it")
                self._qapp = (qtcore, None, 1, None)
                return
            ctor = qtcore["??0QCoreApplication@@QEAA@AEAHPEAPEADH@Z"]
            ctor.restype = ct.c_void_p
            ctor.argtypes = [ct.POINTER(ct.c_int),
                             ct.POINTER(ct.c_char_p), ct.c_uint]
            argc = ct.c_int(1)
            argv = (ct.c_char_p * 2)(b"specim-custom-api", None)
            app_ptr = ctor(ct.byref(argc), ct.byref(argv), 0)
            if app_ptr:
                # keep references alive for the lifetime of this object:
                self._qapp = (qtcore, app_ptr, argc, argv)
                print(f"[specim] QCoreApplication created on main thread "
                      f"({app_ptr:#x})")
            else:
                print("[specim] QCoreApplication ctor returned null; "
                      "continuing without")
        except (OSError, AttributeError, ValueError) as e:
            print(f"[specim] QApp setup skipped ({type(e).__name__}: {e})")
            self._qapp = None

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

    def connect(
        self,
        max_devices: int = MAX_DEVICES,
        timeout: float = 20.0,
        poll: float = 0.5,
    ) -> list[int]:
        """Initialize the SDK, run discovery and open all connected cameras.

        SpecimIQ specifics (verified from the IQ eyeapi.dll):
        * discovery is an *asynchronous* global UDP listener (port 54546,
          multicast ff08::1). It is started by the first
          EYE_OpenConnectedDevices(..., True) call.
        * subsequent calls return 1 immediately and store the number of
          opened devices in the out-parameter.
        So we start discovery, then poll until the device count is > 0 or
        `timeout` seconds elapse.

        Returns the list of device handles. Raises EyeError if no camera
        is found within the timeout.
        """
        d = self.dll
        rc = d.EYE_Load()
        if rc not in (0, None):
            raise EyeError(f"EYE_Load failed ({rc})")

        buf = (ct.c_uint32 * max_devices)()
        deadline = time.time() + timeout
        count = 0
        # First call starts the async discovery listener.
        d.EYE_OpenConnectedDevices(buf, True)
        while time.time() < deadline:
            # Re-calling returns 1 (singleton exists) and refreshes *buf with
            # the number of opened (discovered + connected) devices.
            n = d.EYE_OpenConnectedDevices(buf, True)
            count = int(buf[0]) if n > 0 else 0
            if count > 0:
                break
            time.sleep(poll)

        if count == 0:
            raise EyeError(
                f"no camera found after {timeout:.0f}s of discovery "
                "(is the camera powered on and connected via USB/WiFi?)"
            )

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
def discover_cameras(timeout: float = 5.0, port: int = 54546) -> list[dict]:
    """
    Pure-Python SpecimIQ discovery (no DLL needed).

    The IQ SDK listens on UDP 54546 (multicast group ff08::1) and receives
    JSON datagrams from cameras, e.g.:

        {"type":"iq","services":["rpc",...],"addresses":["ADDR:192.168.7.2",
         "MAC:..","IPV6ADDR:fe80::.."], ...}

    Returns a list of parsed datagrams (dicts) received within `timeout`.
    Use the first 'addresses' entry (ADDR:...) as the camera's IPv4 address
    for the JSON-RPC connection on TCP port 5001 + (device_index - 1).
    """
    import json
    import socket
    results: list[dict] = []
    socks = []

    # IPv4 socket (covers IPv4 announcements / Windows dual-stack delivery)
    try:
        s4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s4.bind(("", port))
        try:
            mreq = socket.inet_aton("239.255.255.251") + socket.inet_aton("0.0.0.0")
            s4.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
        socks.append(s4)
    except OSError:
        pass

    # IPv6 socket: cameras announce on the link-local all-nodes group
    # ff08::1 (verified in eyeapi.dll's DiscoveryListener), which an IPv4
    # socket does not reliably receive on Windows.
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s6.bind(("::", port, 0, 0))
        try:
            group = socket.inet_pton(socket.AF_INET6, "ff08::1")
            # join on all interfaces (scope id 0 = default on most OSes)
            mreq6 = group + socket.pack("=I", 0)
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq6)
        except (OSError, AttributeError):
            pass
        socks.append(s6)
    except OSError:
        pass

    if not socks:
        return results
    for s in socks:
        s.settimeout(0.2)
    end = time.time() + timeout
    while time.time() < end:
        for s in socks:
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                s.close()
                socks.remove(s)
                break
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                msg["_from"] = addr[0]
                results.append(msg)
    for s in socks:
        s.close()
    return results


def probe_camera(host: str, listen_secs: float = 8.0, port: int = 54546,
                 ports=range(5000, 5021)) -> None:
    """
    Diagnostics for "connection refused" on TCP 5001.

    1. Listens on UDP 54546 (multicast ff08::1, like the SDK) for a few
       seconds and prints any camera announcement — proves the camera is
       alive on the network and tells you its *current* IP.
    2. TCP-probes 5000..5020 on `host` — tells you whether the camera is
       listening on the RPC port at all right now.
    3. If 5001 is open, sends eye1.testAlive and prints the answer.

    Usage:  python test_api.py --probe 169.254.222.101
    """
    import json
    import socket

    print(f"[probe] 1/3 listening for camera announcements on UDP {port} "
          f"for {listen_secs:.0f}s (you should hear from the camera even if "
          f"TCP is closed)...")
    heard = discover_cameras(timeout=listen_secs, port=port)
    if heard:
        for msg in heard:
            print(f"[probe]   announcement from {msg.get('_from')}: {msg}")
    else:
        print("[probe]   no announcements heard — camera not on this LAN, "
              "USB link down, or Windows firewall blocking UDP 54546")

    print(f"[probe] 2/3 TCP-probing {host} ports {list(ports)}...")
    open_ports = []
    for p in ports:
        try:
            s = socket.create_connection((host, p), timeout=1.0)
            open_ports.append(p)
            print(f"[probe]   port {p}: OPEN")
            if p == 5001:
                try:
                    s.sendall(json.dumps({"jsonrpc": "2.0", "id": 1,
                                          "method": "eye1.testAlive"}
                                         .encode()) + b"\n")
                    s.settimeout(3.0)
                    buf = b""
                    while b"\n" not in buf:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    print(f"[probe]   testAlive reply: {buf.decode('utf-8', 'replace')!r}")
                except OSError as e:
                    print(f"[probe]   testAlive exchange failed: {e}")
            s.close()
        except OSError:
            print(f"[probe]   port {p}: closed/unreachable")

    if open_ports:
        print(f"[probe] open ports: {open_ports}")
    else:
        print("[probe] no ports open. If the camera IS announcing on UDP, "
              "its RPC service may be off (camera state, or it only opens "
              "5001 while a client session is active). Try: power-cycle the "
              "camera, ensure SpecimIQ Studio is closed, re-plug USB, wait "
              "~10s, then run this again.")


class EyeJsonRpc:
    """
    Minimal raw JSON-RPC 2.0 client for the camera's Eye service, for cases
    where loading eyeapi.dll is not an option.

    The camera (device N) listens on TCP <camera-ip>:5000+N (1-based),
    i.e. the first camera is on port 5001. The camera IP comes from UDP
    discovery (see discover_cameras()): 192.168.7.2 for a USB-attached
    camera (usb0 virtual interface), or the WiFi/LAN address otherwise.
    Messages are plain JSON objects, e.g.:

        request:  {"jsonrpc":"2.0","id":1,"method":"eye1.getFloat",
                   "params":["Camera.ExposureTime"]}
        response: {"jsonrpc":"2.0","id":1,"result":4.0}

    NOTE: this is a best-effort re-implementation of the wire protocol
    (verified from the IQ eyeapi.dll: QJsonRpcSocket, port arithmetic
    5000+index, method name format eye%1.*). The ctypes API above is the
    supported path.
    """

    def __init__(self, host: str = "192.168.7.2", device: int = 1, timeout: float = 10.0):
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
