# run.py
import webbrowser
from app import app

if __name__ == '__main__':
    webbrowser.open("http://127.0.0.1:5000")  # 브라우저 자동 실행
    app.run()
