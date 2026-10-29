#!/usr/bin/env python3
"""
SpecimIQ Diagnostic — single-file, no dependencies beyond the repo.

Run on the Windows PC with the camera connected via USB:
    python diagnose.py

It will:
  1. Connect via eyeapi2.dll (QApp fix included)
  2. Enumerate all 5 RPC devices and print their names
  3. Locate the 'photonfocus' (system) device
  4. Check Remote mode status (RemoteModeOn / Remote.Enabled / Remote.Password)
  5. If Remote is ON, attempt to disable it
  6. Try Acquisition.Start on the photonfocus device
  7. Read Acquisition.Error if start fails
  8. Print a clear verdict + next steps
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from specim_eye_api import SpecimCameraAPI, EyeError


def main():
    print("=" * 60)
    print("SpecimIQ Diagnostic — Camera Connect + Remote Mode + Acquisition")
    print("=" * 60)

    cam = SpecimCameraAPI()
    cam.set_logging(True)  # SDK qDebug output (shows RPC traffic)

    try:
        print("\n[1/6] Connecting to camera via eyeapi2.dll...")
        cam.connect()
        print(f"    OK — connected handles: {cam.handles}")

    except EyeError as e:
        print(f"    FAILED: {e}")
        print("\n>>> Is the camera powered on and USB-connected?")
        print(">>> Check Windows 'ipconfig' for an 'Ethernet N' adapter with 169.254.x.x")
        return 1

    # ------------------------------------------------------------
    # 2. Enumerate all devices and their names
    # ------------------------------------------------------------
    print("\n[2/6] Enumerating RPC devices (5 expected: photonfocus, rgb1, rgb2, stepmotor, server)...")
    for h in cam.handles:
        try:
            name = cam.device_name(h)
            print(f"    handle {h:>2} : {name!r}")
        except EyeError as e:
            print(f"    handle {h:>2} : NAME READ FAILED ({e})")

    # ------------------------------------------------------------
    # 3. Locate photonfocus (system device) and rgb1 (sensor)
    # ------------------------------------------------------------
    print("\n[3/6] Looking up device handles by name...")
    pf = None
    rgb = None
    for name in ("photonfocus", "rgb1", "rgb2", "stepmotor", "server"):
        try:
            h = cam.handle_for(name)
            print(f"    {name:<15} -> handle {h}")
            if name == "photonfocus":
                pf = h
            elif name == "rgb1":
                rgb = h
        except EyeError as e:
            print(f"    {name:<15} -> NOT FOUND ({e})")

    if pf is None:
        print("\n    WARNING: photonfocus not found — falling back to first handle")
        pf = cam.handles[0]
    if rgb is None:
        rgb = cam.handles[0]

    # ------------------------------------------------------------
    # 4. Check Remote mode on photonfocus
    # ------------------------------------------------------------
    print(f"\n[4/6] Checking Remote mode on photonfocus (handle {pf})...")
    remote_on = False
    for feat in ("System.RemoteModeOn", "System.Remote.Enabled", "System.Remote.Password"):
        try:
            v = cam.get(pf, feat)
            print(f"    {feat} = {v!r}")
            if feat in ("System.RemoteModeOn", "System.Remote.Enabled") and v:
                remote_on = True
        except EyeError as e:
            print(f"    {feat} -> READ FAILED ({e})")

    # ------------------------------------------------------------
    # 5. If Remote is ON, try to disable it
    # ------------------------------------------------------------
    if remote_on:
        print("\n[5/6] Remote mode is ON — attempting to disable...")
        for feat, val in (("System.RemoteModeOn", 0), ("System.Remote.Enabled", 0)):
            try:
                cam.set_int(pf, feat, val)
                print(f"    Set {feat} = {val} : OK")
            except EyeError as e:
                print(f"    Set {feat} = {val} FAILED: {e}")
                if "password" in str(e).lower():
                    print("    >>> Camera requires a Remote password (set in Studio).")
                    print("    >>> Open SpecimIQ Studio, go to System > Remote, uncheck 'Enabled'")
                    print("    >>> or enter the password here manually if you know it.")
    else:
        print("\n[5/6] Remote mode already OFF — good.")

    # ------------------------------------------------------------
    # 6. Try Acquisition.Start on photonfocus
    # ------------------------------------------------------------
    print(f"\n[6/6] Attempting Acquisition.Start=1 on photonfocus (handle {pf})...")
    try:
        cam.set_int(pf, "Acquisition.Start", 1)
        print("    Acquisition.Start = 1 : OK")

        # Brief wait, then check status
        import time
        time.sleep(0.5)
        acq = cam.get(pf, "Acquisition.Acquiring")
        print(f"    Acquisition.Acquiring = {acq}")

        # Stop cleanly
        cam.set_int(pf, "Acquisition.Stop", 1)
        time.sleep(0.2)
        acq = cam.get(pf, "Acquisition.Acquiring")
        print(f"    After Stop — Acquiring = {acq}")
        print("\n>>> ACQUISITION WORKS! <<<")
        print("    Next step: run the full pipeline:")
        print("        python acquire.py --count 3 --out captures/")

    except EyeError as e:
        print(f"    FAILED: {e}")

        # Read the detailed error from the camera
        try:
            err = cam.get(pf, "Acquisition.Error")
            print(f"    Acquisition.Error = {err!r}")
        except EyeError:
            pass

        # Common causes
        print("\n>>> COMMON CAUSES FOR -3 ON Acquisition.Start:")
        print("    1. Remote mode still ON (camera ignores local acquisition)")
        print("    2. Camera needs a valid exposure time (try setting Camera.ExposureTime > 0)")
        print("    3. Camera busy / previous acquisition not cleanly stopped")
        print("    4. SD card full or not inserted (camera saves to SD)")
        print("    5. Camera firmware state requires a power-cycle")

        # Suggested next steps
        print("\n>>> SUGGESTED NEXT STEPS:")
        print("    a) Set exposure first:")
        print("           cam.set_float(pf, 'Camera.ExposureTime', 4000.0)  # 4 ms")
        print("    b) Power-cycle camera (unplug USB, wait 10s, replug)")
        print("    c) Open SpecimIQ Studio, ensure Remote is OFF, close Studio")
        print("    d) Check SD card is inserted and has space")

    finally:
        cam.disconnect()

    print("\n" + "=" * 60)
    print("Diagnostic complete.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())