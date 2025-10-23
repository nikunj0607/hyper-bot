# bot.py (paper mode)
import time, datetime

def run_bot():
    while True:
        print(f"[{datetime.datetime.utcnow().isoformat()}] Hyper tick… (paper)")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
