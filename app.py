import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from groq import APIConnectionError, APIStatusError, Groq
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from quiz_parser import parse_pasted_questions


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE = BASE_DIR / "instance" / "quiz.sqlite3"
UPLOAD_DIR = BASE_DIR / "instance" / "uploads"
ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
QUIZ_MINUTES = 25

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "replace-this-development-secret"),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)


def db_connection():
    if "db" not in g:
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def initialize_database():
    connection = db_connection()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('teacher', 'student')),
            name TEXT NOT NULL,
            enrollment TEXT UNIQUE,
            batch TEXT,
            photo TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            teacher_name TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL DEFAULT 25,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
            show_answers INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('blank', 'mcq')),
            prompt TEXT NOT NULL,
            answer_key TEXT NOT NULL DEFAULT '',
            options TEXT NOT NULL DEFAULT '[]',
            marks REAL NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
            student_id INTEGER NOT NULL REFERENCES users(id),
            started_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            submitted_at TEXT,
            score REAL NOT NULL DEFAULT 0,
            total_marks REAL NOT NULL DEFAULT 0,
            focus_violation INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'in_progress',
            tab_switches INTEGER NOT NULL DEFAULT 0,
            resume_requested INTEGER NOT NULL DEFAULT 0,
            resume_position INTEGER NOT NULL DEFAULT 0,
            UNIQUE (quiz_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            response TEXT NOT NULL DEFAULT '',
            marks_awarded REAL NOT NULL DEFAULT 0,
            UNIQUE (attempt_id, question_id)
        );
        """
    )
    quiz_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(quizzes)")
    }
    if "show_answers" not in quiz_columns:
        connection.execute(
            "ALTER TABLE quizzes ADD COLUMN show_answers INTEGER NOT NULL DEFAULT 0"
        )
    attempt_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(attempts)")
    }
    attempt_migrations = {
        "focus_violation": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'in_progress'",
        "tab_switches": "INTEGER NOT NULL DEFAULT 0",
        "resume_requested": "INTEGER NOT NULL DEFAULT 0",
        "resume_position": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in attempt_migrations.items():
        if column not in attempt_columns:
            connection.execute(f"ALTER TABLE attempts ADD COLUMN {column} {definition}")
    connection.execute(
        "UPDATE attempts SET status = 'submitted' WHERE submitted_at IS NOT NULL AND status = 'in_progress'"
    )
    teacher = connection.execute(
        "SELECT id FROM users WHERE username = 'teach'"
    ).fetchone()
    if teacher is None:
        connection.execute(
            """INSERT INTO users (username, password_hash, role, name, created_at)
               VALUES (?, ?, 'teacher', ?, ?)""",
            (
                "teach",
                generate_password_hash(os.getenv("ADMIN_PASSWORD", "teach123")),
                "Teacher Admin",
                utc_now(),
            ),
        )
    connection.commit()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    return datetime.fromisoformat(value)


def current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db_connection().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if role and user["role"] != role:
                abort(403)
            g.user = user
            return view(*args, **kwargs)

        return wrapped

    return decorator


def normalize_answer(value):
    return re.sub(r"\s+", " ", value.strip().casefold())


@app.template_filter("from_json")
def from_json(value):
    return json.loads(value or "[]")


def groq_json_completion(messages, timeout):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None, "GROQ_API_KEY is not configured in .env."

    try:
        completion = Groq(api_key=api_key, timeout=timeout).chat.completions.create(
            model=os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content
        if not content:
            return None, "Groq returned an empty response."
        return json.loads(content), None
    except APIStatusError as error:
        return None, f"Groq rejected the request (HTTP {error.status_code})."
    except (APIConnectionError, TimeoutError):
        return None, "The app could not reach api.groq.com."
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None, "Groq returned an unreadable answer response."


def generate_answer_suggestions(prompts, subject):
    if not prompts:
        return {}, None

    generated, error = groq_json_completion(
        [
            {
                "role": "system",
                "content": (
                    "Answer each quiz question accurately and concisely. Return "
                    "only a JSON object with an answers array; each item must "
                    "contain the original integer index and a short answer string. "
                    "Do not include explanations unless the question asks for one."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "subject": subject,
                        "questions": [
                            {"index": index, "question": prompt}
                            for index, prompt in enumerate(prompts)
                        ],
                    }
                ),
            },
        ],
        timeout=20,
    )
    if error:
        return {}, error
    answer_items = generated.get("answers", []) if isinstance(generated, dict) else []
    suggestions = {}
    if isinstance(answer_items, list):
        for item in answer_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            answer = str(item.get("answer", "")).strip()
            if 0 <= index < len(prompts) and answer:
                suggestions[index] = answer[:500]
    return suggestions, None


def score_short_answers(questions, response_map):
    scores = {}
    candidates = []
    for question in questions:
        if question["kind"] != "blank":
            continue
        response = response_map.get(str(question["id"]), "").strip()
        expected = question["answer_key"].strip()
        score = 0.0
        if response and expected and normalize_answer(response) == normalize_answer(expected):
            score = float(question["marks"])
        elif response and expected:
            candidates.append(
                {
                    "index": len(candidates),
                    "question": question["prompt"],
                    "expected_answer": expected,
                    "student_answer": response,
                    "maximum_marks": float(question["marks"]),
                    "question_id": question["id"],
                }
            )
        scores[question["id"]] = score

    if not candidates:
        return scores

    judged, error = groq_json_completion(
        [
            {
                "role": "system",
                "content": (
                    "Grade each student's short answer against its expected answer. "
                    "Award a number from 0 to the maximum marks based on semantic "
                    "correctness. Return only JSON in this format: "
                    "{\"scores\":[{\"index\":0,\"score\":number}]}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(candidates),
            },
        ],
        timeout=8,
    )
    if error or not isinstance(judged, dict):
        return scores
    for item in judged.get("scores", []):
        try:
            index = int(item["index"])
            candidate = candidates[index]
            score = float(item["score"])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        scores[candidate["question_id"]] = max(
            0.0,
            min(candidate["maximum_marks"], score),
        )
    return scores


def save_photo(file):
    if not file or not file.filename:
        return None
    safe_name = secure_filename(file.filename)
    if "." not in safe_name:
        return None
    extension = safe_name.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_PHOTO_EXTENSIONS:
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{extension}"
    file.save(UPLOAD_DIR / filename)
    return filename


def get_attempt(attempt_id):
    attempt = db_connection().execute(
        "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if attempt is None:
        abort(404)
    if g.user["role"] == "student" and attempt["student_id"] != g.user["id"]:
        abort(403)
    return attempt


def save_attempt_responses(attempt, response_map, resume_position=None):
    connection = db_connection()
    question_ids = {
        str(row["id"])
        for row in connection.execute(
            "SELECT id FROM questions WHERE quiz_id = ?", (attempt["quiz_id"],)
        )
    }
    for question_id, response in response_map.items():
        if str(question_id) not in question_ids:
            continue
        connection.execute(
            """INSERT INTO answers (attempt_id, question_id, response, marks_awarded)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(attempt_id, question_id) DO UPDATE SET response = excluded.response""",
            (
                attempt["id"],
                int(question_id),
                str(response)[:5000],
            ),
        )
    if resume_position is not None:
        connection.execute(
            "UPDATE attempts SET resume_position = ? WHERE id = ?",
            (max(0, int(resume_position)), attempt["id"]),
        )
    connection.commit()


def submit_attempt(attempt, response_map):
    connection = db_connection()
    saved = connection.execute(
        "SELECT question_id, response FROM answers WHERE attempt_id = ?",
        (attempt["id"],),
    ).fetchall()
    merged_responses = {str(row["question_id"]): row["response"] for row in saved}
    merged_responses.update(response_map)
    response_map = merged_responses
    questions = connection.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY position",
        (attempt["quiz_id"],),
    ).fetchall()
    total = sum(float(question["marks"]) for question in questions)
    short_answer_scores = score_short_answers(questions, response_map)
    score = 0.0
    for question in questions:
        response = response_map.get(str(question["id"]), "").strip()
        if question["kind"] == "mcq":
            awarded = (
                float(question["marks"])
                if response == question["answer_key"]
                else 0.0
            )
        else:
            awarded = short_answer_scores.get(question["id"], 0.0)
        score += awarded
        connection.execute(
            """INSERT INTO answers (attempt_id, question_id, response, marks_awarded)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                 response = excluded.response, marks_awarded = excluded.marks_awarded""",
            (attempt["id"], question["id"], response, awarded),
        )
    connection.execute(
          """UPDATE attempts SET submitted_at = ?, score = ?, total_marks = ?,
                        status = 'submitted'
           WHERE id = ?""",
        (utc_now(), score, total, attempt["id"]),
    )
    connection.commit()


@app.route("/")
def index():
    user = current_user()
    if user:
        return redirect(url_for("admin_dashboard" if user["role"] == "teacher" else "student_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db_connection().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("admin_dashboard" if user["role"] == "teacher" else "student_dashboard"))
        flash("That username and password do not match.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        enrollment = request.form.get("enrollment", "").strip()
        batch = request.form.get("batch", "").strip()
        password = request.form.get("password", "")
        photo_file = request.files.get("photo")
        if not all((name, enrollment, batch, password)):
            flash("Complete every field to create your student account.", "error")
        elif len(password) < 8:
            flash("Use a password with at least 8 characters.", "error")
        else:
            photo = None
            if photo_file and photo_file.filename:
                photo = save_photo(photo_file)
            if photo_file and photo_file.filename and not photo:
                flash("Use a JPG, PNG, or WEBP photo under 5 MB.", "error")
            else:
                try:
                    db_connection().execute(
                        """INSERT INTO users
                           (username, password_hash, role, name, enrollment, batch, photo, created_at)
                           VALUES (?, ?, 'student', ?, ?, ?, ?, ?)""",
                        (
                            enrollment,
                            generate_password_hash(password),
                            name,
                            enrollment,
                            batch,
                            photo,
                            utc_now(),
                        ),
                    )
                    db_connection().commit()
                    flash("Enrollment complete. Sign in with your enrollment ID.", "success")
                    return redirect(url_for("login"))
                except sqlite3.IntegrityError:
                    flash("That enrollment ID is already registered.", "error")
    return render_template("register.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/uploads/<path:filename>")
@login_required()
def uploaded_photo(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/student")
@login_required("student")
def student_dashboard():
    connection = db_connection()
    quizzes = connection.execute(
        """SELECT q.*, a.id AS attempt_id, a.submitted_at, a.score, a.total_marks,
                  a.status AS attempt_status, a.resume_requested
           FROM quizzes q LEFT JOIN attempts a
             ON a.quiz_id = q.id AND a.student_id = ?
           WHERE q.status = 'published' ORDER BY q.created_at DESC""",
        (g.user["id"],),
    ).fetchall()
    return render_template("student_dashboard.html", quizzes=quizzes)


@app.post("/quiz/<int:quiz_id>/start")
@login_required("student")
def start_quiz(quiz_id):
    connection = db_connection()
    quiz = connection.execute(
        "SELECT * FROM quizzes WHERE id = ? AND status = 'published'", (quiz_id,)
    ).fetchone()
    if quiz is None:
        abort(404)
    attempt = connection.execute(
        "SELECT * FROM attempts WHERE quiz_id = ? AND student_id = ?",
        (quiz_id, g.user["id"]),
    ).fetchone()
    if attempt is None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=quiz["duration_minutes"])
        cursor = connection.execute(
            """INSERT INTO attempts (quiz_id, student_id, started_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (quiz_id, g.user["id"], now.isoformat(), expires.isoformat()),
        )
        connection.commit()
        attempt_id = cursor.lastrowid
    else:
        if attempt["status"] in {"submitted", "incomplete"}:
            return redirect(url_for("view_result", attempt_id=attempt["id"]))
        attempt_id = attempt["id"]
    return redirect(url_for("take_quiz", attempt_id=attempt_id))


@app.post("/attempt/<int:attempt_id>/save")
@login_required("student")
def save_attempt_progress(attempt_id):
    attempt = get_attempt(attempt_id)
    if attempt["status"] != "in_progress":
        return jsonify(status=attempt["status"]), 409
    data = request.get_json(silent=True) or {}
    responses = data.get("answers", {})
    if not isinstance(responses, dict):
        return jsonify(error="Invalid answer data."), 400
    try:
        position = int(data.get("position", attempt["resume_position"]))
        save_attempt_responses(attempt, responses, position)
    except (TypeError, ValueError):
        return jsonify(error="Invalid progress data."), 400
    return jsonify(status="in_progress", position=position)


@app.post("/attempt/<int:attempt_id>/tab-switch")
@login_required("student")
def record_tab_switch(attempt_id):
    attempt = get_attempt(attempt_id)
    if attempt["status"] != "in_progress":
        return jsonify(status=attempt["status"], tab_switches=attempt["tab_switches"]), 409
    data = request.get_json(silent=True) or {}
    responses = data.get("answers", {})
    if not isinstance(responses, dict):
        responses = {}
    try:
        position = int(data.get("position", attempt["resume_position"]))
    except (TypeError, ValueError):
        position = attempt["resume_position"]
    save_attempt_responses(attempt, responses, position)
    connection = db_connection()
    connection.execute(
        """UPDATE attempts SET tab_switches = tab_switches + 1, focus_violation = 1
           WHERE id = ? AND status = 'in_progress'""",
        (attempt_id,),
    )
    connection.commit()
    updated = connection.execute(
        "SELECT tab_switches FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    strikes = updated["tab_switches"]
    status = "in_progress"
    if strikes >= 3:
        connection.execute(
            """UPDATE attempts SET status = 'incomplete', resume_requested = 0
               WHERE id = ? AND status = 'in_progress'""",
            (attempt_id,),
        )
        connection.commit()
        status = "incomplete"
    return jsonify(status=status, tab_switches=strikes, strikes_remaining=max(0, 3 - strikes))


@app.get("/attempt/<int:attempt_id>/state")
@login_required("student")
def attempt_state(attempt_id):
    attempt = get_attempt(attempt_id)
    return jsonify(
        status=attempt["status"],
        tab_switches=attempt["tab_switches"],
        strikes_remaining=max(0, 3 - attempt["tab_switches"]),
    )


@app.post("/attempt/<int:attempt_id>/request-resume")
@login_required("student")
def request_attempt_resume(attempt_id):
    attempt = get_attempt(attempt_id)
    if attempt["status"] != "incomplete":
        abort(409)
    db_connection().execute(
        "UPDATE attempts SET resume_requested = 1 WHERE id = ?", (attempt_id,)
    )
    db_connection().commit()
    flash("Your request to reopen the incomplete exam was sent to your teacher.", "success")
    return redirect(url_for("view_result", attempt_id=attempt_id))


@app.post("/admin/attempt/<int:attempt_id>/resume")
@login_required("teacher")
def resume_incomplete_attempt(attempt_id):
    connection = db_connection()
    attempt = connection.execute(
        "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if attempt is None:
        abort(404)
    if attempt["status"] != "incomplete":
        abort(409)
    quiz = connection.execute(
        "SELECT duration_minutes FROM quizzes WHERE id = ?", (attempt["quiz_id"],)
    ).fetchone()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=quiz["duration_minutes"])
    connection.execute(
        """UPDATE attempts SET status = 'in_progress', tab_switches = 0,
                  resume_requested = 0, started_at = ?, expires_at = ?, submitted_at = NULL
           WHERE id = ?""",
        (now.isoformat(), expires.isoformat(), attempt_id),
    )
    connection.commit()
    flash("The incomplete exam is reopened with saved answers restored and a fresh timer.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/attempt/<int:attempt_id>", methods=["GET", "POST"])
@login_required("student")
def take_quiz(attempt_id):
    attempt = get_attempt(attempt_id)
    if attempt["status"] == "submitted":
        return redirect(url_for("view_result", attempt_id=attempt_id))
    if attempt["status"] == "incomplete":
        return redirect(url_for("view_result", attempt_id=attempt_id))
    if datetime.now(timezone.utc) >= parse_time(attempt["expires_at"]):
        submit_attempt(attempt, {})
        flash("Time is up. Your attempt has been submitted.", "error")
        return redirect(url_for("view_result", attempt_id=attempt_id))
    if request.method == "POST":
        submit_attempt(
            attempt,
            {
                key.removeprefix("answer_"): value
                for key, value in request.form.items()
                if key.startswith("answer_")
            },
        )
        return redirect(url_for("view_result", attempt_id=attempt_id))

    connection = db_connection()
    quiz = connection.execute(
        "SELECT * FROM quizzes WHERE id = ?", (attempt["quiz_id"],)
    ).fetchone()
    questions = connection.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY position",
        (quiz["id"],),
    ).fetchall()
    saved_answers = {
        str(row["question_id"]): row["response"]
        for row in connection.execute(
            "SELECT question_id, response FROM answers WHERE attempt_id = ?",
            (attempt_id,),
        )
    }
    return render_template(
        "attempt.html",
        attempt=attempt,
        quiz=quiz,
        questions=questions,
        saved_answers=saved_answers,
        resume_position=attempt["resume_position"],
        tab_switches=attempt["tab_switches"],
        remaining_seconds=max(
            0, int((parse_time(attempt["expires_at"]) - datetime.now(timezone.utc)).total_seconds())
        ),
    )


@app.get("/result/<int:attempt_id>")
@login_required()
def view_result(attempt_id):
    attempt = get_attempt(attempt_id)
    quiz = db_connection().execute(
        "SELECT * FROM quizzes WHERE id = ?", (attempt["quiz_id"],)
    ).fetchone()
    if g.user["role"] == "student" and attempt["status"] == "in_progress":
        return redirect(url_for("take_quiz", attempt_id=attempt_id))
    show_review = g.user["role"] == "teacher" or (
        attempt["status"] == "submitted" and bool(quiz["show_answers"])
    )
    answers = []
    if show_review:
        answers = db_connection().execute(
            """SELECT a.*, q.prompt, q.answer_key, q.marks, q.kind
               FROM questions q LEFT JOIN answers a
                 ON a.question_id = q.id AND a.attempt_id = ?
               WHERE q.quiz_id = ? ORDER BY q.position""",
            (attempt_id, attempt["quiz_id"]),
        ).fetchall()
    return render_template(
        "result.html",
        attempt=attempt,
        quiz=quiz,
        answers=answers,
        show_review=show_review,
        )


@app.get("/admin")
@login_required("teacher")
def admin_dashboard():
    connection = db_connection()
    quizzes = connection.execute(
        """SELECT q.*, COUNT(DISTINCT a.id) AS attempts,
                  ROUND(AVG(a.score), 2) AS average_score
           FROM quizzes q LEFT JOIN attempts a ON a.quiz_id = q.id
           GROUP BY q.id ORDER BY q.created_at DESC"""
    ).fetchall()
    marks = connection.execute(
        """SELECT a.*, u.name, u.enrollment, u.batch, u.photo, q.title, q.subject
           FROM attempts a JOIN users u ON u.id = a.student_id
           JOIN quizzes q ON q.id = a.quiz_id
           WHERE a.submitted_at IS NOT NULL OR a.status = 'incomplete'
           ORDER BY a.submitted_at DESC LIMIT 100"""
    ).fetchall()
    return render_template("admin_dashboard.html", quizzes=quizzes, marks=marks)


@app.route("/admin/quizzes/new", methods=["GET", "POST"])
@login_required("teacher")
def create_quiz():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        subject = request.form.get("subject", "").strip()
        teacher_name = request.form.get("teacher_name", "").strip()
        pasted = request.form.get("pasted_questions", "")
        prompts = parse_pasted_questions(pasted)
        mcq_prompts = request.form.getlist("mcq_prompt[]")
        mcq_options = request.form.getlist("mcq_options[]")
        mcq_answers = request.form.getlist("mcq_answer[]")
        mcq_marks = request.form.getlist("mcq_marks[]")
        try:
            default_marks = float(request.form.get("default_marks", "1"))
            default_marks_valid = default_marks >= 0
        except ValueError:
            default_marks = 1.0
            default_marks_valid = False
        if not all((title, subject, teacher_name)):
            flash("Add a quiz name, subject, and teacher name.", "error")
        elif not default_marks_valid:
            flash("Default marks must be a non-negative number.", "error")
        elif not prompts and not any(prompt.strip() for prompt in mcq_prompts):
            flash("Paste at least one question or add an MCQ.", "error")
        else:
            suggested_answers, ai_error = generate_answer_suggestions(prompts, subject)
            connection = db_connection()
            cursor = connection.execute(
                """INSERT INTO quizzes (title, subject, teacher_name, duration_minutes, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (title, subject, teacher_name, QUIZ_MINUTES, g.user["id"], utc_now()),
            )
            quiz_id = cursor.lastrowid
            position = 0
            for index, prompt in enumerate(prompts):
                position += 1
                connection.execute(
                    """INSERT INTO questions
                       (quiz_id, position, kind, prompt, answer_key, marks)
                              VALUES (?, ?, 'blank', ?, ?, ?)""",
                          (quiz_id, position, prompt, suggested_answers.get(index, ""), default_marks),
                )
            for index, prompt in enumerate(mcq_prompts):
                prompt = prompt.strip()
                options = [item.strip() for item in mcq_options[index].split("|") if item.strip()]
                answer = mcq_answers[index].strip()
                if not prompt:
                    continue
                try:
                    marks = max(0.0, float(mcq_marks[index]))
                except (ValueError, IndexError):
                    marks = 1.0
                position += 1
                connection.execute(
                    """INSERT INTO questions (quiz_id, position, kind, prompt, answer_key, options, marks)
                       VALUES (?, ?, 'mcq', ?, ?, ?, ?)""",
                    (quiz_id, position, prompt, answer, json.dumps(options), marks),
                )
            connection.commit()
            if prompts and len(suggested_answers) == len(prompts):
                flash(f"Draft created with {position} questions. AI filled the blank answers; review them and adjust each question's marks.", "success")
            elif prompts and suggested_answers:
                flash(f"Draft created with {position} questions. Some AI answers were filled; complete any blanks, then review each question's marks.", "success")
            elif prompts:
                flash(f"Draft created with {position} questions. AI answers were unavailable: {ai_error or 'request returned no answers'}. Fill them manually; your mark values remain editable.", "error")
            else:
                flash(f"Draft created with {position} questions. Review the answer key and marks before publishing.", "success")
            return redirect(url_for("edit_quiz", quiz_id=quiz_id))
    return render_template("quiz_form.html")


@app.post("/admin/quizzes/<int:quiz_id>/generate-answers")
@login_required("teacher")
def generate_missing_answers(quiz_id):
    connection = db_connection()
    quiz = connection.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        abort(404)
    if quiz["status"] != "draft":
        abort(409)
    missing = connection.execute(
        """SELECT id, prompt FROM questions
           WHERE quiz_id = ? AND kind = 'blank' AND TRIM(answer_key) = ''
           ORDER BY position""",
        (quiz_id,),
    ).fetchall()
    if not missing:
        flash("All fill-in answers already have values.", "success")
        return redirect(url_for("edit_quiz", quiz_id=quiz_id))

    suggestions, ai_error = generate_answer_suggestions(
        [question["prompt"] for question in missing], quiz["subject"]
    )
    for index, answer in suggestions.items():
        if 0 <= index < len(missing):
            connection.execute(
                "UPDATE questions SET answer_key = ? WHERE id = ? AND TRIM(answer_key) = ''",
                (answer, missing[index]["id"]),
            )
    connection.commit()
    if suggestions:
        flash(f"AI filled {len(suggestions)} missing answer(s). Review them and adjust marks per question.", "success")
    else:
        flash(f"AI answers were unavailable: {ai_error or 'request returned no answers'}. You can still enter each answer and mark manually.", "error")
    return redirect(url_for("edit_quiz", quiz_id=quiz_id))


@app.post("/admin/quizzes/<int:quiz_id>/answer-visibility")
@login_required("teacher")
def update_answer_visibility(quiz_id):
    connection = db_connection()
    quiz = connection.execute("SELECT id FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        abort(404)
    show_answers = "1" in request.form.getlist("show_answers")
    connection.execute(
        "UPDATE quizzes SET show_answers = ? WHERE id = ?",
        (int(show_answers), quiz_id),
    )
    connection.commit()
    flash(
        "Students can now review answers after submission."
        if show_answers
        else "Student results now show scores only.",
        "success",
    )
    return redirect(url_for("edit_quiz", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/edit", methods=["GET", "POST"])
@login_required("teacher")
def edit_quiz(quiz_id):
    connection = db_connection()
    quiz = connection.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        abort(404)
    if request.method == "POST":
        if quiz["status"] != "draft":
            abort(409)
        connection.execute(
            "UPDATE quizzes SET title = ?, subject = ?, teacher_name = ? WHERE id = ? AND status = 'draft'",
            (
                request.form.get("title", "").strip(),
                request.form.get("subject", "").strip(),
                request.form.get("teacher_name", "").strip(),
                quiz_id,
            ),
        )
        ids = request.form.getlist("question_id[]")
        prompts = request.form.getlist("prompt[]")
        kinds = request.form.getlist("kind[]")
        answer_keys = request.form.getlist("answer_key[]")
        options_values = request.form.getlist("options[]")
        marks_values = request.form.getlist("marks[]")
        for index, question_id in enumerate(ids):
            prompt = prompts[index].strip()
            kind = kinds[index]
            answer_key = answer_keys[index].strip()
            options = [value.strip() for value in options_values[index].split("|") if value.strip()]
            try:
                marks = max(0.0, float(marks_values[index]))
            except ValueError:
                marks = 0.5
            connection.execute(
                """UPDATE questions SET prompt = ?, kind = ?, answer_key = ?, options = ?, marks = ?
                   WHERE id = ? AND quiz_id = ?""",
                (prompt, kind, answer_key, json.dumps(options), marks, question_id, quiz_id),
            )
        connection.commit()
        flash("Draft changes saved.", "success")
        return redirect(url_for("edit_quiz", quiz_id=quiz_id))
    questions = connection.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY position", (quiz_id,)
    ).fetchall()
    return render_template("quiz_edit.html", quiz=quiz, questions=questions)


@app.post("/admin/quizzes/<int:quiz_id>/publish")
@login_required("teacher")
def publish_quiz(quiz_id):
    connection = db_connection()
    quiz = connection.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        abort(404)
    questions = connection.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY position", (quiz_id,)
    ).fetchall()
    if not questions:
        flash("Add at least one question before publishing.", "error")
    elif any(not question["prompt"].strip() or not question["answer_key"].strip() for question in questions):
        flash("Every question needs a prompt and expected answer before publishing.", "error")
    elif any(
        question["kind"] == "mcq"
        and (
            len(json.loads(question["options"])) < 2
            or question["answer_key"] not in json.loads(question["options"])
        )
        for question in questions
    ):
        flash("Each MCQ needs at least two options, and its correct answer must match one option.", "error")
    else:
        connection.execute(
            "UPDATE quizzes SET status = 'published' WHERE id = ?", (quiz_id,)
        )
        connection.commit()
        flash("Quiz published for students.", "success")
    return redirect(url_for("edit_quiz", quiz_id=quiz_id))


with app.app_context():
    initialize_database()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
