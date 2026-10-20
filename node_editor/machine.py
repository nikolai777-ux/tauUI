"""What this computer has, read from Windows itself (never written in a file): OS, CPU, RAM, the GPUs that are
present now and their memory. Told to the Executor's brain (get_task), so its instructions hold on any computer.

GPUs: Windows' list of PRESENT display devices (SetupAPI, DIGCF_PRESENT) - the registry also keeps cards that were
removed long ago - each one's driver entry (SPDRP_DRIVER = "{display class}\\0001") holds its name (DriverDesc) and
memory (HardwareInformation.qwMemorySize).
"""

import ctypes
import os
import platform
from ctypes import wintypes

_facts = {}
DISPLAY_CLASS = "{4d36e968-e325-11ce-bfc1-08002be10318}"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _DEVINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID), ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


def _present_display_drivers():
    """Driver keys ("{class}\\0001") of the display devices present now."""
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DEVINFO)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DEVINFO), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    hexes = DISPLAY_CLASS.strip("{}").split("-")
    tail = bytes.fromhex(hexes[3] + hexes[4])
    guid = _GUID(int(hexes[0], 16), int(hexes[1], 16), int(hexes[2], 16), (ctypes.c_ubyte * 8)(*tail))
    DIGCF_PRESENT, SPDRP_DRIVER = 0x2, 0x9
    handle = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        i = 0
        while True:
            info = _DEVINFO()
            info.cbSize = ctypes.sizeof(_DEVINFO)
            if not setupapi.SetupDiEnumDeviceInfo(handle, i, ctypes.byref(info)):
                break
            i += 1
            buf = ctypes.create_unicode_buffer(512)
            if setupapi.SetupDiGetDeviceRegistryPropertyW(handle, ctypes.byref(info), SPDRP_DRIVER, None, buf,
                                                          ctypes.sizeof(buf), None):
                out.append(buf.value)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
    return out


def gpus():
    """[{"name", "memory_gb"}] of the GPUs present now (memory: None when the driver doesn't record it)."""
    import winreg
    out = []
    for driver in _present_display_drivers():
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + driver)
            name = winreg.QueryValueEx(key, "DriverDesc")[0]
            try:
                size = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0]
            except OSError:
                size = None
        except OSError:
            continue
        if "basic render" in name.lower() or "basic display" in name.lower():
            continue  # Windows' software stand-in, not a GPU
        out.append({"name": name, "memory_gb": round(size / 2 ** 30) if isinstance(size, int) and size else None})
    return out


def ram_gb():
    class _MEM(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = _MEM()
    m.dwLength = ctypes.sizeof(_MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return round(m.ullTotalPhys / 2 ** 30)


def cpu():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        return " ".join(winreg.QueryValueEx(key, "ProcessorNameString")[0].split())
    except OSError:
        return platform.processor()


def facts():
    """This computer, read once per app run (the hardware doesn't change while it runs)."""
    if not _facts:
        def safe(fn, default):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - a fact that can't be read is left out, never guessed
                return default
        _facts.update(os=f"{platform.system()} {platform.release()} ({platform.version()})",
                      cpu=safe(cpu, ""), cpu_threads=os.cpu_count(), ram_gb=safe(ram_gb, None),
                      gpus=safe(gpus, []))
    return dict(_facts)
