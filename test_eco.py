from datetime import datetime
import time

# ====================================================================
#  TEST CHẾ ĐỘ ECO — HẸN GIỜ BẬT/TẮT (AM/PM giống web)
# ====================================================================

def get_hour():
    while True:
        try:
            h = int(input("    Giờ (1-12): "))
            if 1 <= h <= 12: return h
            print("    ⚠️  Nhập 1-12!")
        except ValueError:
            print("    ⚠️  Nhập số nguyên!")

def get_minute():
    while True:
        try:
            m = int(input("    Phút (0-59): "))
            if 0 <= m <= 59: return m
            print("    ⚠️  Nhập 0-59!")
        except ValueError:
            print("    ⚠️  Nhập số nguyên!")

def get_ampm():
    while True:
        ap = input("    AM/PM (a=AM, p=PM): ").strip().lower()
        if ap == 'a': return 'AM'
        if ap == 'p': return 'PM'
        print("    ⚠️  Nhập 'a' hoặc 'p'!")

def to_24h(h, m, ap):
    if ap == 'AM' and h == 12: h = 0
    if ap == 'PM' and h != 12: h += 12
    return h, m

def fmt(h, m):      return f"{h:02d}:{m:02d}"
def fmt12(h,m,ap):  return f"{h:02d}:{m:02d} {ap}"

def main():
    print("=" * 45)
    print("   CHẾ ĐỘ ECO — HẸN GIỜ BẬT / TẮT QUẠT")
    print("=" * 45)
    print(f"   Thời gian hiện tại: {datetime.now().strftime('%H:%M:%S')}")
    print("-" * 45)

    print("\n🟢 GIỜ BẬT:")
    bat_h, bat_m, bat_ap = get_hour(), get_minute(), get_ampm()
    b24h, b24m = to_24h(bat_h, bat_m, bat_ap)

    print("\n🔴 GIỜ TẮT:")
    tat_h, tat_m, tat_ap = get_hour(), get_minute(), get_ampm()
    t24h, t24m = to_24h(tat_h, tat_m, tat_ap)

    start_min = b24h * 60 + b24m
    stop_min  = t24h * 60 + t24m

    if start_min >= stop_min:
        print("\n⚠️  Giờ tắt phải sau giờ bật! Thoát.")
        return

    print("\n" + "=" * 45)
    print(f"   ✅ Lịch trình:")
    print(f"   BẬT : {fmt12(bat_h,bat_m,bat_ap)}  →  {fmt(b24h,b24m)} (24h)")
    print(f"   TẮT : {fmt12(tat_h,tat_m,tat_ap)}  →  {fmt(t24h,t24m)} (24h)")
    print(f"   Nhấn Ctrl+C để dừng")
    print("=" * 45 + "\n")

    fan_on = False
    try:
        while True:
            now     = datetime.now()
            now_min = now.hour * 60 + now.minute
            now_str = now.strftime('%H:%M:%S')
            in_sch  = start_min <= now_min < stop_min

            if in_sch and not fan_on:
                fan_on = True
                print(f"\n[{now_str}] ✅ QUẠT BẬT — {fmt12(bat_h,bat_m,bat_ap)} → {fmt12(tat_h,tat_m,tat_ap)}")
            elif not in_sch and fan_on:
                fan_on = False
                print(f"\n[{now_str}] ⛔ QUẠT TẮT — {fmt12(bat_h,bat_m,bat_ap)} → {fmt12(tat_h,tat_m,tat_ap)}")
            else:
                st = "🟢 ĐANG CHẠY" if fan_on else "🔴 ĐANG TẮT"
                print(f"[{now_str}] {st}", end='\r')

            time.sleep(5)

    except KeyboardInterrupt:
        print(f"\n\n[{datetime.now().strftime('%H:%M:%S')}] ⛔ Dừng — QUẠT TẮT")

if __name__ == '__main__':
    main()