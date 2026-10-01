"""HTML text extraction."""

from chatforge.tools import htmltext

PAGE = """<!doctype html>
<html><head><title>  My   Page </title>
<style>body { color: red }</style>
<script>var secret = 1;</script></head>
<body>
<nav><a href="/">Home</a> <a href="/x">Menu</a></nav>
<h1>Heading</h1>
<p>First    paragraph
with   wrapped lines &amp; an entity.</p>
<noscript>Enable JS</noscript>
<svg><text>chart label</text></svg>
<ul><li>one</li><li>two</li></ul>
<script>document.write("nope")</script>
<footer>Copyright junk</footer>
</body></html>"""


def test_skips_boilerplate_and_extracts_title():
    title, text = htmltext.extract(PAGE)
    assert title == "My Page"
    assert "Heading" in text
    assert "First paragraph with wrapped lines & an entity." in text
    for junk in (
        "secret",
        "color: red",
        "Home",
        "Menu",
        "Enable JS",
        "chart label",
        "nope",
        "Copyright",
    ):
        assert junk not in text
    assert "My Page" not in text  # the title is not repeated in the body text


def test_whitespace_collapsed():
    text = htmltext.html_to_text("<div>a \n\t  b</div>\n\n\n<div>   c   </div><p></p><p>d</p>")
    assert text == "a b\nc\nd"
    assert "  " not in text
    assert "\n\n" not in text


def test_nested_skip_and_unclosed():
    text = htmltext.html_to_text("<nav><nav>x</nav>still nav</nav>visible<script>never closed")
    assert text == "visible"


def test_self_closing_and_br():
    assert htmltext.html_to_text("one<br/>two<br>three<svg/>four") == "one\ntwo\nthreefour"


def test_plain_fragment_and_empty():
    assert htmltext.html_to_text("just text") == "just text"
    assert htmltext.extract("") == ("", "")


def test_collapse_text():
    assert htmltext.collapse_text("a   b\n\n\n  c  \t\n") == "a b\nc"
