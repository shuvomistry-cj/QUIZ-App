import unittest

from quiz_parser import parse_pasted_questions


SAMPLE = """CSET344: Image and Video Processing
Name Enroll.
Note: Each question carry 0.5 Marks.
--------------------------------------------------------------------------------------------------------------
1: Basic Image Operations
1. The standard Python library used to handle image loading, manipulation, and display
is _________
2. Performing logical operations such as AND, OR, and XOR on pixel values is referred
to as _________ operations.
2: Transformations & Histogram Equalization
5. Geometric modifications like rotation, scaling, and translation rely on _________ to
estimate unknown pixel values at new coordinates.
"""


class ParsePastedQuestionsTests(unittest.TestCase):
    def test_ignores_section_headings_and_joins_wrapped_lines(self):
        questions = parse_pasted_questions(SAMPLE)

        self.assertEqual(len(questions), 3)
        self.assertEqual(
            questions[0],
            "The standard Python library used to handle image loading, manipulation, and display is _________",
        )
        self.assertEqual(
            questions[2],
            "Geometric modifications like rotation, scaling, and translation rely on _________ to estimate unknown pixel values at new coordinates.",
        )


if __name__ == "__main__":
    unittest.main()