"""Make an e-mail's HTML safe to show inside the app (the Inbox reader).

Mail is untrusted. This is an ALLOWLIST built on the standard library's HTML parser, so nothing
is passed through that is not named here:

  * only plain formatting and layout tags survive (p, div, table, a, b, lists, headings...);
  * script, style, iframe, object, embed, form and their kin are dropped WITH their content;
    every other unknown tag is dropped and its text kept;
  * only a short list of attributes survives; every ``on*`` handler and every attribute not on
    the list goes;
  * a link keeps its href only when it is http, https or mailto, and always gets
    ``rel="noopener noreferrer nofollow" target="_blank"``;
  * images are removed altogether: a remote image is how a sender learns the mail was opened
    (tracking pixels), and cid:/data: images have nothing to load from here;
  * a ``style`` attribute keeps only a few harmless declarations (colour, weight, alignment...),
    and never one that contains url(, expression(, @import or a script-like value.

The result is a fragment (no html/head/body). No third-party dependency.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

MAX_INPUT = 1 << 20

_ALLOWED = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup", "dd", "div",
    "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre",
    "s", "small", "span", "strike", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "u", "ul",
}
_VOID = {"br", "hr", "col"}
# Dropped together with everything inside them.
_DROP_WITH_CONTENT = {
    "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "form", "noscript",
    "template", "svg", "math", "head", "title", "select", "textarea", "button", "audio", "video",
    "canvas", "map", "link", "meta", "base",
}
_DROP_VOID = {"link", "meta", "base", "embed", "frame"}      # dropped tags that have no end tag
_GLOBAL_ATTRS = {"align", "valign", "dir", "lang", "title", "style"}
_TAG_ATTRS = {
    "a": {"href"},
    "td": {"colspan", "rowspan", "width", "height", "bgcolor"},
    "th": {"colspan", "rowspan", "width", "height", "bgcolor"},
    "table": {"width", "cellpadding", "cellspacing", "border", "bgcolor"},
    "tr": {"bgcolor"},
    "col": {"span", "width"},
    "colgroup": {"span"},
    "font": {"color", "size"},
    "ol": {"start", "type"},
}
_SAFE_HREF_RE = re.compile(r"^(?:https?://|mailto:)", re.I)
_ATTR_VALUE_RE = re.compile(r"^[\w #%.,:;()/+-]{0,200}$")

_STYLE_PROPS = {
    "color", "background-color", "font-weight", "font-style", "font-size", "font-family",
    "text-align", "text-decoration", "line-height", "vertical-align", "white-space",
    "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right",
    "border", "border-top", "border-bottom", "border-left", "border-right", "border-collapse",
    "border-radius", "width", "max-width", "height",
}
_STYLE_VALUE_RE = re.compile(r"^[\w #%.,'\"()+-]{1,120}$")
_STYLE_BAD_RE = re.compile(r"url|expression|import|javascript|behaviou?r|binding|\\|/\*|<|>|@", re.I)


def _safe_style(value: str) -> str:
    out = []
    for decl in str(value or "").split(";"):
        prop, sep, val = decl.partition(":")
        prop, val = prop.strip().lower(), val.strip()
        if not sep or prop not in _STYLE_PROPS:
            continue
        if _STYLE_BAD_RE.search(val) or not _STYLE_VALUE_RE.match(val):
            continue
        out.append(f"{prop}: {val}")
    return "; ".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip: list[str] = []          # open tags whose content is being dropped
        self._open: list[str] = []          # allowed tags currently open

    # -- tags ---------------------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip:
            if tag in _DROP_WITH_CONTENT and tag not in _DROP_VOID:
                self._skip.append(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            if tag not in _DROP_VOID:
                self._skip.append(tag)
            return
        if tag not in _ALLOWED:
            return                                        # img, body, html, unknown: tag dropped
        kept = []
        allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag, set())
        for name, value in attrs:
            name = (name or "").lower()
            value = "" if value is None else str(value)
            if name not in allowed or name.startswith("on"):
                continue
            if name == "href":
                value = value.strip()
                if not _SAFE_HREF_RE.match(value) or any(c in value for c in "\r\n\t"):
                    continue
            elif name == "style":
                value = _safe_style(value)
                if not value:
                    continue
            elif not _ATTR_VALUE_RE.match(value):
                continue
            kept.append(f' {name}="{escape(value, quote=True)}"')
        if tag == "a":
            kept.append(' rel="noopener noreferrer nofollow" target="_blank"')
        self.out.append(f"<{tag}{''.join(kept)}>")
        if tag not in _VOID:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._skip or tag in _DROP_WITH_CONTENT:
            return
        self.handle_starttag(tag, attrs)
        if tag in _ALLOWED and tag not in _VOID and self._open and self._open[-1] == tag:
            self._open.pop()
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag not in _ALLOWED or tag in _VOID or tag not in self._open:
            return
        # Close everything opened inside it, so the output is always well nested.
        while self._open:
            top = self._open.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    # -- text ---------------------------------------------------------------------------------
    def handle_data(self, data):
        if not self._skip:
            self.out.append(escape(data, quote=False))

    def handle_comment(self, data):          # comments (and conditional comments) are dropped
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    def result(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out).strip()


def sanitize_html(html: str | None) -> str | None:
    """The safe fragment for ``html``, or None when there is nothing to show (no HTML, or the
    parser could not read it: the caller then shows the text body)."""
    if not html or not str(html).strip():
        return None
    p = _Sanitizer()
    try:
        p.feed(str(html)[:MAX_INPUT])
        p.close()
    except Exception:       # noqa: BLE001 - never let a malformed mail break the reader
        return None
    out = p.result()
    return out or None
