"""
Client prerequisite health checks and repair flow.

Checks for:
- VC++ Redistributable 2015-2022 x64 (required by ODA)
- ODA File Converter (required ONLY for .dwg files; .dxf files skip ODA entirely)

SketchUp integration has been removed — ODA is the sole external dependency.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import time
from typing import Callable, Dict

from app.core.cad_import import find_oda_converter, subprocess_flags
from app.utils.paths import resource_path

# winreg only exists on Windows.  This module is only meaningful there,
# but importing it unconditionally turned a wrong-platform run into an
# import error at startup rather than a health check that says "no".
try:
    import winreg
except ImportError:  # pragma: no cover - not Windows
    winreg = None

LogCallback = Callable[[str, str], None]

def _noop_log(severity: str, message: str) -> None:
    pass

def verify_prerequisites(log: LogCallback = _noop_log) -> Dict[str, str]:
    """
    Returns a dict of component -> status ("ok" | "missing" | "broken")
    Never raises an exception.
    """
    status = {}

    # 1. Check VC++ Redistributable 2015-2022 x64
    vc_status = "missing"
    try:
        if winreg is None:
            raise OSError("no registry on this platform")
        key_path = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            installed, _ = winreg.QueryValueEx(key, "Installed")
            if installed == 1:
                vc_status = "ok"
    except Exception:
        pass
    
    status["vc_redist"] = vc_status

    # 2. Check ODA File Converter (only needed for .dwg → .dxf conversion)
    oda_status = "missing"
    oda_path = find_oda_converter(log)
    
    if oda_path and os.path.isfile(oda_path):
        oda_status = "ok"
        try:
            with tempfile.TemporaryDirectory() as tmp_in, tempfile.TemporaryDirectory() as tmp_out:
                cmd = [oda_path, tmp_in, tmp_out, "ACAD2018", "DXF", "0", "1"]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **subprocess_flags(),
                )
                if result.returncode != 0 and "qt" in result.stderr.lower():
                    oda_status = "broken"
        except subprocess.TimeoutExpired:
            oda_status = "ok"
        except Exception:
            oda_status = "broken"

    status["oda"] = oda_status

    # Log clarification about ODA scope
    if oda_status != "ok":
        log("info",
            "Note: ODA File Converter is only required for importing .dwg files. "
            "You can still import .dxf files directly without ODA installed.")

    return status

def repair_dwg_support(
    log: LogCallback = _noop_log,
    report: Callable[[str], None] = lambda _msg: None,
) -> bool:
    """
    Attempts to repair DWG support by running bundled installers for VC++ and ODA.
    Requires UAC elevation, triggered via ShellExecuteW.

    Blocks for as long as the installers take — up to three minutes — so
    it must be called from a worker thread, never from the one drawing
    the window.  ``report`` is how it says where it has got to.
    """
    log("info", "Starting DWG support repair flow...")
    
    prereqs_dir = resource_path(os.path.join("app", "resources", "prereqs"))
    vc_installer = os.path.join(prereqs_dir, "vc_redist.x64.exe")
    oda_msi = os.path.join(prereqs_dir, "ODAFileConverter.msi")
    
    if not os.path.isfile(vc_installer) or not os.path.isfile(oda_msi):
        log("error", "Repair failed: Bundled installers not found in app/resources/prereqs/")
        return False
        
    with tempfile.TemporaryDirectory() as tmp_dir:
        bat_path = os.path.join(tmp_dir, "repair.bat")
        flag_path = os.path.join(tmp_dir, "repair.done")
        
        bat_content = f"""@echo off
echo Installing VC++ Redistributable...
"{vc_installer}" /install /quiet /norestart
echo Installing ODA File Converter...
msiexec /i "{oda_msi}" /qn /norestart
echo done > "{flag_path}"
"""
        with open(bat_path, "w") as f:
            f.write(bat_content)
            
        log("info", "Requesting administrator privileges to install missing components...")
        
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", bat_path, None, tmp_dir, 0)
        
        if ret <= 32:
            log("error", f"Repair cancelled or failed to elevate (error code: {ret})")
            return False
            
        log("info", "Repair in progress, please wait...")
        report("Installing the DWG prerequisites…")

        timeout_seconds = 180
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            if os.path.isfile(flag_path):
                log("success", "Repair script finished successfully.")
                break
            time.sleep(1)
            report(
                "Installing the DWG prerequisites… "
                f"({int(time.time() - start_time)}s)"
            )
        else:
            log("error", "Repair timed out after 3 minutes.")
            return False

    # Verify again
    report("Checking that DWG support now works…")
    status = verify_prerequisites(log)
    if status.get("oda") == "ok" and status.get("vc_redist") == "ok":
        log("success", "DWG support is now fully operational.")
        return True
    else:
        log("error", "Repair finished, but components are still missing or broken.")
        return False
