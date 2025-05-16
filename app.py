from flask import Flask, render_template, request, session, redirect, url_for
import sys, io, os, re
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI

# Flask 앱 설정
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv('FLASK_SECRET', 'your_secret_key')

# 절대경로로 DB 위치 지정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'app.db')

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            university TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            problem TEXT,
            code TEXT,
            output TEXT,
            is_correct BOOLEAN,
            timestamp TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        ''')

# 환경변수 로드 및 OpenAI 클라이언트 설정
load_dotenv(os.path.join(BASE_DIR, '.env'))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Routes ---

@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', problem=None, user=session['user'], category=None, difficulty=None)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
        if user:
            session['user'] = dict(user)
            return redirect(url_for('index'))
        return render_template('login.html', error="아이디 또는 비밀번호가 올바르지 않습니다.")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        name = request.form['name']
        university = request.form['university']
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return render_template('register.html', error="이미 사용 중인 아이디입니다.")
        db.execute(
            "INSERT INTO users (username, password, name, university) VALUES (?, ?, ?, ?)",
            (username, password, name, university)
        )
        db.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# GPT 문제 생성 함수
def get_random_for_problem(category="for문", difficulty="초급"):
    prompt = f"Python의 '{category}' 개념을 연습할 수 있는 {difficulty} 난이도의 문제를 하나 만들어줘."
    response = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()

def parse_problem_response(text):
    prob_match = re.search(r"### 문제(?: 설명)?[:]?\s*(.+?)(?=### 정답 코드)", text, re.DOTALL)
    code_match = re.search(r"### 정답 코드[:]?\s*(.+?)(?=### 정답 출력값)", text, re.DOTALL)
    out_match = re.search(r"### 정답 출력값[:]?\s*(.+)", text, re.DOTALL)
    return {
        'problem': prob_match.group(1).strip() if prob_match else text.strip(),
        'correct_code': code_match.group(1).strip() if code_match else '',
        'correct_output': out_match.group(1).strip() if out_match else ''
    }

# 논리 채점 함수
def ask_gpt_is_logically_correct(problem, user_code, user_output, correct_code, correct_output):
    prompt = f"문제 설명:\n{problem}\n" + \
             f"GPT 정답 코드:\n{correct_code}\n" + \
             f"GPT 예상 출력:\n{correct_output}\n" + \
             f"사용자 코드:\n{user_code}\n" + \
             f"사용자 출력 결과:\n{user_output}\n" + \
             "위 코드가 문제를 정확히 해결했으면 '정답입니다', 아니면 '오답입니다'라고만 답하세요."
    response = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5,
    )
    return response.choices[0].message.content.strip()

# 문제 생성 라우트
@app.route('/generate', methods=['POST'])
def generate():
    if 'user' not in session:
        return redirect(url_for('login'))
    category = request.form.get('category', 'for문')
    difficulty = request.form.get('difficulty', '초급')
    raw = get_random_for_problem(category, difficulty)
    parsed = parse_problem_response(raw)
    session['current_answer'] = parsed
    return render_template('index.html', problem=parsed['problem'], user=session['user'], category=category, difficulty=difficulty)

# 제출 및 채점 라우트
@app.route('/submit', methods=['POST'])
def submit():
    if 'user' not in session:
        return redirect(url_for('login'))
    parsed = session.get('current_answer')
    if not parsed:
        return redirect(url_for('index'))
    user_code = request.form.get('code', '')
    if not user_code.strip():
        return render_template('result.html', result="❗ 코드가 입력되지 않았습니다.", is_correct=False, correct_code=parsed['correct_code'], correct_output=parsed['correct_output'], problem=parsed['problem'], code="", gpt_judgement="코드가 입력되지 않아 채점할 수 없습니다.", user=session['user'])
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        exec(user_code, {})
        user_out = sys.stdout.getvalue().strip()
    except Exception as e:
        user_out = f"오류 발생: {e}"
    sys.stdout = old_stdout
    gpt_judgement = ask_gpt_is_logically_correct(parsed['problem'], user_code, user_out, parsed['correct_code'], parsed['correct_output'])
    is_correct = "정답" in gpt_judgement
    db = get_db()
    db.execute("INSERT INTO history (user_id, problem, code, output, is_correct, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (session['user']['id'], parsed['problem'], user_code, user_out, int(is_correct), datetime.now().isoformat()))
    db.commit()
    return render_template('result.html', problem=parsed['problem'], code=user_code, result=user_out, is_correct=is_correct, correct_code=parsed['correct_code'], correct_output=parsed['correct_output'], gpt_judgement=gpt_judgement, user=session['user'])

# 사용자 히스토리 라우트
@app.route('/history')
def history():
    if 'user' not in session:
        return redirect(url_for('login'))
    db = get_db()
    records = db.execute("SELECT * FROM history WHERE user_id=? ORDER BY id DESC", (session['user']['id'],)).fetchall()
    total = len(records)
    correct = len([r for r in records if r['is_correct']])
    return render_template('history.html', user=session['user'], records=records, total=total, correct=correct)

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
