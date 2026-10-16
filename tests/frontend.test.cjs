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

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const appJsPath = path.join(__dirname, '..', 'cabinet', 'static', 'app.js');
const appJs = fs.readFileSync(appJsPath, 'utf8');
const styleCss = fs.readFileSync(path.join(__dirname, '..', 'cabinet', 'static', 'style.css'), 'utf8');

function sliceFn(source, start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end));
}

test('app.js parses as a script', () => {
  execFileSync('node', ['--check', appJsPath]);
});

test('Score pair and admin hash helpers are wired without dead student table', () => {
  const route = sliceFn(appJs, 'async function route()', 'function showSandbox');
  const userbar = sliceFn(appJs, 'async function renderUserbar', 'function returnToHome');
  assert.match(appJs, /function adminLocation\(/);
  assert.match(appJs, /function isAdminHash\(/);
  assert.match(appJs, /function scorePairMarkup\(/);
  assert.match(appJs, /function paintScorePair\(/);
  assert.match(appJs, /со штрафом/);
  assert.match(appJs, /results_final/);
  assert.match(appJs, /data-score-pair/);
  assert.match(route, /isAdminHash\(\)/);
  assert.match(userbar, /isAdminHash\(\)/);
  assert.doesNotMatch(route, /location\.hash === '#admin'/);
  assert.doesNotMatch(userbar, /location\.hash === '#admin'/);
  assert.doesNotMatch(appJs, /async function loadStudents\(/);
  assert.doesNotMatch(appJs, /function studentDetail\(/);
  assert.doesNotMatch(appJs, /#best-score/);
  assert.doesNotMatch(appJs, /textContent\.split\('\/'\)/);
  assert.doesNotMatch(appJs, /task-result-card \$\{[^}]+\}"><span>Результат<\/span>/);
  assert.doesNotMatch(appJs, /score-pair-max">из /);
  assert.match(appJs, /size: 'plain'/);
  assert.match(styleCss, /\.score-pair\s*\{/);
  assert.match(styleCss, /\.score-pair-plain/);
  assert.doesNotMatch(styleCss, /\.homework-score\s*\{[^}]*color:\s*var\(--accent\)/);
});

test('Queue and export load homework lists from coursework API', () => {
  const queue = sliceFn(appJs, 'async function renderAdminQueue', 'function paintQueue');
  const exported = sliceFn(appJs, 'async function renderAdminExport', 'async function renderAdminCourse');
  assert.match(queue, /\/api\/homeworks/);
  assert.match(exported, /\/api\/homeworks/);
  assert.doesNotMatch(queue, /\/api\/admin\/manifest/);
  assert.doesNotMatch(exported, /\/api\/admin\/manifest/);
  assert.match(appJs, /\/api\/admin\/manifest/);
});
