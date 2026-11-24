"""Locate a headword's permitted surface form without changing source values.

Single-word regular plural/third-person -s and a standalone source '~' object
slot are supported. Template words remain unchanged; generated contexts use
their contiguous lexical form with a real complement. Semantic validation
still checks that the selected form and part of speech fit the sentence.
"""
import re


def target_forms(word):
    # A dictionary object slot is not a literal character in an English
    # sentence. Only this explicit notation is normalized, never an arbitrary
    # wildcard: 'open ~ up' permits 'open up', not 'open ... unrelated ... up'.
    if isinstance(word, str) and word.split().count('~') == 1:
        tokens = word.split()
        if tokens[0] != '~' and all(token == '~' or re.fullmatch(r"[A-Za-z][A-Za-z'’-]*", token) for token in tokens):
            return [' '.join(token for token in tokens if token != '~')]
    forms = [word]
    if re.fullmatch(r"[A-Za-z]+", word or ""):
        if re.search(r"[^aeiou]y$", word, re.I):
            plural = word[:-1] + 'ies'
        elif re.search(r"(?:s|x|z|ch|sh)$", word, re.I):
            plural = word + 'es'
        else:
            plural = word + 's'
        forms.append(plural)
    return forms


def target_matches(text, word, *, inflected=True):
    forms = target_forms(word) if inflected else [word]
    pattern = r"(?<![A-Za-z0-9_'’-])(?:" + '|'.join(
        re.escape(value) for value in sorted(forms, key=len, reverse=True)) + r")(?![A-Za-z0-9_'’-])"
    return list(re.finditer(pattern, text, re.I)) if word else []


def target_surface(text, word, *, unique=True):
    matches = target_matches(text, word)
    return matches[0].group() if matches and (not unique or len(matches) == 1) else None


def blank_target(text, word, *, unique=True):
    matches = target_matches(text, word)
    if not matches or unique and len(matches) != 1:
        raise ValueError('문장에서 대상 단어 하나를 확인해야 합니다.')
    for match in reversed(matches):
        text = text[:match.start()] + '______' + text[match.end():]
    return text
