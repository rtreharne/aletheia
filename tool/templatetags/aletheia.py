"""Safe rendering of AI-written text that may contain superscripts and subscripts."""

import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

SUPERSCRIPTS = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", "0123456789+−=()ni"))
SUBSCRIPTS = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ", "0123456789+−=()aeoxhklmnpst"))

_TAG = re.compile(r"&lt;(/?)(sup|sub)&gt;", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _unicode_runs(text, table, tag):
    pattern = "[" + re.escape("".join(table)) + "]+"
    return re.sub(pattern, lambda m: f"<{tag}>" + "".join(table[c] for c in m.group()) + f"</{tag}>", text)


@register.filter
def scripts(value):
    """Escape everything, then allow only balanced <sup> and <sub> tags, and `inline code`.

    Unicode super/subscript characters are converted to tags too, so they render
    in the page font rather than a fallback. Text inside backticks is shown as
    code, exactly as written.
    """
    text = escape(str(value or ""))
    parts = _INLINE_CODE.split(text)  # even indexes: prose; odd: code
    return mark_safe(
        "".join(f"<code>{part}</code>" if i % 2 else _prose(part) for i, part in enumerate(parts))
    )


def _prose(text):
    out, stack, pos = [], [], 0
    for match in _TAG.finditer(text):
        out.append(text[pos : match.start()])
        closing, name = match.group(1) == "/", match.group(2).lower()
        if not closing:
            stack.append(name)
            out.append(f"<{name}>")
        elif name in stack:
            while stack:  # close anything left open inside it
                top = stack.pop()
                out.append(f"</{top}>")
                if top == name:
                    break
        # A stray closing tag is dropped.
        pos = match.end()
    out.append(text[pos:])
    out.extend(f"</{name}>" for name in reversed(stack))
    html = "".join(out)
    html = _unicode_runs(html, SUPERSCRIPTS, "sup")
    html = _unicode_runs(html, SUBSCRIPTS, "sub")
    # Keep "2.1 × 10<sup>−4</sup>" together on one line.
    html = re.sub(r"(\d) × (\d)", "\\1\u00a0×\u00a0\\2", html)
    return html


@register.filter
def surname_first(value):
    """'Ada Lovelace' → 'Lovelace, Ada' (names already 'Surname, Given' are kept)."""
    from tool.lti import surname_first as order

    return order(str(value or ""))


@register.filter
def or_placeholder(value, label):
    """The value, or a clearly marked [label] where a legal detail hasn't been filled in yet."""
    if value:
        return value
    return mark_safe(f'<span class="placeholder">[{escape(label)}]</span>')
