# 📷 CSI Camera Setup Guide for Teela

This guide explains how to enable a CSI camera on your NVIDIA Jetson so Teela's VisionNode can detect and use it.

---

## 🔍 Before You Start: Check Current State

```bash
# Run the diagnostic probe
python teela/utils/ProbeCamera.py
```

If you see:
- `No /dev/video devices found`
- `No camera sensor detected on I2C`

Then your camera is not yet enabled in the device tree. Follow this guide.

---

## 🛠️ Method 1: NVIDIA Jetson-IO Tool (Recommended)

This is the easiest and safest way to enable camera support.

### Step 1: Launch the tool

```bash
sudo /opt/nvidia/jetson-io/jetson-io.py
```

### Step 2: Enable your camera overlay

In the text UI (use **Arrow Keys** + **Enter**):

1. Select **"Configure 40-pin Expansion Header"**
2. Scroll to your camera model:
   - **IMX219** — Raspberry Pi Camera Module v2 (8MP)
   - **IMX477** — Raspberry Pi Camera Module 3 / HQ Camera (12MP)
   - **OV5693** — Generic 5MP module
3. Press **Space** to enable it (`*` appears)
4. Press **S** to **Save pin configuration**
5. Press **Back** → select **"Reboot Now"** (or exit and reboot manually)

### Step 3: Reboot

```bash
sudo reboot
```

### Step 4: Verify

After the Jetson comes back:

```bash
# Camera device should now exist
ls /dev/video*
# Expected: /dev/video0

# I2C should detect the sensor
i2cdetect -y -r 7
# Expected: address 0x10 for IMX219, 0x1a for IMX477

# Run diagnostics again
python teela/utils/ProbeCamera.py
```

---

## 🛠️ Method 2: Manual Overlay in extlinux.conf

If `jetson-io.py` is not available on your Jetson version:

### Step 1: Edit boot config

```bash
sudo nano /boot/extlinux/extlinux.conf
```

### Step 2: Add camera overlay

Find the line starting with `APPEND` and add the camera parameter:

**For IMX219 (Raspberry Pi Cam v2):**
```
APPEND ${cbootargs} root=/dev/mmcblk0p1 rw rootwait rootfstype=ext4 ... Jetson.IMX219=1
```

**For IMX477 (HQ Camera):**
```
APPEND ${cbootargs} root=/dev/mmcblk0p1 rw rootwait rootfstype=ext4 ... Jetson.IMX477=1
```

### Step 3: Save and reboot

```bash
sudo reboot
```

---

## 🛠️ Method 3: Re-Flash with SDK Manager

If neither method works, your Jetson may need a full re-flash with the camera-enabled device tree:

1. Download [NVIDIA SDK Manager](https://developer.nvidia.com/sdk-manager)
2. Flash JetPack with **"Jetson Runtime Components"** and **"Jetson SDK Components"**
3. During setup, check the box for your camera module

---

## ✅ Post-Setup: Start VisionNode

Once `/dev/video0` exists and `ProbeCamera.py` reports success:

```bash
# Restart nvargus (fixes JetPack 6.x BW Ioctl issue)
sudo systemctl restart nvargus-daemon

# Launch VisionNode with CSI camera
python teela/VisionNode.py --mode csi --sensor-id 0 --display
```

---

## 🔌 Physical Connection Check

If you've enabled the overlay but still see no camera:

| Issue | Fix |
|-------|-----|
| **Camera not detected on I2C** | Check the **flex ribbon cable** is fully inserted and locked |
| **No /dev/video** | Make sure the blue side of the cable faces the GPIO pins (for standard carriers) |
| **Static image / glitch** | Try a shorter cable or lower resolution (`--width 640 --height 480`) |
| **Wrong orientation** | Use `--flip-method 2` (180° rotation) if image is upside-down |

### Jetson Orin NX Carrier Board CSI Connector

```
Carrier Board
┌─────────────────────────┐
│  (...)                  │
│                         │
│  CSI Camera Connector   │ ← Flat flex cable goes here
│  ┌───────────────┐     │
│  │ ▓▓▓▓▓▓▓▓▓▓▓▓▓ │     │
│  └───────────────┘     │
│  [lock]               │ ← Push white tab down to lock cable
│                         │
│  (...) GPIO pins       │
└─────────────────────────┘
```

1. **Lift** the white plastic lock tab
2. **Insert** the flex cable with contacts facing the board
3. **Push down** the lock tab firmly
4. The cable should not wiggle

---

## 🧪 Still Not Working?

Run the diagnostic and save the output:

```bash
python teela/utils/ProbeCamera.py --verbose > camera_diagnosis.txt 2>&1
```

Then open an issue on GitHub with `camera_diagnosis.txt` attached.

---

## 📋 Quick Reference

| Camera | Overlay Name | I2C Address | `sensor-id` |
|--------|-------------|-------------|-------------|
| Raspberry Pi Cam v2 (IMX219) | `Jetson.IMX219=1` | `0x10` | 0 |
| Raspberry Pi Cam 3 (IMX708) | `Jetson.IMX708=1` | `0x1a` | 0 |
| Arducam IMX477 | `Jetson.IMX477=1` | `0x1a` | 0 |
| Generic 5MP OV5693 | `Jetson.OV5693=1` | `0x36` | 0 |

---

## 🎬 Next Steps

After camera is detected:

1. **Test VisionNode**: `python teela/VisionNode.py --mode csi --display`
2. **Launch full Teela**: See `teela/README.md` for 3-terminal startup
3. **Run calibration**: Say "calibrate" to the voice system

---

*Colornosteela-cloud — Teela Robotics 2026*
