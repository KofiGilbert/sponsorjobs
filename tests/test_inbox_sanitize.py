"""The Inbox reader's HTML sanitiser (inbox/sanitize.py): an allowlist, so mail can never run
script, load a remote image, post a form or carry a script link into the app."""
from __future__ import annotations

import pytest

from inbox.sanitize import sanitize_html


def test_plain_formatting_survives():
    out = sanitize_html('<div><p style="color: #333; text-align: center">Hi <b>Zoe</b>,<br>welcome</p>'
                        '<ul><li>one</li></ul><table width="100%"><tr><td colspan="2">x</td></tr></table></div>')
    assert "<b>Zoe</b>" in out and "<br>" in out and "<li>one</li>" in out
    assert 'style="color: #333; text-align: center"' in out
    assert '<td colspan="2">x</td>' in out and '<table width="100%">' in out


@pytest.mark.parametrize("bad", [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=https://evil.example/x.js></SCRIPT>",
    "<style>body{background:url(https://evil.example/t)}</style>",
    "<iframe src='https://evil.example'></iframe>",
    "<form action='https://evil.example'><input name='pw'><button>Go</button></form>",
    "<object data='x'></object><embed src='x'>",
    "<svg onload='alert(1)'><script>alert(1)</script></svg>",
    "<link rel='stylesheet' href='https://evil.example/x.css'>",
    "<meta http-equiv='refresh' content='0;url=https://evil.example'>",
    "<base href='https://evil.example/'>",
])
def test_dangerous_elements_go_with_their_content(bad):
    out = sanitize_html(f"<p>before</p>{bad}<p>after</p>")
    assert out == "<p>before</p><p>after</p>"


def test_event_handlers_and_unknown_attributes_are_stripped():
    out = sanitize_html('<p onclick="evil()" ONMOUSEOVER=evil() id="x" class="y" data-track="1">hello</p>')
    assert out == "<p>hello</p>"


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", " JaVaScRiPt:alert(1)", "java\tscript:alert(1)", "data:text/html,<script>x</script>",
    "vbscript:x", "//evil.example/x", "file:///etc/passwd", "sponsorjobs://anything",
])
def test_only_http_and_mailto_links_keep_their_href(href):
    out = sanitize_html(f'<a href="{href}">click</a>')
    assert "href" not in out and "click" in out


def test_safe_links_open_outside_with_no_referrer():
    out = sanitize_html('<a href="https://acme.example/jobs?a=1&amp;b=2" onclick="x()">Open</a>'
                        '<a href="mailto:hr@acme.example">Mail</a>')
    assert ('<a href="https://acme.example/jobs?a=1&amp;b=2" rel="noopener noreferrer nofollow" '
            'target="_blank">Open</a>') in out
    assert 'href="mailto:hr@acme.example"' in out and "onclick" not in out


def test_images_are_removed_so_no_tracking_pixel_loads():
    out = sanitize_html('<p>Hi<img src="https://track.example/open.gif?id=9" width="1" height="1">'
                        '<img src="cid:logo"><img src="data:image/png;base64,AAAA"></p>'
                        '<table background="https://track.example/bg.png"><tr><td>x</td></tr></table>')
    assert "img" not in out and "track.example" not in out and "cid:" not in out and "data:" not in out


@pytest.mark.parametrize("style", [
    "background: url(https://track.example/p.gif)", "background-image:url('x')", "width: expression(alert(1))",
    "behavior: url(x.htc)", "-moz-binding: url(x)", "color: red; background:url(x)", "position: fixed; top: 0",
    "color: r\\65 d", "font-family: x; @import 'y'",
])
def test_style_keeps_only_harmless_declarations(style):
    out = sanitize_html(f'<p style="{style}">t</p>')
    for bad in ("url", "expression", "behavior", "binding", "position", "import", "\\"):
        assert bad not in out
    assert ">t</p>" in out


def test_nested_and_broken_markup_cannot_smuggle_a_tag():
    out = sanitize_html("<scr<script>ipt>alert(1)</scr</script>ipt><p>ok</p>")
    assert "<script" not in out.lower() and "<p>ok</p>" in out
    out = sanitize_html("<p>one<div><span>two")
    assert out == "<p>one<div><span>two</span></div></p>"          # always closed
    out = sanitize_html("<!--[if mso]><script>x</script><![endif]--><p>x &lt;script&gt; y</p>")
    assert out == "<p>x &lt;script&gt; y</p>"                      # text stays text
    assert "<script" not in sanitize_html("<p>a</p><script>never closed <p>b</p>")


def test_nothing_to_show_is_none():
    assert sanitize_html(None) is None
    assert sanitize_html("   ") is None
    assert sanitize_html("<script>only()</script>") is None
