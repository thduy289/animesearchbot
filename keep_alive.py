import os
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Đang hoạt động..."

def run():
    # Quan trọng: Render cấp PORT nào thì dùng PORT đó, nếu không có mới dùng 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
