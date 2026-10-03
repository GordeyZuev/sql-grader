const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

global.window = { location: { href: 'http://localhost:8000/' } };
require(path.join(__dirname, '..', 'cabinet', 'static', 'markdown.js'));

const render = global.window.CabinetMarkdown.render;

test('Markdown task content renders headings, emphasis, lists, and SQL code spans', () => {
  const html = render('### Required\n\nUse **LEFT JOIN** and `airport_code`.\n\n- Keep unmatched rows\n- Sort by code');
  assert.match(html, /<h3>Required<\/h3>/);
  assert.match(html, /<strong>LEFT JOIN<\/strong>/);
  assert.match(html, /<code>airport_code<\/code>/);
  assert.match(html, /<ul>[\s\S]*<li>Keep unmatched rows<\/li>[\s\S]*<li>Sort by code<\/li>/);
});

test('Markdown normalizes long dashes to the UI typography standard', () => {
  assert.match(render('left — right'), /left – right/);
  assert.doesNotMatch(render('left — right'), /—/);
});

test('Markdown renderer escapes raw HTML and rejects unsafe link protocols', () => {
  const html = render('<script>alert(1)</script> [bad](javascript:alert(1)) [docs](https://example.test)');
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /href="https:\/\/example\.test\//);
});

test('Markdown tables and fenced SQL examples render as readable blocks', () => {
  const html = render('| name | score |\n| --- | --- |\n| Ada | 1 |\n\n```sql\nSELECT 1;\n```');
  assert.match(html, /<table class="markdown-table">/);
  assert.match(html, /<td>Ada<\/td>/);
  assert.match(html, /class="language-sql"/);
  assert.match(html, /SELECT 1;/);
});
