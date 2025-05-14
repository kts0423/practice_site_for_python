from flask import Flask, render_template, request, session, redirect, url_for
import sys, io, os, re
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI

# Flask 앱 설정
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv('FLASK_SECRET', 'your_secret_key')

# 데이터베이스 파일 절대경로
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
            student_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT 0
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

def add_admin_column_if_missing():
    db = get_db()
    try:
        db.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

# 환경변수 로드
load_dotenv(os.path.join(BASE_DIR, '.env'))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 관리자 권한 확인
def is_admin():
    return session.get('user', {}).get('is_admin', False)

# 메인 페이지
@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    if is_admin():
        return redirect(url_for('admin_user_list'))
    return render_template('index.html', problem=None, user=session['user'], category=None, difficulty=None)

# 로그인
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        student_id = request.form['student_id']
        name = request.form['name']
        is_adm = 'is_admin' in request.form
        db = get_db()
        if is_adm:
            user = db.execute(
                "SELECT * FROM users WHERE student_id=? AND name=? AND is_admin=1",
                (student_id, name)
            ).fetchone()
        else:
            user = db.execute(
                "SELECT * FROM users WHERE student_id=? AND name=?",
                (student_id, name)
            ).fetchone()
        if user:
            session['user'] = dict(user)
            return redirect(url_for('admin_user_list' if is_adm else 'index'))
        return render_template('login.html', error="회원 정보가 올바르지 않습니다.")
    return render_template('login.html')

# 로그아웃
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# 회원가입
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        student_id = request.form['student_id']
        name = request.form['name']
        is_adm = 'is_admin' in request.form
        if not student_id.isdigit():
            return render_template('register.html', error="학번은 숫자여야 합니다.")
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE student_id=?", (student_id,)).fetchone():
            return render_template('register.html', error="이미 등록된 학번입니다.")
        db.execute(
            "INSERT INTO users (student_id, name, is_admin) VALUES (?, ?, ?)",
            (student_id, name, int(is_adm))
        )
        db.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

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
    out_match  = re.search(r"### 정답 출력값[:]?\s*(.+)", text, re.DOTALL)
    return {
        'problem': prob_match.group(1).strip()   if prob_match else text.strip(),
        'correct_code': code_match.group(1).strip() if code_match else '',
        'correct_output': out_match.group(1).strip() if out_match  else ''
    }

def ask_gpt_is_logically_correct(problem, user_code, user_output, correct_code, correct_output):
    prompt = (
        f"문제 설명:\n{problem}\nGPT 정답 코드:\n{correct_code}\n"
        f"GPT 출력:\n{correct_output}\n사용자 코드:\n{user_code}\n"
        f"사용자 출력:\n{user_output}\n"
        "이 코드가 문제를 정확히 해결했는지 알려줘."
    )
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
    user_code = request.form.get('code','')
    old_stdout = sys.stdout; sys.stdout = io.StringIO()
    try:
        exec(user_code, {})
        user_out = sys.stdout.getvalue().strip()
    except Exception as e:
        user_out = f"오류 발생: {e}"
    sys.stdout = sys.__stdout__
    judgment = ask_gpt_is_logically_correct(parsed['problem'], user_code, user_out, parsed['correct_code'], parsed['correct_output'])
    is_correct = '정답' in judgment
    db = get_db()
    db.execute(
        "INSERT INTO history (user_id, problem, code, output, is_correct, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (session['user']['id'], parsed['problem'], user_code, user_out, int(is_correct), datetime.now().isoformat())
    )
    db.commit()
    return render_template('result.html', problem=parsed['problem'], code=user_code, result=user_out, is_correct=is_correct, correct_code=parsed['correct_code'], correct_output=parsed['correct_output'], gpt_judgement=judgment, user=session['user'])

# 개인 히스토리
@app.route('/history')
def history():
    if 'user' not in session:
        return redirect(url_for('login'))
    db = get_db()
    records = db.execute("SELECT * FROM history WHERE user_id=? ORDER BY id DESC", (session['user']['id'],)).fetchall()
    total = len(records); correct = len([r for r in records if r['is_correct']])
    return render_template('history.html', user=session['user'], records=records, total=total, correct=correct)

# 관리자: 사용자 목록
@app.route('/admin/users')
def admin_user_list():
    if not is_admin(): return redirect(url_for('login'))
    db = get_db(); users = db.execute("SELECT id,name,student_id FROM users WHERE is_admin=0 ORDER BY name").fetchall()
    return render_template('admin_users.html', users=users, user=session['user'])

# 관리자: 사용자 날짜 목록
@app.route('/admin/user/<int:user_id>/dates')
def admin_user_dates(user_id):
    if not is_admin(): return redirect(url_for('login'))
    db=get_db(); info=db.execute("SELECT name FROM users WHERE id=?",(user_id,)).fetchone()
    dates=db.execute("SELECT DISTINCT date(timestamp) as date FROM history WHERE user_id=? ORDER BY date DESC",(user_id,)).fetchall()
    return render_template('admin_user_dates.html', user_id=user_id, user_name=info['name'], dates=dates, user=session['user'])

# 관리자: 특정 사용자 특정 날짜 히스토리
@app.route('/admin/user/<int:user_id>/history/<date>')
def admin_user_history_by_date(user_id, date):
    if not is_admin(): return redirect(url_for('login'))
    db=get_db(); info=db.execute("SELECT name FROM users WHERE id=?",(user_id,)).fetchone()
    recs=db.execute("SELECT * FROM history WHERE user_id=? AND date(timestamp)=? ORDER BY id DESC",(user_id, date)).fetchall()
    return render_template('admin_user_history.html', user_name=info['name'], date=date, records=recs, user=session['user'])

if __name__ == '__main__':
    init_db(); add_admin_column_if_missing(); app.run(debug=True)
