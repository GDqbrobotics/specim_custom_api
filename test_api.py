#!/usr/bin/env python
"""
Test script for the Specim Eye API (specim_eye_api.py).

Run with:  python test_api.py [--no-dll] [--host HOST] [--debug] [--acquire]

By default, tests use eyeapi2.dll. With --no-dll, they use EyeJsonRpc to
connect directly to the camera's JSON-RPC service; --host selects its address.
Without --acquire, the script only reads features. With --acquire it configures
a short capture (1000 lines), starts Acquisition.Start, waits for completion
and stops — i.e. it actually triggers an acquisition on the camera.

Prerequisites:
  * Specim camera connected and powered on
  * Specim camera driver installed (the one that ships with Insight)
  * DLL mode requires eyeapi2.dll and its Qt dependencies in the configured
    libs folder; --no-dll does not load the DLLs
"""

import argparse
import sys
import time

from specim_eye_api import EyeError, EyeJsonRpc, SpecimCameraAPI


class TestRunner:
    def __init__(self):
        self.results = []

    def run_test(self, name, test_func):
        print(f"\n  [{name}] ...", end=" ", flush=True)
        result = False
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
    parser = argparse.ArgumentParser(description="Test the Specim Eye camera API.")
    parser.add_argument(
        "--no-dll",
        action="store_true",
        help="use the raw JSON-RPC client instead of loading eyeapi2.dll",
    )
    parser.add_argument(
        "--host",
        help="camera host for --no-dll (default: 192.168.7.2, the USB bridge address)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable eyeapi2.dll diagnostic logging (DLL mode only)",
    )
    parser.add_argument(
        "--acquire",
        action="store_true",
        help="configure and trigger a 1000-line capture",
    )
    args = parser.parse_args()
    if args.host is not None and not args.no_dll:
        parser.error("--host can only be used together with --no-dll")

    print("=" * 60)
    print("Specim Eye API Test Suite")
    print("=" * 60)

    runner = TestRunner()

    if args.no_dll:
        host = args.host or "192.168.7.2"
        print(f"\n1. Connecting to {host}:5001 (raw JSON-RPC; no DLL)")
        cam = EyeJsonRpc(host=host)
    else:
        print("\n1. Connecting (EYE_Load + EYE_OpenConnectedDevices)")
        cam = SpecimCameraAPI()
        cam.set_logging(args.debug)

    def connect():
        if args.no_dll:
            cam.connect()
            return True
        cam.connect()
        return cam.connected

    if not runner.run_test("Connect", connect):
        runner.print_summary()
        return 1

    if args.no_dll:
        h = None
    else:
        print(f"\n   Connected handles: {cam.handles}")
        h = cam.handles[0]

    def get_float(feature):
        return cam.get_float(feature) if args.no_dll else cam.get_float(h, feature)

    def get_int(feature):
        return cam.get_int(feature) if args.no_dll else cam.get_int(h, feature)

    def get_string(feature):
        return cam.get_string(feature) if args.no_dll else cam.get_string(h, feature)

    def get_bool(feature):
        return cam.get_bool(feature) if args.no_dll else cam.get_bool(h, feature)

    print("\n2. Camera identification")

    def model():
        m = get_string("Camera.Model")
        print(f"   model: {m!r}")
        return m is not None

    def device_name():
        n = get_string("System.DeviceName")
        print(f"   device: {n!r}")
        return n is not None

    def firmware():
        try:
            fw = get_string("System.FirmwareVersion")
            print(f"   firmware: {fw!r}")
            return fw is not None
        except EyeError:
            return False

    runner.run_test("Camera.Model", model)
    runner.run_test("System.DeviceName", device_name)
    runner.run_test("System.FirmwareVersion", firmware)

    print("\n3. Readable acquisition settings")

    def exposure():
        e = get_float("Camera.ExposureTime")
        print(f"   exposure: {e} µs")
        return e is not None

    def count():
        c = get_int("Camera.Count")
        print(f"   count: {c}")
        return c is not None

    def framerate():
        try:
            fr = get_float("Camera.FrameRate")
            print(f"   frame rate: {fr}")
            return fr is not None
        except EyeError:
            return False

    def acquiring():
        a = get_bool("Acquisition.Acquiring")
        print(f"   acquiring: {a}")
        return not a  # expect idle before we start

    runner.run_test("Camera.ExposureTime", exposure)
    runner.run_test("Camera.Count", count)
    runner.run_test("Camera.FrameRate", framerate)
    runner.run_test("Acquisition.Acquiring (idle)", acquiring)

    if args.no_dll:
        print("\n4. FTP / file server: skipped (raw JSON-RPC helper has no FTP-port API)")
    else:
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

    if args.acquire:
        print("\n5. Acquisition test (1000 lines)")

        initial_counter = None
        try:
            initial_counter = get_int("Acquisition.FrameCounter")
        except EyeError:
            pass

        def do_capture():
            t0 = time.time()
            if args.no_dll:
                cam.set_float("Camera.ExposureTime", 4.0)
                cam.set_int("Camera.Count", 1000)
                cam.set_int("Acquisition.Start", 1)
                try:
                    deadline = time.time() + 60
                    while time.time() < deadline:
                        if not get_bool("Acquisition.Acquiring"):
                            break
                        time.sleep(0.2)
                    else:
                        raise TimeoutError("acquisition still running after 60s")
                finally:
                    cam.set_int("Acquisition.Stop", 1)
            else:
                cam.acquire(handle=h, exposure_us=4.0, count=1000, wait=True, timeout=60)
            print(f"   capture done in {time.time() - t0:.1f}s")
            return True

        runner.run_test("Trigger + wait acquisition", do_capture)

        def counter_moved():
            try:
                c = get_int("Acquisition.FrameCounter")
                print(f"   frame counter: {initial_counter} -> {c}")
                return initial_counter is None or c != initial_counter
            except EyeError:
                return True  # counter not exposed; not a failure

        runner.run_test("FrameCounter advanced", counter_moved)

        def back_to_idle():
            time.sleep(0.5)
            return not get_bool("Acquisition.Acquiring")

        runner.run_test("Back to idle", back_to_idle)
    else:
        print("\n5. Acquisition test: skipped (pass --acquire to trigger one)")

    ok = runner.print_summary()
    if args.no_dll:
        cam.close()
    else:
        cam.disconnect()
    print("\nBye.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
