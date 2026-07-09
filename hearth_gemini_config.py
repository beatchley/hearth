"""
Shared Gemini configuration — the model name lived independently in three
files until a July 2026 deprecation (gemini-2.5-flash) required three
separate edits and let one of them (hearth_comment_classifier.py) go
unnoticed. Every Gemini call site should import GEMINI_MODEL_NAME from here
instead of hardcoding the string, so the next model migration is a one-line
change in one place.
"""

GEMINI_MODEL_NAME = "gemini-3.5-flash"
