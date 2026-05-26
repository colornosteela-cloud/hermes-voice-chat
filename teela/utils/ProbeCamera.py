#!/usr/bin/env python3
"""
Teela Camera Probe — Hardware Diagnostics
============================================
Run this BEFORE starting VisionNode to check if your CSI camera
is detectable and which GStreamer pipeline will work.

Usage:
    python ProbeCamera.py --verbose
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: str, shell: bool = True, timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"


def banner(title: str) -> None:
    print(f"\n{'=' * 50}")
    print(f"  {title}")
    print(f"{'=' * 50}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def status(label: str, ok: bool, details: str = "") -> None:
    sym = "✅" if ok else "❌"
    print(f"  {sym} {label}{f': {details}' if details else ''}")


def check_jetson_model() -> dict:
    """Probe Jetson board + JetPack version."""
    info = {}
    section("Jetson Board Detection")
    
    # JetPack release
    jp = Path("/etc/nv_tegra_release")
    if jp.exists():
        lines = jp.read_text().splitlines()
        info["jetpack"] = lines[0] if lines else "unknown"
        print(f"  JetPack: {info['jetpack']}")
    else:
        info["jetpack"] = None
        print("  ⚠️  /etc/nv_tegra_release not found (not a Jetson?)")
    
    # Device-tree compatible
    dt = Path("/proc/device-tree/compatible")
    if dt.exists():
        raw = dt.read_bytes().replace(b"\x00", b"\n").decode(errors="ignore")
        info["board"] = raw.strip().splitlines()[0] if raw.strip() else "unknown"
        print(f"  Board: {info['board']}")
    else:
        info["board"] = "unknown"
    
    return info


def check_nvargus_daemon() -> dict:
    """Check nvargus-daemon status."""
    section("nvargus-daemon (CSI Camera Service)")
    info = {"running": False, "errors": []}
    
    rc, out, err = run("systemctl is-active nvargus-daemon 2>/dev/null")
    if rc == 0:
        info["running"] = True
        status("nvargus-daemon is RUNNING", True)
    else:
        status("nvargus-daemon is NOT running", False)
        info["errors"].append("nvargus-daemon not active")
    
    # Check for known error patterns in recent journal
    rc, out, err = run("journalctl -u nvargus-daemon --no-pager -n 20 2>/dev/null", timeout=15)
    if "ResourceError" in out or "ResourceError" in err:
        info["errors"].append("nvargus-daemon ResourceError (BW Ioctl FD)")
        status("nvargus-daemon shows ResourceError", False, "known JetPack 6.x issue — restart needed")
    elif "Error" in out or "Error" in err:
        info["errors"].append("nvargus-daemon reports errors")
        status("nvargus-daemon has errors", False, "check journalctl")
    else:
        status("nvargus-daemon journal looks clean", True)
    
    # Check argus socket
    if Path("/tmp/argus_socket").exists():
        status("Argus socket exists", True)
    else:
        status("Argus socket missing", False)
        info["errors"].append("no argus_socket")
    
    return info


def check_video_devices() -> dict:
    """Scan /dev/video* and identify each."""
    section("Video Devices (/dev/video*)")
    
    devices = sorted(Path("/dev").glob("video*"))
    info = {"devices": {}, "count": len(devices)}
    
    if not devices:
        status("No /dev/video devices found", False, "camera may not be connected or driver not loaded")
        return info
    
    for dev in devices:
        dev_name = str(dev)
        rc, out, err = run(f"v4l2-ctl --device={dev_name} --all 2>/dev/null", timeout=5)
        cap = "unknown"
        if "Driver name" in out:
            for line in out.splitlines():
                if "Driver name" in line:
                    cap = line.split(":")[-1].strip()
                    break
        info["devices"][dev_name] = cap
        status(f"{dev_name}", True, cap)
    
    return info


def probe_gstreamer() -> dict:
    """Test which GStreamer pipelines work."""
    section("GStreamer Pipeline Tests")
    info = {"pipelines": {}}
    
    tests = [
        ("nvarguscamerasrc", 
         "gst-launch-1.0 nvarguscamerasrc sensor-id=0 num-buffers=1 ! nvvidconv ! video/x-raw, format=BGRx ! fakesink -v 2>&1"),
        ("nvv4l2camerasrc", 
         "gst-launch-1.0 nvv4l2camerasrc device=/dev/video0 num-buffers=1 ! video/x-raw(memory:NVMM), format=NV12 ! fakesink -v 2>&1"),
        ("v4l2src", 
         "gst-launch-1.0 v4l2src device=/dev/video0 num-buffers=1 ! video/x-raw, format=YUY2, width=640, height=480 ! fakesink -v 2>&1"),
    ]
    
    for name, cmd in tests:
        rc, out, err = run(cmd, timeout=10)
        combined = out + err
        
        if "Internal data stream error" in combined:
            status(f"Pipeline: {name}", False, "sensor not detected")
            info["pipelines"][name] = {"ok": False, "reason": "sensor_not_detected"}
        elif "erroneous pipeline" in combined:
            status(f"Pipeline: {name}", False, "element not available or misconfigured")
            info["pipelines"][name] = {"ok": False, "reason": "bad_pipeline"}
        elif "Cannot identify" in combined:
            status(f"Pipeline: {name}", False, "/dev/video0 not found")
            info["pipelines"][name] = {"ok": False, "reason": "no_device"}
        elif "EOS" in combined or rc == 0:
            status(f"Pipeline: {name}", True, "captured frames successfully")
            info["pipelines"][name] = {"ok": True}
        else:
            status(f"Pipeline: {name}", False, "unknown failure — see verbose mode")
            info["pipelines"][name] = {"ok": False, "reason": "unknown"}
    
    return info


def check_i2c_camera() -> dict:
    """Scan I2C buses for camera sensor addresses."""
    section("I2C Camera Sensor Scan")
    info = {"found": False, "details": []}
    
    # Common camera sensor addresses: 0x10 (IMX219), 0x1a (IMX477), 0x36 (ov5693), 0x48, 0x64
    camera_addrs = {0x10: "IMX219", 0x1a: "IMX477", 0x36: "OV5693", 0x48: "Generic", 0x64: "Generic"}
    
    buses = [7, 8, 9, 2, 0]  # Typical camera buses on Jetson
    any_found = False
    
    for bus in buses:
        rc, out, err = run(f"i2cdetect -y -r {bus} 2>/dev/null | grep -E '^[0-9a-f]{2}:'", timeout=5)
        if rc != 0:
            continue
        
        for line in out.splitlines():
            addr_str = line[:2]  # first hex byte of line
            try:
                addr = int(addr_str, 16)
            except ValueError:
                continue
            
            if addr in camera_addrs:
                found_str = f"Bus {bus}, addr 0x{addr:02x} ({camera_addrs[addr]})"
                info["details"].append(found_str)
                status(f"Camera sensor detected", True, found_str)
                any_found = True
    
    if not any_found:
        status("No camera sensor detected on I2C", False, "camera may not be plugged in")
    
    info["found"] = any_found
    return info


def print_recommendations(board: dict, argus: dict, video: dict, gst: dict, i2c: dict) -> None:
    section("RECOMMENDATIONS")
    
    issues = []
    fixes = []
    
    if not i2c["found"]:
        issues.append("No camera sensor found on I2C buses")
        fixes.append("1. PHYSICALLY CONNECT a CSI camera module (IMX219, IMX477) to the Jetson camera connector")
        fixes.append("   Make sure the flex cable is seated properly and locked in.")
    
    if not argus["running"]:
        issues.append("nvargus-daemon not running")
        fixes.append("2. Restart the camera daemon:    sudo systemctl restart nvargus-daemon")
    
    if argus["errors"]:
        for e in argus["errors"]:
            if "ResourceError" in e:
                issues.append("nvargus-daemon ResourceError (JetPack 6.x bug)")
                fixes.append("3. Known JP6.x fix — run as root once:")
                fixes.append("   sudo bash -c 'echo on > /sys/devices/platform/power/override'")
                fixes.append("   sudo systemctl restart nvargus-daemon")
    
    if video["count"] == 0:
        issues.append("No /dev/video devices")
        fixes.append("4. If camera IS connected, enable the overlay in boot config:")
        fixes.append("   sudo nano /boot/extlinux/extlinux.conf")
        fixes.append("   Add to APPEND: Jetson.IMX477=1  (or Jetson.IMX219=1)")
        fixes.append("   Then reboot: sudo reboot")
    
    if not any(p["ok"] for p in gst["pipelines"].values()):
        issues.append("No working GStreamer pipeline")
        fixes.append("5. VisionNode will fallback to FakeCamera mode (simulated frames for dev)")
    
    if not issues:
        print("✅ All checks passed! Your camera should work with VisionNode.")
        print("   Recommended pipeline: nvarguscamerasrc")
        return
    
    print("❌ Issues found:")
    for i in issues:
        print(f"   • {i}")
    print("")
    print("Fixes (run in terminal):")
    for f in fixes:
        print(f"   {f}")
    print("")
    print("💡 If you don't have a physical CSI camera yet, VisionNode supports:")
    print("   --mock-mode : Simulated frames for headless development")
    print("   --usb       : Use a USB webcam via /dev/video0")


def main():
    parser = argparse.ArgumentParser(description="Teela Camera Probe — hardware diagnostics")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full command output")
    args = parser.parse_args()
    
    banner("TEELA CAMERA PROBE")
    print("Checking your Jetson's camera hardware...")
    
    board = check_jetson_model()
    argus = check_nvargus_daemon()
    video = check_video_devices()
    gst = probe_gstreamer()
    i2c = check_i2c_camera()
    
    print_recommendations(board, argus, video, gst, i2c)
    
    print("")
    print("=" * 50)
    print("  Probe complete.")
    print("=" * 50)


if __name__ == "__main__":
    main()
