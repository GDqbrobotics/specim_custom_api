#!/usr/bin/env bash
# Smoke test: load the SpecimIQ eyeapi.dll (as eyeapi2.dll) under Wine and
# exercise the EYE_* C API without a camera attached.
#
# Expected output (no camera):
#   OK  loaded eyeapi2.dll
#   OK  all EYE_* symbols resolved ...
#   EYE_Load = 0
#   EYE_GetFtpPort = 0 (expect 5010) MISMATCH   <- before connect() is normal
#   EYE_OpenConnectedDevices(discovery=1) = 0, count=0
#   EYE_Opened = 1
#   DONE
set -e
cd "$(dirname "$0")"

# 1. Make sure the DLLs needed by eyeapi2.dll (IQ) are present.
ROOT="$(cd ../.. && pwd)"
cp -n "$ROOT/eyeapi.dll"     eyeapi2.dll
cp -n "$ROOT/qjsonrpc.dll"   qjsonrpc.dll
cp -n "$ROOT/Qt5Core.dll"    Qt5Core.dll
cp -n "$ROOT/Qt5Network.dll" Qt5Network.dll

# 2. Build the test binary if missing (needs a mingw-w64 cross gcc).
if [ ! -x test.exe ] || [ test.c -nt test.exe ]; then
    CC=""
    for c in x86_64-w64-mingw32-gcc-posix x86_64-w64-mingw32-gcc; do
        command -v "$c" >/dev/null && CC="$c" && break
    done
    if [ -z "$CC" ] && [ -x /tmp/mingw_pkgs/root/usr/bin/x86_64-w64-mingw32-gcc-posix ]; then
        CC=/tmp/mingw_pkgs/root/usr/bin/x86_64-w64-mingw32-gcc-posix
    fi
    if [ -z "$CC" ]; then
        echo "no x86_64-w64-mingw32-gcc found (apt install mingw-w64)"; exit 1
    fi
    INC=/usr/x86_64-w64-mingw32/include
    LIB=/usr/x86_64-w64-mingw32/lib
    [ -d "$INC" ] || INC=/tmp/mingw_pkgs/root/usr/x86_64-w64-mingw32/include
    [ -d "$LIB" ] || LIB=/tmp/mingw_pkgs/root/usr/x86_64-w64-mingw32/lib
    "$CC" -I"$INC" -L"$LIB" test.c -o test.exe -lkernel32
fi

# 3. Run under Wine (offscreen Qt, quiet logs).
QT_QPA_PLATFORM=offscreen WINEDEBUG=-all wine64 ./test.exe || \
    QT_QPA_PLATFORM=offscreen WINEDEBUG=-all wine ./test.exe
