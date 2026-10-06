import re


QUESTION_START = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
SECTION_HEADING = re.compile(r"^\s*\d+\s*:\s+.+$")
DIVIDER = re.compile(r"^\s*-{5,}\s*$")


def parse_pasted_questions(text: str) -> list[str]:
    """Parse numbered question text, including lines wrapped across paragraphs."""
    questions: list[str] = []
    current: list[str] = []

    def save_current() -> None:
        if current:
            question = " ".join(part.strip() for part in current if part.strip())
            if question:
                questions.append(question)
            current.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or DIVIDER.match(line) or SECTION_HEADING.match(line):
            continue

        match = QUESTION_START.match(line)
        if match:
            save_current()
            current.append(match.group(2))
        elif current:
            current.append(line)

    save_current()
    return questions