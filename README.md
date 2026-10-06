# CSET344 Quiz Desk

A small Flask and SQLite quiz app for teacher-managed image and video processing assessments.

## Run locally

1. Create a virtual environment and install dependencies from `requirements.txt`.
2. Copy `.env.example` to `.env` and set a private `FLASK_SECRET_KEY`, `ADMIN_PASSWORD`, and `GROQ_API_KEY`. The app loads `.env` automatically on startup. Keep `.env` private; it is excluded from version control.
3. Start the app with `python app.py` from this folder, then open `http://127.0.0.1:5000`.

The first run creates `instance/quiz.sqlite3` and a teacher account with username `teach`. Set `ADMIN_PASSWORD` before first run; the development default is `teach123`. Student enrollment requires a name, unique enrollment ID, batch, and password; a profile photo is optional. Students sign in with the enrollment ID.

## Quiz workflow

- The teacher creates a quiz with a name, subject, and teacher name.
- Paste numbered questions such as `1. ...` into the fill-in-the-blank field. Wrapped lines are joined; section headings such as `1: Basic Image Operations` are skipped. Choose a default mark value for the imported questions (1 mark by default), then change any question independently in the draft editor. New MCQs also start at 1 mark. When `GROQ_API_KEY` is configured, answer suggestions are generated as the draft is imported; teachers can also fill missing answers later.
- Optional MCQs can be added with choices separated by `|`.
- Review the generated questions and enter an expected answer for every item. An MCQ's expected answer must exactly match one of its choices. Publishing is blocked until the answer key is complete.
- Students get one 25-minute attempt per quiz. The countdown submits the form at expiry; the server also enforces the deadline.
- Exam answers and the current question are autosaved. The first two exam-tab switches are recorded as warnings; a third marks the attempt `Incomplete · Cheat found` without grading it. The student can request a reopen; a teacher can reopen the same attempt, preserving answers and restarting its timer. A web app cannot prevent students from opening other tabs or using another device, and client-side tab detection is not tamper-proof.
- Exact normalized answers and MCQs are graded locally. When `GROQ_API_KEY` is set, non-exact fill-in answers are sent together in one Groq request for semantic scoring against the teacher's answer key. If the request fails, those responses receive zero rather than being left ungraded.
- The teacher dashboard lists quizzes and recent submitted marks with each student's name, enrollment, batch, and photo.

## Data and settings

SQLite data and uploaded student photos are stored under `instance/`. Keep that directory private and back it up. This is a classroom prototype; before public deployment, add CSRF protection, HTTPS, rate limiting, stronger upload validation, and a production WSGI server.

Environment variables:

- `FLASK_SECRET_KEY`: Flask session signing secret.
- `ADMIN_PASSWORD`: password assigned to seeded `teach` account on first database creation.
- `GROQ_API_KEY`: optional API key for semantic answer grading.
- `GROQ_MODEL`: optional Groq model name; default is `qwen/qwen3.8-27b`.
- `FLASK_DEBUG`: set to `1` only during local development.
