import requests

# ====== CẤU HÌNH ======
APP_KEY      = "24f485c8937b41c59354b53bd9bfbeec"  # AppKey đầy đủ của bạn
ACCESS_TOKEN = "at.7sgmy75zcd8biv471ofbq0tzciu91xzt-3624rt6whm-12zfhhw-mvo2c6vza"   # Copy từ console (bấm hiện ra)
DEVICE_SN    = "J83082531"
CHANNEL      = 1
BASE_URL = "https://isgpopen.ezvizlife.com"

# Map hướng sang mã EZVIZ API
DIR_MAP = {"UP": 0, "DOWN": 1, "LEFT": 2, "RIGHT": 3}

def ptz_start(direction, speed=2):
    url = f"{BASE_URL}/api/lapp/device/ptz/start"
    r = requests.post(url, data={
        "accessToken":  ACCESS_TOKEN,
        "deviceSerial": DEVICE_SN,
        "channelNo":    CHANNEL,
        "direction":    DIR_MAP[direction],
        "speed":        speed
    })
    print(f"Status: {r.status_code}")
    print(f"Body: {r.text}")   # ← đổi từ r.json() thành r.text

def ptz_stop(direction):
    url = f"{BASE_URL}/api/lapp/device/ptz/stop"
    r = requests.post(url, data={
        "accessToken":  ACCESS_TOKEN,
        "deviceSerial": DEVICE_SN,
        "channelNo":    CHANNEL,
        "direction":    DIR_MAP[direction],
    })
    print(f"Status: {r.status_code}")
    print(f"Body: {r.text}")   # ← đổi từ r.json() thành r.text

# Test xoay phải 2 giây
import time
ptz_start("RIGHT", speed=2)
time.sleep(2)
ptz_stop("RIGHT")