import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as quiz_app


class QuizWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database = quiz_app.DATABASE
        self.old_upload_dir = quiz_app.UPLOAD_DIR
        root = Path(self.temp_dir.name)
        quiz_app.DATABASE = root / "quiz.sqlite3"
        quiz_app.UPLOAD_DIR = root / "uploads"
        quiz_app.app.config.update(TESTING=True, SECRET_KEY="test-session-secret")
        with quiz_app.app.app_context():
            quiz_app.initialize_database()
            connection = quiz_app.db_connection()
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE username = 'teach'",
                (generate_password_hash("teacher-password"),),
            )
            connection.execute(
                """INSERT INTO users
                   (username, password_hash, role, name, enrollment, batch, photo, created_at)
                   VALUES (?, ?, 'student', ?, ?, ?, ?, ?)""",
                (
                    "ENR001",
                    generate_password_hash("student-password"),
                    "Sam Student",
                    "ENR001",
                    "2024",
                    "sam.webp",
                    quiz_app.utc_now(),
                ),
            )
            connection.commit()
        self.client = quiz_app.app.test_client()

    def tearDown(self):
        quiz_app.DATABASE = self.old_database
        quiz_app.UPLOAD_DIR = self.old_upload_dir
        self.temp_dir.cleanup()

    def test_student_can_register_without_a_profile_photo(self):
        response = self.client.post(
            "/register",
            data={
                "name": "Photo Free",
                "enrollment": "ENR002",
                "batch": "2024",
                "password": "student-password",
            },
        )

        self.assertEqual(response.status_code, 302)
        with quiz_app.app.app_context():
            student = quiz_app.db_connection().execute(
                "SELECT photo FROM users WHERE enrollment = ?", ("ENR002",)
            ).fetchone()
        self.assertIsNone(student["photo"])

    def test_teacher_publishes_mixed_quiz_and_student_receives_score(self):
        response = self.client.post(
            "/login",
            data={"username": "teach", "password": "teacher-password"},
        )
        self.assertEqual(response.status_code, 302)
        with patch.object(quiz_app, "generate_answer_suggestions", return_value=({0: "Pillow"}, None)):
            response = self.client.post(
                "/admin/quizzes/new",
                data={
                    "title": "Image basics",
                    "subject": "CSET344",
                    "teacher_name": "Teacher",
                    "pasted_questions": "1. The image library is _________.",
                    "mcq_prompt[]": ["Choose a logical operator."],
                    "mcq_options[]": ["AND | OR"],
                    "mcq_answer[]": ["AND"],
                    "mcq_marks[]": ["0.5"],
                },
            )
        quiz_id = int(response.location.split("/")[3])

        with quiz_app.app.app_context():
            connection = quiz_app.db_connection()
            quiz = connection.execute(
                "SELECT status FROM quizzes WHERE id = ?", (quiz_id,)
            ).fetchone()
            questions = connection.execute(
                "SELECT * FROM questions WHERE quiz_id = ? ORDER BY position",
                (quiz_id,),
            ).fetchall()
        self.assertEqual(quiz["status"], "draft")
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0]["answer_key"], "Pillow")
        self.assertEqual(questions[0]["marks"], 1.0)
        self.assertIn("from_json", quiz_app.app.jinja_env.filters)

        response = self.client.post(
            f"/admin/quizzes/{quiz_id}/edit",
            data={
                "title": "Image basics",
                "subject": "CSET344",
                "teacher_name": "Teacher",
                "question_id[]": [str(q["id"]) for q in questions],
                "prompt[]": [q["prompt"] for q in questions],
                "kind[]": [q["kind"] for q in questions],
                "answer_key[]": ["Pillow", "AND"],
                "options[]": ["", "AND | OR"],
                "marks[]": ["2", "0.5"],
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f"/admin/quizzes/{quiz_id}/publish")
        self.assertEqual(response.status_code, 302)

        self.client.post(
            "/login",
            data={"username": "ENR001", "password": "student-password"},
        )
        response = self.client.post(f"/quiz/{quiz_id}/start")
        attempt_id = int(response.location.rsplit("/", 1)[1])
        response = self.client.get(response.location)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a logical operator.", response.data)
        response = self.client.post(
            f"/attempt/{attempt_id}",
            data={
                "answer_1": "Pillow",
                "answer_2": "AND",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.location)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2.5", response.data)
        self.assertIn(b"Only your score is shown", response.data)
        self.assertNotIn(b"RESPONSE REVIEW", response.data)
        self.assertNotIn(b"Pillow", response.data)

        with quiz_app.app.app_context():
            attempt = quiz_app.db_connection().execute(
                "SELECT score, total_marks, submitted_at, focus_violation FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt["score"], 2.5)
        self.assertEqual(attempt["total_marks"], 2.5)
        self.assertIsNotNone(attempt["submitted_at"])
        self.assertEqual(attempt["focus_violation"], 0)

        self.client.post(
            "/login",
            data={"username": "teach", "password": "teacher-password"},
        )
        response = self.client.get("/admin")
        self.assertIn(b"Clear", response.data)
        response = self.client.post(
            f"/admin/quizzes/{quiz_id}/answer-visibility",
            data={"show_answers": ["0", "1"]},
        )
        self.assertEqual(response.status_code, 302)

        self.client.post(
            "/login",
            data={"username": "ENR001", "password": "student-password"},
        )
        response = self.client.get(f"/result/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"RESPONSE REVIEW", response.data)
        self.assertIn(b"Expected", response.data)
        self.assertIn(b"Pillow", response.data)

    def test_teacher_can_fill_missing_answers_for_existing_draft(self):
        self.client.post(
            "/login",
            data={"username": "teach", "password": "teacher-password"},
        )
        with patch.object(quiz_app, "generate_answer_suggestions", return_value=({}, None)):
            response = self.client.post(
                "/admin/quizzes/new",
                data={
                    "title": "Image basics",
                    "subject": "CSET344",
                    "teacher_name": "Teacher",
                    "pasted_questions": "1. The image library is _________.",
                    "default_marks": "2.5",
                },
            )
        quiz_id = int(response.location.split("/")[3])

        with patch.object(quiz_app, "generate_answer_suggestions", return_value=({0: "Pillow"}, None)) as generate:
            response = self.client.post(f"/admin/quizzes/{quiz_id}/generate-answers")

        self.assertEqual(response.status_code, 302)
        generate.assert_called_once_with(
            ["The image library is _________."], "CSET344"
        )
        with quiz_app.app.app_context():
            question = quiz_app.db_connection().execute(
                "SELECT answer_key, marks FROM questions WHERE quiz_id = ?",
                (quiz_id,),
            ).fetchone()
        self.assertEqual(question["answer_key"], "Pillow")
        self.assertEqual(question["marks"], 2.5)

    def test_teacher_sees_groq_provider_error(self):
        self.client.post(
            "/login",
            data={"username": "teach", "password": "teacher-password"},
        )
        with patch.object(quiz_app, "generate_answer_suggestions", return_value=({}, None)):
            response = self.client.post(
                "/admin/quizzes/new",
                data={
                    "title": "Image basics",
                    "subject": "CSET344",
                    "teacher_name": "Teacher",
                    "pasted_questions": "1. The image library is _________.",
                },
            )
        quiz_id = int(response.location.split("/")[3])

        with patch.object(
            quiz_app,
            "generate_answer_suggestions",
            return_value=({}, "Groq rejected the request (HTTP 403)."),
        ):
            response = self.client.post(
                f"/admin/quizzes/{quiz_id}/generate-answers",
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"HTTP 403", response.data)
        self.assertIn(b"enter each answer and mark manually", response.data)

    def test_three_tab_switches_flag_incomplete_and_teacher_can_resume(self):
        self.client.post(
            "/login",
            data={"username": "teach", "password": "teacher-password"},
        )
        with patch.object(quiz_app, "generate_answer_suggestions", return_value=({}, None)):
            response = self.client.post(
                "/admin/quizzes/new",
                data={
                    "title": "Resume test",
                    "subject": "CSET344",
                    "teacher_name": "Teacher",
                    "pasted_questions": "1. The image library is _________.",
                },
            )
        quiz_id = int(response.location.split("/")[3])
        with quiz_app.app.app_context():
            question_id = quiz_app.db_connection().execute(
                "SELECT id FROM questions WHERE quiz_id = ?", (quiz_id,)
            ).fetchone()["id"]
        self.client.post(
            f"/admin/quizzes/{quiz_id}/edit",
            data={
                "title": "Resume test",
                "subject": "CSET344",
                "teacher_name": "Teacher",
                "question_id[]": [str(question_id)],
                "prompt[]": ["The image library is _________."],
                "kind[]": ["blank"],
                "answer_key[]": ["Pillow"],
                "options[]": [""],
                "marks[]": ["1"],
            },
        )
        self.client.post(f"/admin/quizzes/{quiz_id}/publish")

        self.client.post(
            "/login",
            data={"username": "ENR001", "password": "student-password"},
        )
        response = self.client.post(f"/quiz/{quiz_id}/start")
        attempt_id = int(response.location.rsplit("/", 1)[1])
        response = self.client.post(
            f"/attempt/{attempt_id}/save",
            json={"answers": {str(question_id): "PIL"}, "position": 0},
        )
        self.assertEqual(response.status_code, 200)

        for strike in (1, 2):
            response = self.client.post(
                f"/attempt/{attempt_id}/tab-switch",
                json={"answers": {str(question_id): "PIL"}, "position": 0},
            )
            self.assertEqual(response.json["status"], "in_progress")
            self.assertEqual(response.json["tab_switches"], strike)

        response = self.client.post(
            f"/attempt/{attempt_id}/tab-switch",
            json={"answers": {str(question_id): "PIL"}, "position": 0},
        )
        self.assertEqual(response.json["status"], "incomplete")
        self.assertEqual(response.json["tab_switches"], 3)
        with quiz_app.app.app_context():
            attempt = quiz_app.db_connection().execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            saved = quiz_app.db_connection().execute(
                "SELECT response FROM answers WHERE attempt_id = ? AND question_id = ?",
                (attempt_id, question_id),
            ).fetchone()
        self.assertEqual(attempt["status"], "incomplete")
        self.assertIsNone(attempt["submitted_at"])
        self.assertEqual(attempt["focus_violation"], 1)
        self.assertEqual(saved["response"], "PIL")

        response = self.client.get(f"/result/{attempt_id}")
        self.assertIn(b"INCOMPLETE", response.data)
        self.assertNotIn(b"PIL", response.data)
        self.client.post(f"/attempt/{attempt_id}/request-resume")

        self.client.post(
            "/login",
            data={"username": "teach", "password": "teacher-password"},
        )
        response = self.client.get("/admin", follow_redirects=True)
        self.assertIn(b"CHEAT FOUND", response.data)
        self.assertIn(b"Student requested reopen", response.data)
        response = self.client.post(f"/admin/attempt/{attempt_id}/resume")
        self.assertEqual(response.status_code, 302)
        with quiz_app.app.app_context():
            attempt = quiz_app.db_connection().execute(
                "SELECT status, tab_switches, focus_violation FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt["status"], "in_progress")
        self.assertEqual(attempt["tab_switches"], 0)
        self.assertEqual(attempt["focus_violation"], 1)

        self.client.post(
            "/login",
            data={"username": "ENR001", "password": "student-password"},
        )
        response = self.client.get(f"/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="PIL"', response.data)


if __name__ == "__main__":
    unittest.main()
