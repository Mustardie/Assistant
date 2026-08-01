/**
 * Unit tests for the page text extraction logic.
 * Tests the functions that run inside the injected content script.
 *
 * Run with: node tests/page_extraction.test.js
 * Requires: npm install jsdom
 */

const { JSDOM } = require('jsdom');

/**
 * Extracts visible text from a DOM tree, excluding hidden elements,
 * script/style tags, and other non-content elements.
 * This is a testable replica of the logic in handlers/page.js.
 */
function extractVisibleText(dom) {
  const hiddenTags = new Set([
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH',
    'META', 'LINK', 'TITLE', 'HEAD', 'IFRAME',
  ]);

  function isHidden(el) {
    if (el.hidden) return true;
    if (el.getAttribute('aria-hidden') === 'true') return true;
    const style = dom.window.getComputedStyle(el);
    if (style.display === 'none') return true;
    if (style.visibility === 'hidden') return true;
    return false;
  }

  const parts = [];
  const walker = dom.window.document.createTreeWalker(
    dom.window.document.body,
    dom.window.NodeFilter.SHOW_ELEMENT | dom.window.NodeFilter.SHOW_TEXT,
    {
      acceptNode(node) {
        if (node.nodeType === dom.window.Node.ELEMENT_NODE) {
          if (hiddenTags.has(node.tagName)) return dom.window.NodeFilter.FILTER_REJECT;
          if (isHidden(node)) return dom.window.NodeFilter.FILTER_REJECT;
        }
        if (node.nodeType === dom.window.Node.TEXT_NODE) {
          if (!node.textContent.trim()) return dom.window.NodeFilter.FILTER_REJECT;
        }
        return dom.window.NodeFilter.FILTER_ACCEPT;
      },
    },
    false
  );

  while (walker.nextNode()) {
    if (walker.currentNode.nodeType === dom.window.Node.TEXT_NODE) {
      parts.push(walker.currentNode.textContent.trim());
    }
  }

  return parts.join('\n').replace(/\n{4,}/g, '\n\n\n').trim();
}

function extractMetadata(dom) {
  const meta = {};
  dom.window.document.querySelectorAll('meta').forEach(el => {
    const key = el.getAttribute('name') || el.getAttribute('property') || '';
    const content = el.getAttribute('content') || '';
    if (key && content) meta[key] = content;
  });

  return {
    description: meta.description || meta['og:description'] || '',
    keywords: meta.keywords || '',
    author: meta.author || '',
    viewport: meta.viewport || '',
    charset: dom.window.document.characterSet || '',
    ogTitle: meta['og:title'] || '',
    ogDescription: meta['og:description'] || '',
    ogImage: meta['og:image'] || '',
    ogUrl: meta['og:url'] || '',
    twitterCard: meta['twitter:card'] || '',
    themeColor: meta['theme-color'] || '',
  };
}

// ---- Tests ----

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) {
    console.log(`  PASS  ${msg}`);
    passed++;
  } else {
    console.error(`  FAIL  ${msg}`);
    failed++;
  }
}

// Test 1: Basic visible text extraction
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><body>
      <h1>Title</h1>
      <p>Hello world</p>
      <p hidden>This is hidden</p>
      <script>var x = 1;</script>
      <style>.foo { color: red; }</style>
    </body></html>
  `);
  const text = extractVisibleText(dom);
  assert(text.includes('Title'), 'extracts heading text');
  assert(text.includes('Hello world'), 'extracts paragraph text');
  assert(!text.includes('This is hidden'), 'excludes hidden elements');
  assert(!text.includes('var x = 1'), 'excludes script content');
  assert(!text.includes('.foo'), 'excludes style content');
}

// Test 2: aria-hidden elements
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><body>
      <p>Visible</p>
      <p aria-hidden="true">Screen reader hidden</p>
    </body></html>
  `);
  const text = extractVisibleText(dom);
  assert(text.includes('Visible'), 'includes visible content');
  assert(!text.includes('Screen reader hidden'), 'excludes aria-hidden elements');
}

// Test 3: display:none elements
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><body>
      <p>Visible</p>
      <p style="display:none">Display none</p>
      <p style="visibility:hidden">Visibility hidden</p>
    </body></html>
  `);
  const text = extractVisibleText(dom);
  assert(text.includes('Visible'), 'includes visible content');
  assert(!text.includes('Display none'), 'excludes display:none');
  assert(!text.includes('Visibility hidden'), 'excludes visibility:hidden');
}

// Test 4: Metadata extraction
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><head>
      <meta charset="UTF-8">
      <meta name="description" content="Test page">
      <meta name="keywords" content="test, example">
      <meta name="author" content="Nova">
      <meta property="og:title" content="OG Title">
      <meta property="og:image" content="https://example.com/image.jpg">
      <meta name="viewport" content="width=device-width">
      <meta name="theme-color" content="#ffffff">
    </head><body><p>Hello</p></body></html>
  `);
  const meta = extractMetadata(dom);
  assert(meta.description === 'Test page', 'extracts meta description');
  assert(meta.keywords === 'test, example', 'extracts meta keywords');
  assert(meta.author === 'Nova', 'extracts meta author');
  assert(meta.ogTitle === 'OG Title', 'extracts OG title');
  assert(meta.ogImage === 'https://example.com/image.jpg', 'extracts OG image');
  assert(meta.viewport === 'width=device-width', 'extracts viewport');
  assert(meta.themeColor === '#ffffff', 'extracts theme-color');
  assert(meta.charset === 'UTF-8', 'extracts charset');
}

// Test 5: Empty page
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body></body></html>`);
  const text = extractVisibleText(dom);
  assert(text === '', 'empty page returns empty string');
}

// Test 6: Nested text
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><body>
      <div>
        <p>First <strong>bold</strong> text</p>
        <p>Second line</p>
      </div>
    </body></html>
  `);
  const text = extractVisibleText(dom);
  assert(text.includes('First'), 'extracts text from nested elements');
  assert(text.includes('bold'), 'extracts text from nested inline elements');
  assert(text.includes('Second line'), 'extracts second paragraph');
}

// Test 7: SVG and special elements are skipped
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><body>
      <p>Visible text</p>
      <svg><text>SVG text</text></svg>
    </body></html>
  `);
  const text = extractVisibleText(dom);
  assert(text.includes('Visible text'), 'includes visible text');
  assert(!text.includes('SVG text'), 'excludes SVG content');
}

// Test 8: Large content handling (via chunking simulation)
{
  const dom = new JSDOM(`
    <!DOCTYPE html>
    <html><body>
      ${'<p>Content</p>\n'.repeat(10000)}
    </body></html>
  `);
  const text = extractVisibleText(dom);
  const lines = text.split('\n').filter(l => l.trim());
  assert(lines.length === 10000, 'handles 10000 paragraphs');
  assert(text.length > 50000, 'large page produces substantial text output');
}

// Summary
console.log(`\nResults: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
