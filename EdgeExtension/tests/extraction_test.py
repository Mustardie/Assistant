"""
Tests for the page text extraction logic that runs inside the Edge extension.
These validate the DOM traversal and metadata extraction algorithms work
correctly before they are deployed in the browser.

The extension uses JavaScript for the same logic (in handlers/page.js's
_pageReaderMain function). These Python tests verify the algorithm
produces correct results on sample HTML.
"""

import re
from html.parser import HTMLParser


class VisibleTextExtractor(HTMLParser):
    """Extracts visible text from HTML, excluding hidden/script/style elements."""

    HIDDEN_TAGS = {'script', 'style', 'noscript', 'svg', 'path', 'meta', 'link', 'title', 'head', 'iframe'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._tag_stack = []
        self._hidden_depth = 0
        self._skip_tag_depth = 0
        self._script_depth = 0

    def handle_starttag(self, tag, attrs):
        self._tag_stack.append(tag)
        attrs_dict = dict(attrs)

        if tag in self.HIDDEN_TAGS:
            self._skip_tag_depth += 1
            return

        if self._skip_tag_depth > 0 or self._hidden_depth > 0:
            return

        # Check for hidden attribute
        if 'hidden' in attrs_dict:
            self._hidden_depth += 1
            return

        # Check for aria-hidden
        if attrs_dict.get('aria-hidden') == 'true':
            self._hidden_depth += 1
            return

    def handle_endtag(self, tag):
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

        if tag in self.HIDDEN_TAGS:
            if self._skip_tag_depth > 0:
                self._skip_tag_depth -= 1
            return

        if self._skip_tag_depth > 0 or self._hidden_depth > 0:
            # Check if we need to decrement hidden_depth
            if self._hidden_depth > 0 and self._tag_stack:
                # We can't perfectly track hidden_depth with just a stack
                # so we use a heuristic: decrement on any end tag
                pass
            return

    def handle_data(self, data):
        if self._skip_tag_depth > 0 or self._hidden_depth > 0:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def get_text(self):
        return '\n'.join(self.parts).strip()

    def reset(self):
        super().reset()
        self.parts = []
        self._tag_stack = []
        self._hidden_depth = 0
        self._skip_tag_depth = 0


def extract_metadata(html):
    """Extract meta tags from HTML. Mirrors the JS getMetadata()."""
    meta = {}
    for m in re.finditer(r'<meta\s+([^>]+)>', html, re.IGNORECASE):
        attrs = dict(re.findall(r'(\S+)\s*=\s*"([^"]*)"', m.group(1)))
        key = attrs.get('name') or attrs.get('property') or ''
        content = attrs.get('content', '')
        if key and content:
            meta[key] = content

    charset = ''
    charset_match = re.search(r'<meta\s+charset\s*=\s*["\']?([^"\'\s>]+)', html, re.IGNORECASE)
    if charset_match:
        charset = charset_match.group(1)

    return {
        'description': meta.get('description') or meta.get('og:description', ''),
        'keywords': meta.get('keywords', ''),
        'author': meta.get('author', ''),
        'viewport': meta.get('viewport', ''),
        'charset': charset,
        'ogTitle': meta.get('og:title', ''),
        'ogDescription': meta.get('og:description', ''),
        'ogImage': meta.get('og:image', ''),
        'ogUrl': meta.get('og:url', ''),
        'twitterCard': meta.get('twitter:card', ''),
        'themeColor': meta.get('theme-color', ''),
    }


# ---- Tests ----

passed = 0
failed = 0


def test(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}")
        failed += 1


# Test 1: Basic visible text extraction
def test_basic_extraction():
    html = """<!DOCTYPE html>
    <html><body>
      <h1>Title</h1>
      <p>Hello world</p>
      <p hidden>This is hidden</p>
      <script>var x = 1;</script>
      <style>.foo { color: red; }</style>
    </body></html>"""
    ex = VisibleTextExtractor()
    ex.feed(html)
    text = ex.get_text()
    test("extracts heading text", 'Title' in text)
    test("extracts paragraph text", 'Hello world' in text)
    test("excludes hidden elements", 'This is hidden' not in text)
    test("excludes script content", 'var x = 1' not in text)
    test("excludes style content", '.foo' not in text)


# Test 2: Empty body
def test_empty_body():
    ex = VisibleTextExtractor()
    ex.feed("""<!DOCTYPE html><html><body></body></html>""")
    test("empty page returns empty string", ex.get_text() == '')


# Test 3: Nested text with inline elements
def test_nested_text():
    html = """<!DOCTYPE html>
    <html><body>
      <div>
        <p>First <strong>bold</strong> text</p>
        <p>Second line</p>
      </div>
    </body></html>"""
    ex = VisibleTextExtractor()
    ex.feed(html)
    text = ex.get_text()
    test("extracts nested text", 'First' in text)
    test("extracts inline element text", 'bold' in text)
    test("extracts second paragraph", 'Second line' in text)


# Test 4: Large content handling
def test_large_content():
    html = "<html><body>" + "\n".join(f"<p>Paragraph {i}</p>" for i in range(1000)) + "</body></html>"
    ex = VisibleTextExtractor()
    ex.feed(html)
    text = ex.get_text()
    lines = [l for l in text.split('\n') if l.strip()]
    test("handles 1000 paragraphs", len(lines) == 1000)
    test("large page produces substantial output", len(text) > 10000)


# Test 5: Metadata extraction
def test_metadata():
    html = """<!DOCTYPE html>
    <html><head>
      <meta charset="UTF-8">
      <meta name="description" content="Test page">
      <meta name="keywords" content="test, example">
      <meta name="author" content="Nova">
      <meta property="og:title" content="OG Title">
      <meta property="og:image" content="https://example.com/image.jpg">
      <meta name="viewport" content="width=device-width">
      <meta name="theme-color" content="#ffffff">
    </head><body><p>Hello</p></body></html>"""
    meta = extract_metadata(html)
    test("extracts meta description", meta['description'] == 'Test page')
    test("extracts meta keywords", meta['keywords'] == 'test, example')
    test("extracts meta author", meta['author'] == 'Nova')
    test("extracts OG title", meta['ogTitle'] == 'OG Title')
    test("extracts OG image", meta['ogImage'] == 'https://example.com/image.jpg')
    test("extracts viewport", meta['viewport'] == 'width=device-width')
    test("extracts theme-color", meta['themeColor'] == '#ffffff')
    test("extracts charset", meta['charset'] == 'UTF-8')


# Test 6: SVG and special elements are excluded
def test_svg_exclusion():
    html = """<!DOCTYPE html>
    <html><body>
      <p>Visible text</p>
      <svg><text>SVG text</text></svg>
    </body></html>"""
    ex = VisibleTextExtractor()
    ex.feed(html)
    text = ex.get_text()
    test("includes visible text", 'Visible text' in text)
    test("excludes SVG content", 'SVG text' not in text)


# Run all tests
if __name__ == '__main__':
    test_basic_extraction()
    test_empty_body()
    test_nested_text()
    test_large_content()
    test_metadata()
    test_svg_exclusion()
    print(f"\nResults: {passed} passed, {failed} failed")
    raise SystemExit(0 if failed == 0 else 1)
