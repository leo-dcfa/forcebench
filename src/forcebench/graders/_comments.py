r"""Comment stripping for regex checks on source files (``static_code`` checks).

``strip_comments(text, path)`` removes the comments of the file's language, chosen by its
extension, and leaves string literals alone, so a pattern never matches commented-out code and
a ``//`` inside a string (a URL) is not taken for a comment:

- Apex, Java: ``//`` and ``/* */``; strings in ``'...'`` (and ``"..."``).
- JavaScript/TypeScript: the same, plus template literals; a backslash outside a string escapes
  the next character, so the escaped slashes of a regex literal (``/https?:\/\//``) never
  start a comment.
- CSS: ``/* */`` only.
- HTML, XML (``-meta.xml`` included), Visualforce and Aura markup: ``<!-- -->``.
- YAML, shell, properties, ignore files: ``#`` at the start of a word outside quotes (see
  ``_shell.strip_comment``), line by line.
- Anything else (JSON, SOQL, unknown extensions): unchanged.

A comment is replaced by the line breaks it spans, or by one space, so line-anchored patterns
see the same lines as before and ``a/**/b`` stays two tokens.
"""

import re
from pathlib import PurePosixPath

from forcebench.graders._shell import strip_comment


# A string runs to its closing quote or, unterminated, to the end of its line (so the rest of
# that line is never read as a comment, and scanning stays linear).
_STRING = r"'(?:\\.|[^'\\\n])*(?:'|(?=\n)|\Z)|\"(?:\\.|[^\"\\\n])*(?:\"|(?=\n)|\Z)"
_BLOCK = r"/\*.*?(?:\*/|\Z)"
_LINE = r"//[^\n]*"

_C_LIKE_RE = re.compile(rf"(?P<keep>{_STRING})|(?P<comment>{_BLOCK}|{_LINE})", re.S)
_JS_RE = re.compile(
    rf"(?P<keep>{_STRING}|`(?:\\.|[^`\\])*`|\\.)|(?P<comment>{_BLOCK}|{_LINE})", re.S
)
_CSS_RE = re.compile(rf"(?P<keep>{_STRING})|(?P<comment>{_BLOCK})", re.S)
_MARKUP_RE = re.compile(r"(?P<keep>(?!))|(?P<comment><!--.*?(?:-->|\Z))", re.S)

_BY_SUFFIX: dict[str, re.Pattern[str]] = {
    **dict.fromkeys([".cls", ".trigger", ".apex", ".java"], _C_LIKE_RE),
    **dict.fromkeys([".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"], _JS_RE),
    ".css": _CSS_RE,
    **dict.fromkeys(
        [".html", ".htm", ".xml", ".page", ".component", ".cmp", ".app", ".evt", ".design"],
        _MARKUP_RE,
    ),
}
_HASH_SUFFIXES = {".yaml", ".yml", ".sh", ".bash", ".zsh", ".properties"}
_HASH_NAMES = {".forceignore", ".gitignore", ".env"}


def _blank(m: re.Match[str]) -> str:
    if m.group("comment") is None:
        return m.group(0)
    breaks = m.group(0).count("\n")
    return "\n" * breaks if breaks else " "


def strip_comments(text: str, path: str) -> str:
    """``text`` without the comments of the language ``path``'s extension names."""
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    if suffix in _HASH_SUFFIXES or p.name.lower() in _HASH_NAMES:
        return "\n".join(strip_comment(line) for line in text.split("\n"))
    pattern = _BY_SUFFIX.get(suffix)
    return pattern.sub(_blank, text) if pattern else text
