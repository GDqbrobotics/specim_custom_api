#!/usr/bin/env python3
"""
SpecimIQ acquisition pipeline for a photogrammetry machine.

Flow:
  1. UDP-discover cameras (pure Python, prints what announces itself)
  2. connect via eyeapi2.dll (IQ SDK)
  3. read identity (model / serial / device name)
  4. register a real-time data callback that writes frames to <out>/frame_XXXX.raw
  5. configure (exposure, frame count), start acquisition, wait, stop
  6. (optional) list datasets on the camera's SD card via FTP (port 5010,
     ftpuser/password)

Usage:
  python acquire.py                          # 1 frame, 4 µs exposure
  python acquire.py --count 10 --exposure 8 --out captures/
  python acquire.py --list-datasets          # just list FTP datasets
  python acquire.py --discover-only          # just run UDP discovery
"""
import argparse
import ftplib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from specim_eye_api import (  # noqa: E402
    SpecimCameraAPI, discover_cameras, camera_targets, EyeError,
)


def discover_only(timeout: float) -> int:
    hits = discover_cameras(timeout=timeout)
    if not hits:
        print(f"no camera announced itself on UDP 54546 within {timeout:.0f}s")
        return 1
    for h in hits:
        print("discovered:", {k: v for k, v in h.items() if k != "_from"},
              "from", h.get("_from"))
    return 0


def list_datasets(host: str, port: int = 5010, user: str = "ftpuser",
                  passwd: str = "password") -> int:
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=10)
    ftp.login(user, passwd)
    print(f"logged in to {host}:{port} as {user}")
    print("datasets on camera SD card:")
    try:
        for line in ftp.retrlines("NLST /datasets"):
            print("  ", line.strip())
    except ftplib.error_perm as e:
        print("  (NLST failed:", e, ")")
        print("  root listing:")
        for line in ftp.retrlines("NLST"):
            print("   ", line.strip())
    ftp.quit()
    return 0


def acquire(args) -> int:
    # 1. discovery (informational)
    if not args.no_discover:
        print(f"[1/5] UDP discovery ({args.discover_timeout:.0f}s)...")
        for h in discover_cameras(timeout=args.discover_timeout):
            print("      ", h.get("type"), h.get("addresses", []),
                  "from", h.get("_from"))

    # 2. connect
    print("[2/5] connecting via eyeapi2.dll ...")
    cam = SpecimCameraAPI(dll_dir=args.dll_dir)
    cam.set_logging(args.debug)
    cam.connect(timeout=args.connect_timeout)
    # A SpecimIQ camera exposes several RPC objects: 'rgb1' (sensor),
    # 'photonfocus' (System.*), 'stepmotor', 'server'. Use the right one.
    try:
        h = cam.handle_for("rgb1")
    except EyeError:
        h = cam.h(0)
    try:
        sys_h = cam.handle_for("photonfocus")
    except EyeError:
        sys_h = h
    print("      devices:", [d["name"] for d in cam.devices()])

    # 3. identity
    print("[3/5] camera identity:")
    for feature, hh in (("Camera.Model", h), ("Camera.SerialNumber", h),
                        ("System.DeviceName", sys_h),
                        ("System.FirmwareVersion", sys_h)):
        try:
            print(f"      {feature} = {cam.get(hh, feature)!r}")
        except EyeError:
            print(f"      {feature} = (unavailable)")
    try:
        print(f"      FTP port = {cam.ftp_port()} "
              "(user ftpuser / password password)")
    except EyeError as e:
        print(f"      FTP port unavailable ({e})")

    # 4. frame sink
    os.makedirs(args.out, exist_ok=True)
    width = None
    try:
        width = int(cam.get_int(h, "Camera.Image.Width"))
    except EyeError:
        pass
    frames: list[bytes] = []

    def on_frame(data: bytes, s1: int, s2: int) -> None:
        frames.append(data)
        print(f"      frame #{len(frames)}: {len(data)} bytes "
              f"(s1={s1}, s2={s2})", flush=True)

    cam.add_data_callback(on_frame, h)

    # 5. configure + capture
    print(f"[4/5] exposure={args.exposure} µs, count={args.count}")
    try:
        cam.set_float(h, "Camera.ExposureTime", float(args.exposure))
    except EyeError as e:
        print("      (exposure set failed:", e, ")")
    try:
        cam.set_int(h, "Camera.Count", args.count)
    except EyeError as e:
        print("      (count set failed:", e, ")")

    writeable = cam.is_writeable(h, "Camera.Count")
    if writeable:
        print(f"[4b/5] setting Camera.Count = {args.count}")
        cam.set_int(h, "Camera.Count", args.count)
    else:
        print("[4b/5] Camera.Count is read-only here; will do "
              f"{args.count} start/stop cycle(s) of 1 frame each")

    t0 = time.time()
    if writeable:
        cam.start(h)
        cam.wait_acquisition_done(h, timeout=args.timeout)
        cam.stop(h)
    else:
        for i in range(max(1, args.count)):
            cam.start(h)
            cam.wait_acquisition_done(h, timeout=args.timeout)
            cam.stop(h)
            print(f"      cycle {i + 1}/{max(1, args.count)} done, "
                  f"frames so far: {len(frames)}")
    dt = time.time() - t0
    print(f"[5/5] acquisition finished in {dt:.1f}s, "
          f"frames received: {len(frames)}")

    # save frames
    if width:
        per_frame = width * 2  # 16-bit spectral data assumption
        n_frames = max(1, len(frames) // max(per_frame, 1)) if frames else 0
    else:
        n_frames = 0
    if frames:
        if n_frames > 1:
            for i in range(n_frames):
                chunk = frames[i * len(frames) // n_frames:
                               (i + 1) * len(frames) // n_frames]
                blob = b"".join(chunk)
                path = os.path.join(args.out, f"frame_{i:04d}.raw")
                with open(path, "wb") as f:
                    f.write(blob)
            print(f"      wrote {n_frames} frames to {args.out}/")
        else:
            path = os.path.join(args.out, "capture.raw")
            with open(path, "wb") as f:
                f.write(b"".join(frames))
            print(f"      wrote raw capture to {path} "
                  f"({len(frames) and sum(len(x) for x in frames)} bytes)")

    # 6. optional FTP listing
    if args.list_datasets:
        print("FTP datasets:")
        host = "192.168.7.2"  # USB bridge fallback
        targets = camera_targets(discover_cameras(timeout=3.0))
        if targets:
            host = targets[0][0]
        print(f"      trying FTP host {host}")
        list_datasets(host)

    cam.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", type=int, default=1, help="frames to record")
    p.add_argument("--exposure", type=float, default=4.0, help="exposure µs")
    p.add_argument("--out", default="captures", help="output directory")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="acquisition wait timeout (s)")
    p.add_argument("--connect-timeout", type=float, default=20.0,
                   help="discovery/connect timeout (s)")
    p.add_argument("--discover-timeout", type=float, default=3.0,
                   help="initial UDP discovery window (s)")
    p.add_argument("--dll-dir", default=None,
                   help="folder with eyeapi2.dll (default: libs/)")
    p.add_argument("--no-discover", action="store_true")
    p.add_argument("--discover-only", action="store_true")
    p.add_argument("--list-datasets", action="store_true",
                   help="list camera datasets over FTP (5010)")
    p.add_argument("--debug", action="store_true", help="SDK qDebug logging")
    args = p.parse_args()

    if args.discover_only:
        return discover_only(args.connect_timeout)
    try:
        return acquire(args)
    except EyeError as e:
        print("ERROR:", e)
        return 1
    except FileNotFoundError as e:
        print("ERROR:", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
