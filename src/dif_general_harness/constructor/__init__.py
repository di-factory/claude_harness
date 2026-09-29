"""The constructor (v1): match a pack, interview, build the instance spec, verify.

v1 is deterministic: the questions and the checks come from the packs. The approval gate
and deploy arrive in v2 (M2); the LLM-driven constructor agent pack and the OpenClaw skill
build on these same functions.
"""

from .build import BuildResult, build, pack_questions
from .catalog import PackMatch, match
from .evals import EvalReport, RecordingApprover, run_suites
from .interview import AnswerError, Question, interview, load_answers, parse, questions

__all__ = [
    "AnswerError",
    "BuildResult",
    "EvalReport",
    "PackMatch",
    "Question",
    "RecordingApprover",
    "build",
    "interview",
    "load_answers",
    "match",
    "pack_questions",
    "parse",
    "questions",
    "run_suites",
]
