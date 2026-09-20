"""Lexical metrics and escaped token diffs; never render filing HTML."""

import html
import re
from difflib import SequenceMatcher

from rapidfuzz.fuzz import ratio, token_set_ratio


def lexical_similarity(old: str, new: str) -> float:
    return ratio(old, new) / 100.0


def candidate_similarity(old: str, new: str) -> float:
    return (ratio(old, new) + token_set_ratio(old, new)) / 200.0


def cosmetic_only(old: str, new: str) -> bool:
    # Do not suppress small edits to numbers, negation, obligations, or new sentences.
    # Ignore case and whitespace only: punctuation can change financial meaning too.
    return re.sub(r"\s+", "", old).casefold() == re.sub(r"\s+", "", new).casefold()


def word_diff(old: str, new: str) -> str:
    before, after = re.findall(r"\s+|\w+|[^\w\s]", old), re.findall(r"\s+|\w+|[^\w\s]", new)
    result = []
    for tag, i, j, k, m in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        left, right = html.escape("".join(before[i:j])), html.escape("".join(after[k:m]))
        if tag == "equal":
            result.append(left)
        else:
            if left:
                result.append(f"<del>{left}</del>")
            if right:
                result.append(f"<ins>{right}</ins>")
    return "".join(result)
