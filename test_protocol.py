import requests

ACCESS_TOKEN = "at.9tgs48932nzul5hj075hojhz2zivgeah-4qrvqzjd7e-10qfuu7-ngqqvdizw"
DEVICE_SN    = "J83082531"
BASE_URL     = "https://isgpopen.ezvizlife.com"

for p in [1, 2, 3, 4, 5, 6]:
    r = requests.post(f"{BASE_URL}/api/lapp/live/address/get", data={
        "accessToken":  ACCESS_TOKEN,
        "deviceSerial": DEVICE_SN,
        "channelNo":    1,
        "protocol":     p,
        "quality":      2,
    })
    url = r.json().get('data', {}).get('url', 'N/A')
    print(f"Protocol {p}: {url[:80]}")