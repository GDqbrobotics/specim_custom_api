#!/usr/bin/env python
"""
Test script for the Specim Eye API (specim_eye_api.py).

Run with:  python test_api.py [--acquire]

Without --acquire it only reads non-invasive features (model, firmware,
exposure, ...). With --acquire it configures a short capture (1000 lines),
starts Acquisition.Start, waits for completion and stops — i.e. it actually
triggers an acquisition on the camera.

Prerequisites:
  * Specim camera (FX17) connected and powered on
  * Specim camera driver installed (the one that ships with Insight)
  * Run from the machine where the camera is attached (this API folder is
    inside the Insight install, so eyeapi.dll + Qt DLLs resolve automatically)
"""

import sys
import time

from specim_eye_api import SpecimCameraAPI, EyeError


class TestRunner:
    def __init__(self):
        self.results = []

    def run_test(self, name, test_func):
        print(f"\n  [{name}] ...", end=" ", flush=True)
        try:
            result = test_func()
            if result:
                print("PASS")
                self.results.append((name, True, None))
            else:
                print("FAIL")
                self.results.append((name, False, "returned falsy"))
        except Exception as e:
            print(f"ERROR: {e}")
            self.results.append((name, False, str(e)))
        return result

    def print_summary(self):
        total = len(self.results)
        failed = [r for r in self.results if not r[1]]
        print("\n" + "=" * 60)
        print("TEST SUMMARY")
        print("=" * 60)
        print(f"Total:  {total}")
        print(f"Passed: {total - len(failed)}")
        print(f"Failed: {len(failed)}")
        if failed:
            print("\nFailed tests:")
            for name, _, err in failed:
                print(f"  - {name}: {err}")
        return not failed


def main():
    do_acquire = "--acquire" in sys.argv

    print("=" * 60)
    print("Specim Eye API Test Suite")
    print("=" * 60)

    runner = TestRunner()

    print("\n1. Connecting (EYE_Load + EYE_OpenConnectedDevices)")
    cam = SpecimCameraAPI()
    cam.set_logging(False)

    def connect():
        cam.connect()
        return cam.connected

    if not runner.run_test("Connect", connect):
        runner.print_summary()
        return 1

    print(f"\n   Connected handles: {cam.handles}")
    h = cam.handles[0]

    print("\n2. Camera identification")

    def model():
        m = cam.get(h, "Camera.Model")
        print(f"   model: {m!r}")
        return m is not None

    def device_name():
        n = cam.get(h, "System.DeviceName")
        print(f"   device: {n!r}")
        return n is not None

    def firmware():
        try:
            fw = cam.get(h, "System.FirmwareVersion")
            print(f"   firmware: {fw!r}")
            return fw is not None
        except EyeError:
            return False

    runner.run_test("Camera.Model", model)
    runner.run_test("System.DeviceName", device_name)
    runner.run_test("System.FirmwareVersion", firmware)

    print("\n3. Readable acquisition settings")

    def exposure():
        e = cam.get(h, "Camera.ExposureTime")
        print(f"   exposure: {e} µs")
        return e is not None

    def count():
        c = cam.get(h, "Camera.Count")
        print(f"   count: {c}")
        return c is not None

    def framerate():
        try:
            fr = cam.get(h, "Camera.FrameRate")
            print(f"   frame rate: {fr}")
            return fr is not None
        except EyeError:
            return False

    def acquiring():
        a = cam.get_bool(h, "Acquisition.Acquiring")
        print(f"   acquiring: {a}")
        return not a  # expect idle before we start

    runner.run_test("Camera.ExposureTime", exposure)
    runner.run_test("Camera.Count", count)
    runner.run_test("Camera.FrameRate", framerate)
    runner.run_test("Acquisition.Acquiring (idle)", acquiring)

    print("\n4. FTP / file server")

    def ftp():
        try:
            p = cam.ftp_port()
            print(f"   FTP port: {p}")
            return p > 0
        except EyeError as e:
            print(f"   (no FTP port: {e})")
            return False

    runner.run_test("EYE_GetFtpPort", ftp)

    if do_acquire:
        print("\n5. Acquisition test (1000 lines)")

        initial_counter = None
        try:
            initial_counter = cam.get_int(h, "Acquisition.FrameCounter")
        except EyeError:
            pass

        def do_capture():
            t0 = time.time()
            cam.acquire(handle=h, exposure_us=4.0, count=1000, wait=True, timeout=60)
            print(f"   capture done in {time.time() - t0:.1f}s")
            return True

        runner.run_test("Trigger + wait acquisition", do_capture)

        def counter_moved():
            try:
                c = cam.get_int(h, "Acquisition.FrameCounter")
                print(f"   frame counter: {initial_counter} -> {c}")
                return initial_counter is None or c != initial_counter
            except EyeError:
                return True  # counter not exposed; not a failure

        runner.run_test("FrameCounter advanced", counter_moved)

        def back_to_idle():
            time.sleep(0.5)
            return not cam.get_bool(h, "Acquisition.Acquiring")

        runner.run_test("Back to idle", back_to_idle)
    else:
        print("\n5. Acquisition test: skipped (pass --acquire to trigger one)")

    ok = runner.print_summary()
    cam.disconnect()
    print("\nBye.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
