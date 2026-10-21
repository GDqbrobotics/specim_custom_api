/* Smoke test for SpecimIQ eyeapi.dll under Wine.
 * Uses the mangled C++ export names (the only names in the export table).
 */
#include <windows.h>
#include <stdio.h>

typedef int (__cdecl *F_int)(void);
typedef void (__cdecl *F_log)(int);
typedef int (__cdecl *F_ocs)(unsigned*, int);
typedef int (__cdecl *F_chg)(void);
typedef int (__cdecl *F_list)(unsigned*, int);
typedef int (__cdecl *F_name)(wchar_t*, unsigned, unsigned);

static void* get(HMODULE h, const char* n) { return GetProcAddress(h, n); }

int main(void) {
    HMODULE h = LoadLibraryW(L"eyeapi2.dll");
    if (!h) { printf("FAIL LoadLibrary: %lu\n", GetLastError()); return 2; }
    printf("OK  loaded eyeapi2.dll\n");

    F_int Load   = (F_int)get(h, "?EYE_Load@@YAHXZ");
    F_int Unload = (F_int)get(h, "?EYE_Unload@@YAHXZ");
    F_int Opened = (F_int)get(h, "?EYE_Opened@@YAHXZ");
    F_int Ftp    = (F_int)get(h, "?EYE_GetFtpPort@@YAHXZ");
    F_log  Log   = (F_log)get(h, "?EYE_SetLogging@@YAX_N@Z");
    F_ocs  OCS   = (F_ocs)get(h, "?EYE_OpenConnectedDevices@@YAHPEAH_N@Z");
    F_chg  Chg   = (F_chg)get(h, "?EYE_CheckConnectedDevicesChanged@@YA_NXZ");
    F_list List  = (F_list)get(h, "?EYE_GetHandleByIndex@@YAHPEA_JH@Z");
    F_name Name  = (F_name)get(h, "?EYE_GetDeviceName@@YAHPEA_WHH@Z");
    if (!Load || !Opened || !Ftp || !Log || !OCS) {
        printf("FAIL missing procs (Load=%p Opened=%p Ftp=%p Log=%p OCS=%p)\n",
               (void*)Load, (void*)Opened, (void*)Ftp, (void*)Log, (void*)OCS);
        return 3;
    }
    printf("OK  all EYE_* symbols resolved (Chg=%p Handle=%p Name=%p Unload=%p)\n",
           (void*)Chg, (void*)List, (void*)Name, (void*)Unload);

    int r = Load();
    printf("EYE_Load = %d\n", r);
    Log(1);
    printf("EYE_SetLogging(1) done\n");
    int p = Ftp();
    printf("EYE_GetFtpPort (pre-connect) = %d (0 expected: no SDK instance yet)\n", p);

    unsigned n = 0xDEAD;
    r = OCS(&n, 1);   /* start async discovery */
    printf("EYE_OpenConnectedDevices(discovery=1) = %d, count=%u\n", r, n);

    /* give the discovery listener a few seconds (no camera attached) */
    Sleep(3000);
    if (Chg) printf("EYE_CheckConnectedDevicesChanged = %d\n", Chg());
    n = 0xDEAD;
    r = OCS(&n, 1);
    printf("EYE_OpenConnectedDevices(2nd) = %d, count=%u\n", r, n);

    /* now the SDK instance exists -> getFtpPort should be the fixed 5010 */
    p = Ftp();
    printf("EYE_GetFtpPort (post-connect) = %d (expect 5010) %s\n",
           p, p == 5010 ? "OK" : "MISMATCH");
    if (Name) {
        wchar_t buf[256] = {0};
        Name(buf, 256, 1);
        printf("EYE_GetDeviceName(1) = \"%ls\"\n", buf);
    }
    printf("EYE_Opened = %d\n", Opened());
    if (Unload) {
        printf("EYE_Unload = %d\n", Unload());
    }
    printf("DONE\n");
    return 0;
}
