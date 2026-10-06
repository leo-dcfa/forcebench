"""Shell lexing shared by the ``sf_cli`` and ``ci_workflow`` graders.

posix ``shlex`` differs from bash in ways that change what a command means, so a line is
pre-scanned (``prescan``) before shlex splits it (``split_words``):

- ``#`` starts a comment only at the start of a word outside quotes (``--path /x#frag`` and
  ``${VAR#v}`` keep theirs), and the comment runs to the end of its line;
- a ``$`` the shell passes literally, in single quotes or escaped (``'$KEY'``, ``\\$KEY``), is
  replaced by ``LITERAL_DOLLAR``, so it is never taken for the variable;
- the file descriptor written directly before a redirect (``2>&1``, ``1> out.txt``) is prefixed
  with ``FD_MARK``; in ``--wait 2 > out.txt`` the ``2`` stays a value.

Double quotes nest inside ``$(...)`` as in bash (``"$(cmd "a # b")"``).
"""

import re
import shlex


# Private-use markers set by prescan.
LITERAL_DOLLAR = ""
FD_MARK = ""

OPERATORS = frozenset({"&&", "||", ";;", "|&", ";", "|", "&"})
REDIRECTS = frozenset({"&>>", "<<<", "&>", ">>", "<<", ">&", "<&", ">|", "<>", "<", ">"})
# Longest first, so that `&>` is one redirect, not `&` then `>`.
_PUNCTUATION = sorted({*OPERATORS, *REDIRECTS, "(", ")"}, key=len, reverse=True)
_PUNCT_TOKEN_RE = re.compile(r"^[();<>|&]+$")
_WORD_BREAKS = frozenset(";&|()<>")


def prescan(line: str, mark: bool = True) -> str:
    """Cut comments and set the markers described in the module docstring. With
    ``mark=False`` only comments are cut; the rest of the text is unchanged.
    """
    out: list[str] = []
    stack: list[str] = []  # open double quotes ('"'), $(...) ("$(") and groups ("(")
    word_start = True
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        quoted = bool(stack) and stack[-1] == '"'
        if ch == "\\" and i + 1 < n:
            nxt = line[i + 1]
            out += [ch, LITERAL_DOLLAR if mark and nxt == "$" else nxt]
            i, word_start = i + 2, False
            continue
        if ch == "'" and not quoted:
            end = line.find("'", i + 1)
            end = n if end == -1 else end + 1  # unbalanced: left for shlex to report
            text = line[i:end]
            out.append(text.replace("$", LITERAL_DOLLAR) if mark else text)
            i, word_start = end, False
            continue
        if ch == '"':
            if quoted:
                stack.pop()
            else:
                stack.append('"')
            out.append(ch)
            i, word_start = i + 1, False
            continue
        if line.startswith("$(", i):
            stack.append("$(")
            out.append("$(")
            i, word_start = i + 2, True
            continue
        if quoted:
            out.append(ch)
            i += 1
            continue
        if ch == "#" and word_start:
            end = line.find("\n", i)
            if end == -1:
                break
            i = end
            continue
        if ch.isdigit() and word_start:
            j = i
            while j < n and line[j].isdigit():
                j += 1
            if j < n and line[j] in "<>":
                out.append((FD_MARK if mark else "") + line[i:j])
                i, word_start = j, False
                continue
        if ch == "(":
            stack.append("(")
        elif ch == ")" and stack and stack.pop() == "$(":
            out.append(ch)  # the end of a substitution does not end the word: `$(x)#y`
            i, word_start = i + 1, False
            continue
        word_start = ch.isspace() or ch in _WORD_BREAKS
        out.append(ch)
        i += 1
    return "".join(out)


def strip_comment(line: str) -> str:
    """The line without its shell (or YAML) comment."""
    return prescan(line, mark=False)


def unmark(text: str) -> str:
    """Pre-scanned text back as written (a literal ``$`` is a plain ``$`` again)."""
    return text.replace(LITERAL_DOLLAR, "$").replace(FD_MARK, "")


def _split_punct(tok: str) -> list[str]:
    """Split a run of shell punctuation (``)||``) into operators."""
    out, i = [], 0
    while i < len(tok):
        op = next((p for p in _PUNCTUATION if tok.startswith(p, i)), tok[i])
        out.append(op)
        i += len(op)
    return out


def split_words(text: str) -> list[str]:
    """shlex-split pre-scanned text: quotes removed, operators and redirections as tokens of
    their own. Raises ValueError on unbalanced quotes.
    """
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # prescan cut the comments
    out: list[str] = []
    for tok in lex:
        out.extend(_split_punct(tok) if _PUNCT_TOKEN_RE.match(tok) else [tok])
    return out


def tokenize(line: str) -> list[str]:
    """The words of a shell line, lexed like bash (see the module docstring)."""
    return split_words(prescan(line))
