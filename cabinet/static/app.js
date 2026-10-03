const app = document.querySelector('#app');
const userbar = document.querySelector('#userbar');
let token = localStorage.getItem('cabinet_token');
let authMode = 'login';
let currentUser = null;
let usernameCheckTimer;
const draftTimers = new Map();
const pendingDrafts = new Map();
const draftWrites = new Map();
const draftRequests = new Map();
let taskHistoryOffset = 0;
let auditOffset = 0;

const escapeHtml = (value) => String(value ?? '').replaceAll('\u2014', '–').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
})[char]);

function dataTableMarkup(columns, rows) {
  const header = `<th class="row-number">#</th>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('')}`;
  const body = rows.map((row, index) => `<tr><th class="row-number">${index + 1}</th>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('');
  return `<div class="result-table-scroll" role="region" aria-label="Таблица результата. Прокручивайте по горизонтали и вертикали" tabindex="0"><table class="result-table"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function textTableMarkup(columns, rows, count = rows.length) {
  const headers = ['#', ...columns];
  const values = rows.map((row, index) => [String(index + 1), ...row.map((cell) => cell === null ? 'NULL' : String(cell))]);
  const widths = headers.map((header, col) => Math.min(40, Math.max(header.length, ...values.map((row) => row[col]?.length || 0))));
  const cell = (value, index) => String(value).slice(0, widths[index]).padEnd(widths[index]);
  const border = `+${widths.map((width) => '-'.repeat(width + 2)).join('+')}+`;
  const line = (row) => `| ${row.map(cell).join(' | ')} |`;
  const footer = count > rows.length
    ? `(${rows.length} rows shown, ${count} total)`
    : `(${count} ${count === 1 ? 'row' : 'rows'})`;
  return [border, line(headers), border, ...values.map(line), border, footer].join('\n');
}

window.CabinetApp = { escape: escapeHtml, dataTable: dataTableMarkup, textTable: textTableMarkup };

function bindPasswordToggles(root = document) {
  root.querySelectorAll('.password-toggle[data-target]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.target);
      if (!input) return;
      const visible = input.type === 'password';
      const secret = button.dataset.secretLabel || 'пароль';
      input.type = visible ? 'text' : 'password';
      button.innerHTML = visible
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18M10.6 5.2A10.7 10.7 0 0 1 12 5c6.4 0 10 7 10 7a14 14 0 0 1-3.1 3.8M6.2 6.2C3.5 8 2 12 2 12s3.6 7 10 7c1.2 0 2.3-.3 3.3-.7"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
      button.setAttribute('aria-pressed', String(visible));
      button.setAttribute('aria-label', `${visible ? 'Скрыть' : 'Показать'} ${secret}`);
      button.title = `${visible ? 'Скрыть' : 'Показать'} ${secret}`;
    });
  });
}

function errorText(body, fallback = 'Ошибка запроса') {
  const detail = body?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const text = detail.map((item) => item?.msg).filter(Boolean).join('. ');
    if (text) return text;
  }
  return fallback;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  let body;
  try {
    body = await response.json();
  } catch {
    body = { detail: response.statusText };
  }
  if (!response.ok) {
    const error = new Error(errorText(body));
    error.status = response.status;
    throw error;
  }
  return body;
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('cabinet_theme', theme);
  const button = document.querySelector('#theme-toggle');
  if (button) {
    const isDark = theme === 'dark';
    button.innerHTML = isDark
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path class="theme-rays" d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.2 15.4A8.3 8.3 0 0 1 8.6 3.8 8.5 8.5 0 1 0 20.2 15.4Z"/></svg>';
    button.title = isDark ? 'Включить светлую тему' : 'Включить тёмную тему';
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-pressed', String(isDark));
  }
}

document.querySelector('#theme-toggle').addEventListener('click', () => {
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});
setTheme(localStorage.getItem('cabinet_theme') || 'light');

function staffEntryMarkup() {
  return `<div class="role-entries"><button type="button" class="role-entry" data-mode="assistant"><strong>Войти как ассистент</strong><span>Логин и пароль. Без прав ассистента вход закрыт</span></button><button type="button" class="role-entry" data-mode="administrator"><strong>Войти как администратор</strong><span>Логин, пароль и ключ</span></button></div>`;
}

function showAuth(error = '') {
  token = null;
  currentUser = null;
  localStorage.removeItem('cabinet_token');
  sessionStorage.removeItem('cabinet_admin_token');
  sessionStorage.removeItem('cabinet_admin_session');
  sessionStorage.removeItem('cabinet_admin_name');
  userbar.textContent = '';
  const staff = authMode === 'assistant' || authMode === 'administrator';
  const title = authMode === 'assistant' ? 'Вход ассистента' : authMode === 'administrator' ? 'Вход администратора' : 'SQL Trainer';
  const lead = authMode === 'assistant'
    ? 'Логин и пароль. Если у учётки нет прав ассистента, вход не откроется.'
    : authMode === 'administrator'
      ? 'Те же логин и пароль и ещё ключ. Администратор публикует курс, смотрит журнал и выдаёт права.'
      : 'Войдите в кабинет или создайте аккаунт студента.';
  const keyField = authMode === 'administrator'
    ? '<label class="field">Ключ<div class="password-control"><input id="staff-phrase-input" name="phrase" type="password" required autocomplete="off"><button class="password-toggle" type="button" data-target="staff-phrase-input" data-secret-label="ключ" aria-label="Показать ключ" aria-pressed="false" title="Показать ключ"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></div></label>'
    : '';
  const form = `<form id="auth-form" class="auth-form" onsubmit="event.preventDefault()"><label class="field ${authMode === 'register' ? '' : 'hidden'}" id="name-field">ФИО<input name="full_name" autocomplete="name" ${authMode === 'register' ? 'required' : ''} placeholder="Иван Иванов"></label><label class="field">Логин<input id="auth-username" name="username" required minlength="4" maxlength="32" ${authMode === 'register' ? 'pattern="[A-Za-z0-9._-]{4,32}"' : ''} autocomplete="username" placeholder="Например, ivan_26"><span id="username-hint" class="field-hint">${authMode === 'register' ? '4–32 символа: латинские буквы, цифры, точка, дефис или подчёркивание' : ''}</span></label><label class="field">Пароль<div class="password-control"><input id="password" name="password" type="password" minlength="8" required autocomplete="${authMode === 'register' ? 'new-password' : 'current-password'}"><button class="password-toggle" type="button" data-target="password" aria-label="Показать пароль" aria-pressed="false" title="Показать пароль"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></div>${authMode === 'register' ? '<div class="password-guide"><span id="password-length" class="password-rule">Не менее 8 символов</span></div>' : ''}</label>${authMode === 'register' ? '<label class="field">Повторите пароль<div class="password-control"><input id="password-confirm" name="password_confirm" type="password" minlength="8" required autocomplete="new-password"><button class="password-toggle" type="button" data-target="password-confirm" aria-label="Показать пароль" aria-pressed="false" title="Показать пароль"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></div><div class="password-guide"><span id="password-match" class="password-rule">Пароли должны совпадать</span></div></label>' : ''}${keyField}<button class="primary auth-submit" id="auth-submit">${authMode === 'register' ? 'Создать аккаунт' : authMode === 'login' ? 'Войти в кабинет' : 'Войти в панель'}</button></form>`;
  app.innerHTML = `
    <section class="auth-card">
      <div class="auth-mark">SQL Trainer</div>
      ${staff ? `<h1>${title}</h1>` : ''}
      <p class="muted">${lead}</p>
      ${staff ? '' : '<div class="tabs"><button class="' + (authMode === 'login' ? 'active' : '') + '" data-mode="login">Вход</button><button class="' + (authMode === 'register' ? 'active' : '') + '" data-mode="register">Регистрация</button></div>'}
      ${form}
      ${authMode === 'login' ? '<p class="forgot-password">Забыли пароль? Обратитесь к ассистенту для сброса пароля.</p>' : ''}
      <div id="notice" class="notice ${error ? 'error-notice' : ''}" role="status">${escapeHtml(error)}</div>
      ${staff ? `<button type="button" class="text-button" data-mode="login">Вход студента</button><button type="button" class="text-button" data-mode="${authMode === 'assistant' ? 'administrator' : 'assistant'}">${authMode === 'assistant' ? 'Войти как администратор' : 'Войти как ассистент'}</button>` : staffEntryMarkup()}
    </section>`;

  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      authMode = button.dataset.mode;
      showAuth();
    });
  });
  document.querySelector('#auth-form').addEventListener('submit', authSubmit);
  const usernameInput = document.querySelector('#auth-username');
  if (authMode === 'register' && usernameInput) usernameInput.addEventListener('input', () => {
    clearTimeout(usernameCheckTimer);
    const username = usernameInput.value.trim();
    const hint = document.querySelector('#username-hint');
    if (!/^[A-Za-z0-9._-]{4,32}$/.test(username)) {
      hint.textContent = username.length < 4 ? 'Введите не менее 4 символов' : 'Только латинские буквы, цифры, точка, дефис и подчёркивание';
      hint.className = 'field-hint invalid-hint';
      return;
    }
    hint.textContent = 'Проверяем логин…';
    hint.className = 'field-hint';
    usernameCheckTimer = setTimeout(async () => {
      try {
        const result = await api(`/api/auth/username-availability?username=${encodeURIComponent(username)}`);
        if (usernameInput.value.trim() !== username) return;
        hint.textContent = result.available ? 'Логин свободен' : 'Логин уже занят';
        hint.className = `field-hint ${result.available ? 'valid-hint' : 'invalid-hint'}`;
      } catch { hint.textContent = 'Не удалось проверить логин'; }
    }, 280);
  });
  bindPasswordToggles(app);
  const password = document.querySelector('#password');
  const confirmation = document.querySelector('#password-confirm');
  if (confirmation) {
    const updatePasswordDetails = () => {
      const longEnough = password.value.length >= 8;
      const matches = confirmation.value.length > 0 && confirmation.value === password.value;
      const lengthHint = document.querySelector('#password-length');
      const matchHint = document.querySelector('#password-match');
      lengthHint.textContent = longEnough ? 'Длина подходит' : `Ещё ${8 - password.value.length} символов`;
      lengthHint.className = `password-rule ${longEnough ? 'valid' : ''}`;
      matchHint.textContent = matches ? 'Пароли совпадают' : 'Пароли должны совпадать';
      matchHint.className = `password-rule ${matches ? 'valid' : ''}`;
    };
    password.addEventListener('input', updatePasswordDetails);
    confirmation.addEventListener('input', updatePasswordDetails);
  }
}

async function authSubmit(event) {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  if (authMode === 'register') {
    if (values.password.length < 8) {
      const hint = document.querySelector('#password-length');
      if (hint) {
        hint.textContent = `Ещё ${8 - values.password.length} символов`;
        hint.className = 'password-rule invalid';
      }
      document.querySelector('#password').focus();
      return;
    }
    if (values.password !== values.password_confirm) {
      document.querySelector('#notice').textContent = 'Пароли не совпадают.';
      return;
    }
    delete values.password_confirm;
  }
  const staff = authMode === 'assistant' || authMode === 'administrator';
  const path = authMode === 'assistant' ? '/api/auth/assistant' : authMode === 'administrator' ? '/api/auth/administrator' : `/api/auth/${authMode}`;
  const payload = authMode === 'administrator'
    ? { username: values.username, password: values.password, phrase: String(values.phrase || '').trim() }
    : values;
  try {
    const result = await api(path, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    token = result.token;
    localStorage.setItem('cabinet_token', token);
    if (staff) {
      authMode = 'login';
      if (location.hash === '#admin') await route();
      else location.hash = '#admin';
      return;
    }
    await route();
  } catch (error) {
    const notice = document.querySelector('#notice');
    if (notice) {
      notice.textContent = error.message;
      notice.className = 'notice error-notice';
    }
  }
}

function formatDate(value) {
  const date = new Date(new Date(value).getTime() + 3 * 60 * 60 * 1000);
  const two = (part) => String(part).padStart(2, '0');
  return `${two(date.getUTCDate())}.${two(date.getUTCMonth() + 1)}.${String(date.getUTCFullYear()).slice(-2)} ${two(date.getUTCHours())}:${two(date.getUTCMinutes())}`;
}

async function renderUserbar(student = null) {
  if (!student) student = await api('/api/me');
  currentUser = student;
  const onPanel = location.hash === '#admin' || location.hash.startsWith('#admin/');
  const onProfile = location.hash === '#profile';
  const onStudy = !onPanel && !onProfile;
  const switcher = student.is_assistant || student.is_superadmin
    ? `<nav class="place-switch" aria-label="Разделы"><a href="#home" class="${onStudy ? 'active' : ''}" ${onStudy ? 'aria-current="page"' : ''}>Задания</a><a href="#admin" class="${onPanel ? 'active' : ''}" ${onPanel ? 'aria-current="page"' : ''}>Панель</a></nav>`
    : '';
  userbar.innerHTML = `${switcher}<span class="user-identity">${escapeHtml(student.full_name)} <span>${escapeHtml(student.username)}</span></span><button class="secondary compact${onProfile ? ' is-current' : ''}" id="profile-edit" type="button">Профиль</button><button class="secondary compact" id="session-logout" type="button">Выйти</button>`;
  document.querySelector('#profile-edit').addEventListener('click', () => { location.hash = '#profile'; });
  document.querySelector('#session-logout').addEventListener('click', logoutStudent);
  return student;
}

function returnToHome() {
  location.hash = '#home';
}

async function logoutStudent() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* expired session */ }
  authMode = 'login';
  window.history.replaceState(null, '', `${location.pathname}${location.search}#home`);
  showAuth();
}

function formatPoints(value) {
  return Number(value).toFixed(2);
}

function scoreTone(score, maximum) {
  const value = Number(score);
  const total = Number(maximum);
  if (total > 0 && value >= total) return 'is-complete';
  if (value > 0) return 'is-partial';
  return 'is-zero';
}

function homeworkTone(homework) {
  const { complete } = homeworkStatus(homework);
  if (homework.tasks.length > 0 && complete === homework.tasks.length) return 'complete';
  if (complete > 0 || Number(homework.score) > 0) return 'partial';
  return '';
}

function paintScoreState(row, card, score, maximum) {
  const tone = scoreTone(score, maximum);
  const done = tone === 'is-complete';
  const partial = tone === 'is-partial';
  if (card) {
    card.classList.toggle('is-complete', done);
    card.classList.toggle('is-partial', partial);
    card.classList.toggle('is-zero', !done && !partial);
  }
  if (!row) return;
  row.classList.toggle('done', done);
  row.classList.toggle('partial', partial);
  row.classList.toggle('is-zero', !done && !partial);
  const title = row.querySelector('.task-nav-title')?.textContent || '';
  const index = row.querySelector('.task-nav-index')?.textContent || '';
  row.setAttribute('aria-label', `${index}, ${title}, ${done ? 'выполнено' : partial ? 'частично' : '0 баллов'}`);
  let state = row.querySelector('.task-nav-state');
  if (!state) {
    state = document.createElement('span');
    state.className = 'task-nav-state';
    row.append(state);
  }
  state.title = done ? 'Выполнено' : partial ? 'Частично' : '0 баллов';
  state.replaceChildren();
}

function animatePoints(element, from, to, maximum) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    element.textContent = `${formatPoints(to)} / ${formatPoints(maximum)}`;
    return;
  }
  const startedAt = performance.now();
  const duration = 720;
  function frame(now) {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - (1 - progress) ** 3;
    element.textContent = `${formatPoints(from + (to - from) * eased)} / ${formatPoints(maximum)}`;
    if (progress < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function celebrateCompletion(card) {
  if (!card || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const burst = document.createElement('span');
  burst.className = 'completion-burst';
  burst.setAttribute('aria-hidden', 'true');
  burst.innerHTML = Array.from({ length: 12 }, (_, index) => `<i style="--angle:${index * 30}deg"></i>`).join('');
  card.append(burst);
  window.setTimeout(() => burst.remove(), 950);
}

function russianCount(value, forms) {
  const numeric = Number(value);
  if (!Number.isInteger(numeric)) return `${value} ${forms[1]}`;
  const count = Math.abs(numeric) % 100;
  const last = count % 10;
  const form = count > 10 && count < 20 ? forms[2] : last > 1 && last < 5 ? forms[1] : last === 1 ? forms[0] : forms[2];
  return `${value} ${form}`;
}

function verdictTitle(verdict) {
  return ({
    OK: 'Верно', STRUCT: 'Частично', WRONG: 'Неверно',
    SYNTAX: 'Ошибка синтаксиса', ERROR: 'Ошибка выполнения', EMPTY: 'Неверно',
    FORBID: 'Запрещённая команда', HARD_LATE: 'Проверка закрыта',
  })[verdict] || verdict;
}

function queryStatusTitle(status) {
  if (!status) return '';
  return ({
    ok: 'выполнен', error: 'ошибка', rejected: 'отклонён', blocked: 'заблокирован',
    not_run: 'не запускался',
  })[status] || '';
}

const expandIcon = '<svg class="attempt-expand" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function historyDetail({ meta, reason = '', sql, table = '', restoreClass = '', restoreLabel = '', restoreAttrs = '' }) {
  const query = `<div class="attempt-block"><span class="attempt-label">Запрос</span><pre><code>${escapeHtml(sql)}</code></pre></div>`;
  const output = table ? `<div class="attempt-block"><span class="attempt-label">Результат</span>${table}</div>` : '';
  const restore = restoreLabel ? `<button type="button" class="secondary ${restoreClass}" ${restoreAttrs}>${restoreLabel}</button>` : '';
  return `<div class="attempt-detail">${meta}${reason}${query}${output}${restore}</div>`;
}

function attemptMarkup(attempt, index, open = false, restorable = false) {
  const result = attempt.result;
  const table = resultTableMarkup(result);
  const reason = attempt.verdict !== 'OK' && attempt.message
    ? `<p class="attempt-reason">${escapeHtml(attempt.message)}</p>` : '';
  const verdictClass = attempt.verdict === 'OK' ? 'verdict-success' : attempt.verdict === 'STRUCT' ? 'verdict-partial' : 'verdict-error';
  const pointsClass = Number(attempt.points) > 0 ? '' : 'is-zero';
  const statusLabel = queryStatusTitle(attempt.query_status || attempt.status);
  return `<details class="attempt-card" ${open ? 'open' : ''}><summary><span class="attempt-index">Попытка ${index}</span><span class="attempt-verdict ${verdictClass}">${escapeHtml(verdictTitle(attempt.verdict))}</span><span class="attempt-points ${pointsClass}">${russianCount(attempt.points, ['балл', 'балла', 'баллов'])}</span><span class="attempt-time">${formatDate(attempt.created_at)}</span>${expandIcon}</summary>${historyDetail({
    meta: `<div class="attempt-meta"><span>${attempt.row_count ?? 0} строк</span><span>${attempt.elapsed_ms ?? 0} мс</span>${statusLabel ? `<span>${escapeHtml(statusLabel)}</span>` : ''}</div>`,
    reason,
    sql: attempt.sql,
    table,
    restoreClass: 'restore-task-query',
    restoreLabel: restorable ? 'Вернуть запрос в редактор' : '',
  })}</details>`;
}

function resultTableMarkup(result) {
  if (result?.error) return '';
  if (!result?.columns?.length) return '';
  const mode = ['simple', 'psql'].includes(localStorage.getItem('cabinet_result_mode')) ? 'simple' : 'pretty';
  const content = mode === 'simple'
    ? `<pre class="psql-output">${escapeHtml(textTableMarkup(result.columns, result.rows || [], result.row_count))}</pre>`
    : dataTableMarkup(result.columns, result.rows || []);
  return `<div class="attempt-output">${content}${result.truncated ? '<span class="field-hint">Показаны первые 200 строк</span>' : ''}</div>`;
}

function runMarkup(run, index, restorable = false) {
  return `<details class="attempt-card"><summary><span class="attempt-index">Запуск ${index}</span><span class="attempt-verdict ${run.status === 'ok' ? 'verdict-success' : 'verdict-error'}">${run.status === 'ok' ? 'Выполнен' : 'Ошибка'}</span><span class="attempt-points">${run.row_count ?? 0} строк</span><span class="attempt-time">${formatDate(run.created_at)}</span>${expandIcon}</summary>${historyDetail({
    meta: `<div class="attempt-meta"><span>${run.elapsed_ms ?? 0} мс</span></div>`,
    reason: run.message ? `<p class="attempt-reason">${escapeHtml(run.message)}</p>` : '',
    sql: run.sql,
    table: resultTableMarkup(run.result),
    restoreClass: 'restore-task-query',
    restoreLabel: restorable ? 'Вернуть запрос в редактор' : '',
  })}</details>`;
}

function breadcrumbMarkup(items) {
  return `<nav class="page-breadcrumb" aria-label="Навигация">${items.map((item, index) => {
    const content = item.href
      ? `<a href="${escapeHtml(item.href)}">${escapeHtml(item.label)}</a>`
      : `<span aria-current="page">${escapeHtml(item.label)}</span>`;
    return `${index ? '<span class="breadcrumb-separator" aria-hidden="true">/</span>' : ''}${content}`;
  }).join('')}</nav>`;
}

function homeworkStatus(homework) {
  const complete = homework.tasks.filter((task) => Number(task.score) >= Number(task.max_points)).length;
  return { complete };
}

function taskDisplayTitle(task) {
  if (task.title && task.title !== task.id) return task.title;
  const firstLine = String(task.statement || '').split('\n')[0];
  const title = firstLine.replace(/`([^`]+)`/g, '$1').replace(/\*\*/g, '').trim();
  return title.length > 78 ? `${title.slice(0, 75).trimEnd()}…` : title;
}

function taskStateMarkup(done, partial) {
  const title = done ? 'Выполнено' : partial ? 'Частично' : '0 баллов';
  return `<span class="task-nav-state" title="${title}"></span>`;
}

function taskRailMarkup(homeworkId, homeworkTitle, tasks, currentTaskId = '') {
  return `<nav class="task-rail" aria-label="Задачи домашнего задания"><div class="task-rail-heading"><span class="eyebrow">${escapeHtml(homeworkId.toUpperCase())}</span><strong>${escapeHtml(homeworkTitle)}</strong></div><div class="task-rail-list">${tasks.map((task) => {
    const done = Number(task.score) >= Number(task.max_points);
    const active = task.id === currentTaskId;
    const partial = Number(task.score) > 0 && !done;
    const title = taskDisplayTitle(task);
    const status = done ? 'выполнено' : partial ? 'частично' : '0 баллов';
    return `<a class="task-nav-row ${active ? 'active' : ''} ${done ? 'done' : partial ? 'partial' : 'is-zero'}" href="#task/${encodeURIComponent(homeworkId)}/${encodeURIComponent(task.id)}" aria-label="${escapeHtml(task.id)}, ${escapeHtml(title)}, ${status}" ${active ? 'aria-current="page"' : ''}><span class="task-nav-index">${escapeHtml(task.id)}</span><span class="task-nav-title">${escapeHtml(title)}</span>${taskStateMarkup(done, partial)}</a>`;
  }).join('')}</div></nav>`;
}

function renderHomework(homework) {
  const status = homeworkStatus(homework);
  const percent = homework.tasks.length ? Math.round(status.complete / homework.tasks.length * 100) : 0;
  const toneClass = { complete: 'is-complete', partial: 'is-partial' }[homeworkTone(homework)] || '';
  app.innerHTML = `<div class="page-shell homework-page">
    ${breadcrumbMarkup([{ label: 'Главная', href: '#home' }, { label: `${homework.id.toUpperCase()} · ${homework.title}` }])}
    <div class="homework-layout">${taskRailMarkup(homework.id, homework.title, homework.tasks)}<section class="homework-overview"><div><p class="eyebrow">${escapeHtml(homework.id.toUpperCase())}</p><h1>${escapeHtml(homework.title)}</h1><div class="homework-progress"><div><span>${status.complete} из ${homework.tasks.length} задач решено</span><span>${percent}%</span></div><div class="progress-track ${toneClass}"><span style="width:${percent}%"></span></div></div><div class="homework-overview-deadlines"><span>Мягкий срок <strong>${formatDate(homework.soft_deadline)}</strong></span><span>Жёсткий срок <strong>${formatDate(homework.hard_deadline)}</strong></span></div></div><div class="homework-score ${toneClass} ${Number(homework.score) === 0 ? 'is-zero' : ''}"><strong>${formatPoints(homework.score)}</strong><span>из ${formatPoints(homework.max_points)} баллов</span></div></section></div>
  </div>`;
}

async function renderHome() {
  try {
    const homeworks = await api('/api/homeworks');
    const totalScore = homeworks.reduce((sum, homework) => sum + Number(homework.score), 0);
    const maxScore = homeworks.reduce((sum, homework) => sum + Number(homework.max_points), 0);

    app.innerHTML = `
      <div class="page-shell home-page">
        <section class="home-hero"><h1>Мои задания</h1></section>
        <button type="button" class="sandbox-launch" id="sandbox-open"><span class="sandbox-title">Песочница SQL</span><span class="sandbox-open-label">Открыть</span></button>
        <div class="section-heading homework-list-heading"><h2>Домашние задания</h2><span class="home-total-score">Баллы за курс <strong class="${scoreTone(totalScore, maxScore)}">${formatPoints(totalScore)} / ${formatPoints(maxScore)}</strong></span></div>
        <section class="homework-grid">${homeworks.map((homework) => {
          const status = homeworkStatus(homework);
          const percent = homework.tasks.length ? Math.round(status.complete / homework.tasks.length * 100) : 0;
          const tone = homeworkTone(homework);
          const tileClass = tone === 'complete' ? 'homework-complete' : tone === 'partial' ? 'is-partial' : '';
          const toneClass = tone ? `is-${tone}` : '';
          const scoreClass = Number(homework.score) === 0 ? 'is-zero' : '';
          return `<a class="homework-tile ${tileClass}" href="#homework/${encodeURIComponent(homework.id)}"><div class="homework-tile-heading"><span class="eyebrow">${escapeHtml(homework.id.toUpperCase())}</span><h3>${escapeHtml(homework.title)}</h3><span class="homework-tile-score">Баллы <strong class="${scoreClass}">${formatPoints(homework.score)} / ${formatPoints(homework.max_points)}</strong></span></div><div class="homework-tile-deadlines"><span>Мягкий срок <strong>${formatDate(homework.soft_deadline)}</strong></span><span>Жёсткий срок <strong>${formatDate(homework.hard_deadline)}</strong></span></div><div class="homework-tile-progress"><div class="progress-track ${toneClass}"><span style="width:${percent}%"></span></div><span>${status.complete} из ${homework.tasks.length} задач</span></div></a>`;
        }).join('')}</section>
      </div>`;
    document.querySelector('#sandbox-open').addEventListener('click', () => { location.hash = '#sandbox'; });
  } catch (error) {
    app.innerHTML = `<div class="page-shell"><section class="card-surface"><h1>Курса ещё нет</h1><p class="muted">${escapeHtml(error.message)}</p><p class="muted">Когда администратор опубликует курс, задания появятся здесь.</p></section></div>`;
  }
}

async function renderProfile(student = null) {
  try {
    if (!student) student = await api('/api/me');
    app.innerHTML = `<div class="page-shell profile-page">
      ${breadcrumbMarkup([{ label: 'Главная', href: '#home' }, { label: 'Профиль' }])}
      <section class="profile-card">
        <p class="eyebrow">УЧЁТНАЯ ЗАПИСЬ</p><h1>Профиль</h1>
        <p class="profile-login">Логин <strong>${escapeHtml(student.username)}</strong></p>
        <form id="profile-name-form" class="profile-form"><label class="field">ФИО<input name="full_name" required minlength="2" maxlength="200" value="${escapeHtml(student.full_name)}"></label><button class="primary">Сохранить ФИО</button></form>
        <div class="profile-divider"></div>
        <form id="profile-password-form" class="profile-form">
          <h2>Смена пароля</h2>
          ${[
            ['current-password', 'current_password', 'Текущий пароль', 'current-password', 1],
            ['new-password', 'new_password', 'Новый пароль', 'new-password', 8],
            ['confirm-password', 'password_confirm', 'Повторите новый пароль', 'new-password', 8],
          ].map(([id, name, label, autocomplete, minimum]) => `<label class="field">${label}<div class="password-control"><input id="${id}" name="${name}" type="password" required minlength="${minimum}" autocomplete="${autocomplete}"><button class="password-toggle" type="button" data-target="${id}" aria-label="Показать пароль" title="Показать пароль"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></div>${name === 'new_password' ? '<span class="field-hint">Не менее 8 символов</span>' : ''}</label>`).join('')}
          <button class="primary">Изменить пароль</button>
        </form>
        <div class="profile-divider"></div><button type="button" class="secondary profile-logout" id="profile-logout">Выйти из аккаунта</button><div id="profile-notice" class="notice" role="status"></div>
      </section>
    </div>`;
    bindPasswordToggles(app);
    document.querySelector('#profile-logout').addEventListener('click', logoutStudent);
    document.querySelector('#profile-name-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const fullName = new FormData(event.currentTarget).get('full_name').trim();
      try {
        await api('/api/me', { method: 'PATCH', body: JSON.stringify({ full_name: fullName }) });
        document.querySelector('#profile-notice').textContent = 'ФИО сохранено.';
        currentUser = { ...student, full_name: fullName };
        await renderUserbar(currentUser);
      } catch (error) { document.querySelector('#profile-notice').textContent = error.message; }
    });
    document.querySelector('#profile-password-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      if (values.new_password !== values.password_confirm) {
        document.querySelector('#profile-notice').textContent = 'Новые пароли не совпадают.';
        return;
      }
      try {
        await api('/api/me/password', { method: 'POST', body: JSON.stringify({ current_password: values.current_password, new_password: values.new_password }) });
        event.currentTarget.reset();
        document.querySelector('#profile-notice').textContent = 'Пароль изменён.';
      } catch (error) { document.querySelector('#profile-notice').textContent = error.message; }
    });
  } catch (error) { showAuth(error.message); }
}

const requirementNames = {
  cte: 'CTE · WITH', subquery: 'Подзапрос', subquery_from: 'Подзапрос в FROM',
  join: 'JOIN', inner_join: 'INNER JOIN', left_join: 'LEFT JOIN', right_join: 'RIGHT JOIN',
  full_join: 'FULL JOIN', cross_join: 'CROSS JOIN', exists: 'EXISTS', is_null: 'IS NULL',
  using: 'USING', except: 'EXCEPT', except_all: 'EXCEPT ALL', except_distinct: 'EXCEPT',
  intersect_distinct: 'INTERSECT', intersect_all: 'INTERSECT ALL', union_distinct: 'UNION',
  union_all: 'UNION ALL', two_joins: 'Два соединения', two_left_joins: 'Два LEFT JOIN',
  three_inner_joins: 'Три INNER JOIN', over: 'Оконная функция · OVER', partition_by: 'PARTITION BY',
  rows_between: 'ROWS BETWEEN', row_number: 'ROW_NUMBER', rank: 'RANK', dense_rank: 'DENSE_RANK',
  lag: 'LAG', first_value: 'FIRST_VALUE', last_value: 'LAST_VALUE', ntile: 'NTILE',
  percent_rank: 'PERCENT_RANK', cume_dist: 'CUME_DIST', avg_over: 'AVG OVER',
};

async function renderTask(homeworkId, taskId) {
  try {
    const [data, homeworks] = await Promise.all([
      api(`/api/homeworks/${encodeURIComponent(homeworkId)}/tasks/${encodeURIComponent(taskId)}`),
      api('/api/homeworks'),
    ]);
    const task = data.task;
    const homework = data.homework;
    const homeworkTasks = homeworks.find((item) => item.id === homeworkId)?.tasks || [];
    const required = task.required.map((key) => requirementNames[key] || key);
    app.innerHTML = `
      <div class="page-shell task-page">
        ${breadcrumbMarkup([{ label: 'Главная', href: '#home' }, { label: `${homework.id.toUpperCase()} · ${homework.title}`, href: `#homework/${encodeURIComponent(homeworkId)}` }, { label: `${task.id} · ${taskDisplayTitle(task)}` }])}
        <div class="task-layout">
          ${taskRailMarkup(homework.id, homework.title, homeworkTasks, taskId)}
          <div class="task-workspace">
          <aside class="task-context">
            <section class="context-card problem-card"><div class="context-title"><h2>${escapeHtml(taskDisplayTitle(task))}</h2></div><article class="markdown-body">${window.CabinetMarkdown.render(task.statement)}</article>${required.length ? `<div class="task-requirements"><span class="eyebrow">ОБЯЗАТЕЛЬНО ДЛЯ ПРОВЕРКИ</span><div class="requirement-chips">${required.map((name) => `<span class="requirement-chip">${escapeHtml(name)}</span>`).join('')}</div><p class="requirement-note">За отсутствующие конструкции начисляется частичный балл.</p></div>` : ''}</section>
            <section class="context-card deadline-summary"><p class="eyebrow">СРОКИ · ${escapeHtml(homework.id.toUpperCase())}</p><div class="deadline-values"><div><span>Мягкий срок</span><strong>${formatDate(homework.soft_deadline)}</strong></div><div><span>Жёсткий срок</span><strong>${formatDate(homework.hard_deadline)}</strong></div></div>${homework.grading_open ? '' : '<p class="deadline-status closed">Срок истёк. Чтобы открыть проверку, обратитесь к преподавателю.</p>'}</section>
            <section class="context-card task-result-card ${scoreTone(data.score, task.points)}"><span>Результат</span><strong id="best-score">${formatPoints(data.score)} / ${formatPoints(task.points)}</strong>${data.manual_grade ? `<div class="student-manual-grade"><strong>Оценка преподавателя · ${formatPoints(data.manual_grade.points)} балла</strong><span>${escapeHtml(data.manual_grade.comment)}</span><small>${escapeHtml(data.manual_grade.actor)} · ${formatDate(data.manual_grade.created_at)}</small></div>` : ''}</section>
          </aside>
          <section class="workbench-column"><div id="task-editor"></div></section>
          </div>
        </div>
        <section class="attempts-section"><div class="section-heading"><h2>История запросов</h2><span class="section-count" id="task-history-count">0 записей</span></div><div class="attempt-list" id="task-history-list"><div class="empty-attempts">Загружаем историю…</div></div><button type="button" class="secondary history-more hidden" id="task-history-more">Загрузить предыдущие</button></section>
      </div>`;

    const editor = window.CabinetSQLEditor.mount(document.querySelector('#task-editor'), {
      id: 'task-sql',
      value: data.draft,
      checkOpen: homework.grading_open,
      onChange: saveDraftDebounced.bind(null, homeworkId, taskId),
      onFormat: formatSql,
      onRun: (sql) => runTaskQuery(homeworkId, taskId, sql),
      onCheck: (sql) => checkTaskQuery(homeworkId, taskId, sql),
    });
    document.querySelector('.task-page').addEventListener('click', (event) => {
      if (event.target.closest('.restore-task-query')) {
        const sql = event.target.closest('.attempt-detail').querySelector('pre code').textContent;
        editor.setValue(sql);
        document.querySelector('#task-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    document.querySelector('#task-history-more').addEventListener('click', () => loadTaskHistory(homeworkId, taskId, true));
    await loadTaskHistory(homeworkId, taskId, false);
    if (data.draft_saved_at) editor.setDraftStatus(`Черновик сохранён · ${formatDate(data.draft_saved_at)}`, 'saved');
  } catch (error) {
    app.innerHTML = `<div class="page-shell"><div class="error-panel"><strong>Не удалось открыть задачу</strong><p>${escapeHtml(error.message)}</p><button class="secondary" id="error-back">К задачам ДЗ</button></div></div>`;
    document.querySelector('#error-back').addEventListener('click', () => { location.hash = `#homework/${encodeURIComponent(homeworkId)}`; });
  }
}

function draftPath(homeworkId, taskId) {
  return `/api/homeworks/${encodeURIComponent(homeworkId)}/tasks/${encodeURIComponent(taskId)}/draft`;
}

async function persistDraft(draft) {
  const key = `${draft.homeworkId}/${draft.taskId}`;
  const previous = draftWrites.get(key) || Promise.resolve();
  const write = previous.catch(() => {}).then(() => api(draftPath(draft.homeworkId, draft.taskId), {
    method: 'PUT', body: JSON.stringify({ sql: draft.sql }), keepalive: true,
  }));
  draftWrites.set(key, write);
  let result;
  try {
    result = await write;
  } finally {
    if (draftWrites.get(key) === write) draftWrites.delete(key);
  }
  if (location.hash === `#task/${draft.homeworkId}/${draft.taskId}`) {
    const status = document.querySelector('#task-editor [data-draft]');
    if (status) {
      status.textContent = `Черновик сохранён · ${formatDate(result.saved_at)}`;
      status.dataset.state = 'saved';
    }
  }
  return result;
}

function sendPendingDraft(key, draft) {
  if (pendingDrafts.get(key) !== draft) return Promise.resolve();
  const active = draftRequests.get(key);
  if (active?.draft === draft) return active.promise;
  const promise = persistDraft(draft).then(() => {
    if (pendingDrafts.get(key) === draft) pendingDrafts.delete(key);
  }).catch((error) => {
    if (pendingDrafts.get(key) === draft && location.hash === `#task/${draft.homeworkId}/${draft.taskId}`) {
      const status = document.querySelector('#task-editor [data-draft]');
      if (status) { status.textContent = error.message; status.dataset.state = 'error'; }
    }
  }).finally(() => {
    if (draftRequests.get(key)?.draft === draft) draftRequests.delete(key);
  });
  draftRequests.set(key, { draft, promise });
  return promise;
}

function saveDraftDebounced(homeworkId, taskId, sql) {
  const key = `${homeworkId}/${taskId}`;
  const draft = { homeworkId, taskId, sql };
  pendingDrafts.set(key, draft);
  clearTimeout(draftTimers.get(key));
  draftTimers.set(key, setTimeout(async () => {
    draftTimers.delete(key);
    await sendPendingDraft(key, draft);
  }, 500));
}

function clearPendingDraft(homeworkId, taskId) {
  const key = `${homeworkId}/${taskId}`;
  clearTimeout(draftTimers.get(key));
  draftTimers.delete(key);
  pendingDrafts.delete(key);
}

window.addEventListener('pagehide', () => {
  for (const [key, draft] of pendingDrafts) {
    clearTimeout(draftTimers.get(key));
    void sendPendingDraft(key, draft);
  }
  draftTimers.clear();
});

document.addEventListener('visibilitychange', () => {
  for (const [key, draft] of pendingDrafts) {
    if (document.visibilityState === 'hidden') {
      clearTimeout(draftTimers.get(key));
      draftTimers.delete(key);
      void sendPendingDraft(key, draft);
    } else if (!draftTimers.has(key) && !draftRequests.has(key)) {
      void sendPendingDraft(key, draft);
    }
  }
});

async function formatSql(sql) {
  return (await api('/api/format', { method: 'POST', body: JSON.stringify({ sql }) })).sql;
}

async function runTaskQuery(homeworkId, taskId, sql) {
  clearPendingDraft(homeworkId, taskId);
  await persistDraft({ homeworkId, taskId, sql });
  const result = await api(`/api/homeworks/${encodeURIComponent(homeworkId)}/tasks/${encodeURIComponent(taskId)}/run`, {
    method: 'POST', body: JSON.stringify({ sql }),
  });
  await loadTaskHistory(homeworkId, taskId, false);
  return result;
}

async function loadTaskHistory(homeworkId, taskId, append = true) {
  const list = document.querySelector('#task-history-list');
  if (!list || location.hash !== `#task/${homeworkId}/${taskId}`) return;
  if (!append) taskHistoryOffset = 0;
  try {
    const data = await api(`/api/homeworks/${encodeURIComponent(homeworkId)}/tasks/${encodeURIComponent(taskId)}/history?offset=${taskHistoryOffset}&limit=50`);
    const markup = data.items.map((item, index) => item.kind === 'check'
      ? attemptMarkup({ ...item, points: item.points || '0', verdict: item.verdict || 'ERROR', message: item.attempt_message || item.message, query_status: item.status }, data.total - taskHistoryOffset - index, false, true)
      : runMarkup(item, data.total - taskHistoryOffset - index, true)).join('');
    if (append && taskHistoryOffset) list.insertAdjacentHTML('beforeend', markup);
    else list.innerHTML = markup || '<div class="empty-attempts">Пока нет запросов.</div>';
    taskHistoryOffset += data.items.length;
    document.querySelector('#task-history-count').textContent = russianCount(data.total, ['запись', 'записи', 'записей']);
    document.querySelector('#task-history-more').classList.toggle('hidden', !data.has_more);
  } catch (error) { list.textContent = error.message; }
}

async function checkTaskQuery(homeworkId, taskId, sql) {
  clearPendingDraft(homeworkId, taskId);
  await persistDraft({ homeworkId, taskId, sql });
  const result = await api(`/api/homeworks/${encodeURIComponent(homeworkId)}/tasks/${encodeURIComponent(taskId)}/check`, {
    method: 'POST', body: JSON.stringify({ sql }),
  });
  if (location.hash === `#task/${homeworkId}/${taskId}`) {
    const score = document.querySelector('#best-score');
    const previousPoints = score ? Number(score.textContent.split('/')[0].trim()) : 0;
    const standing = Number(result.score ?? result.points);
    const maximum = Number(result.max_points);
    if (score) {
      if (result.improved) animatePoints(score, previousPoints, standing, maximum);
      else score.textContent = `${formatPoints(standing)} / ${formatPoints(maximum)}`;
    }
    const currentTask = document.querySelector('.task-nav-row[aria-current="page"]');
    const resultCard = document.querySelector('.task-result-card');
    const newlyCompleted = currentTask && !currentTask.classList.contains('done') && standing >= maximum;
    paintScoreState(currentTask, resultCard, standing, maximum);
    if (newlyCompleted) {
      currentTask.classList.add('just-completed');
      resultCard?.classList.add('just-completed');
      celebrateCompletion(resultCard);
      window.setTimeout(() => {
        currentTask.classList.remove('just-completed');
        resultCard?.classList.remove('just-completed');
      }, 700);
    }
    await loadTaskHistory(homeworkId, taskId, false);
  }
  return result;
}

async function route() {
  const match = location.hash.match(/^#task\/([^/]+)\/([^/]+)$/);
  const homeworkMatch = location.hash.match(/^#homework\/([^/]+)$/);
  const adminRoute = location.hash === '#admin' || location.hash.startsWith('#admin/');
  const authenticatedRoute = Boolean(token) && !adminRoute;
  let student = null;
  if (authenticatedRoute) {
    try { student = await renderUserbar(); } catch (error) { showAuth(error.message); return; }
  }
  if (match && token) {
    await renderTask(decodeURIComponent(match[1]), decodeURIComponent(match[2]));
  } else if (homeworkMatch && token) {
    try {
      const homeworks = await api('/api/homeworks');
      const homework = homeworks.find((item) => item.id === decodeURIComponent(homeworkMatch[1]));
      if (!homework) throw new Error('Домашнее задание не найдено');
      renderHomework(homework);
    } catch (error) {
      app.innerHTML = `<div class="page-shell"><div class="error-panel"><strong>Не удалось открыть ДЗ</strong><p>${escapeHtml(error.message)}</p><button class="secondary" id="error-back">Все задания</button></div></div>`;
      document.querySelector('#error-back').addEventListener('click', returnToHome);
    }
  } else if (location.hash === '#sandbox' && token) {
    showSandbox();
  } else if (location.hash === '#profile' && token) {
    await renderProfile(student);
  } else if (adminRoute) {
    await showAdmin();
  } else if (token) {
    await renderHome();
  } else {
    showAuth();
  }
}

window.addEventListener('hashchange', route);

function showSandbox() {
  app.innerHTML = `<div class="page-shell sandbox-page">${breadcrumbMarkup([{ label: 'Главная', href: '#home' }, { label: 'Песочница' }])}<div class="task-heading"><div><p class="eyebrow">БЕЗ ОЦЕНИВАНИЯ</p><h1>Песочница SQL</h1></div></div><div id="sandbox-editor"></div><section class="sandbox-history-section"><div class="section-heading"><h2>История запросов</h2><span class="section-count">Последние 10</span></div><div id="sandbox-history"></div></section></div>`;
  const editor = window.CabinetSQLEditor.mount(document.querySelector('#sandbox-editor'), {
    id: 'sandbox-sql', sandbox: true, onFormat: formatSql,
    onRun: async (sql) => {
      const result = await api('/api/run', { method: 'POST', body: JSON.stringify({ sql }) });
      await loadSandboxHistory(editor);
      return result;
    },
  });
  loadSandboxHistory(editor);
}

async function loadSandboxHistory(editor) {
  const target = document.querySelector('#sandbox-history');
  try {
    const rows = await api('/api/sandbox/history');
    target.innerHTML = rows.length ? `<div class="sandbox-history-list">${rows.map((row, index) => `<details class="attempt-card"><summary><span class="attempt-index">Запуск ${rows.length - index}</span><span class="attempt-verdict ${row.status === 'ok' ? 'verdict-success' : 'verdict-error'}">${row.status === 'ok' ? 'Выполнен' : 'Ошибка'}</span><span class="attempt-points">${row.row_count ?? 0} строк</span><span class="attempt-time">${formatDate(row.created_at)}</span>${expandIcon}</summary>${historyDetail({
      meta: '',
      reason: row.message ? `<p class="attempt-reason">${escapeHtml(row.message)}</p>` : '',
      sql: row.sql,
      table: resultTableMarkup(row.result),
      restoreClass: 'restore-query',
      restoreLabel: 'Вернуть запрос в редактор',
      restoreAttrs: `data-index="${index}"`,
    })}</details>`).join('')}</div>` : '<div class="empty-attempts">Здесь появятся последние 10 запросов из песочницы.</div>';
    target.querySelectorAll('.restore-query').forEach((button) => {
      button.addEventListener('click', () => editor.setValue(rows[Number(button.dataset.index)].sql));
    });
  } catch (error) {
    target.textContent = error.message;
  }
}

async function showAdmin() {
  if (!token) {
    showAuth();
    return;
  }
  let student;
  try {
    student = await renderUserbar();
  } catch (error) {
    showAuth(error.message);
    return;
  }
  if (!(student.is_assistant || student.is_superadmin)) {
    renderStaffGate();
    return;
  }
  await renderAdminApp();
}

async function openStaffLogin(mode) {
  const session = token;
  token = null;
  currentUser = null;
  localStorage.removeItem('cabinet_token');
  authMode = mode;
  if (session) {
    try {
      await fetch('/api/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${session}` } });
    } catch { /* expired session */ }
  }
  if (location.hash && location.hash !== '#home') {
    window.history.replaceState(null, '', `${location.pathname}${location.search}#home`);
  }
  showAuth();
}

function renderStaffGate() {
  app.innerHTML = `<div class="page-shell"><section class="auth-card staff-gate"><div class="auth-mark">SQL Trainer</div><h1>Панель открывается отдельным входом</h1><p class="muted">Сейчас вы вошли как студент. Ассистент входит логином и паролем. Администратор вводит те же поля и ключ.</p>${staffEntryMarkup()}<button type="button" class="text-button" id="back-to-study">Вернуться к заданиям</button></section></div>`;
  document.querySelector('#back-to-study').addEventListener('click', () => { location.hash = '#home'; });
  document.querySelector('[data-mode="assistant"]').addEventListener('click', () => openStaffLogin('assistant'));
  document.querySelector('[data-mode="administrator"]').addEventListener('click', () => openStaffLogin('administrator'));
}

async function adminApi(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  let body;
  try { body = await response.json(); } catch { body = { detail: response.statusText }; }
  if (!response.ok) {
    const error = new Error(errorText(body));
    error.status = response.status;
    throw error;
  }
  return body;
}

let queueStudents = [];

function adminIsSuper() {
  return Boolean(currentUser?.is_superadmin);
}

function parseAdminRoute() {
  const parts = location.hash.replace(/^#admin\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 'student' && parts.length >= 4) return { screen: 'task', studentId: parts[1], homework: parts[2], task: parts[3] };
  if (parts[0] === 'student' && parts[1]) return { screen: 'student', studentId: parts[1] };
  if (['export', 'course', 'audit', 'accounts'].includes(parts[0])) return { screen: parts[0] };
  return { screen: 'queue' };
}

function adminNav(active) {
  const items = [
    ['queue', 'Очередь', '#admin'],
    ['export', 'Выгрузки', '#admin/export'],
  ];
  if (adminIsSuper()) {
    items.push(
      ['course', 'Курс', '#admin/course'],
      ['audit', 'Журнал', '#admin/audit'],
      ['accounts', 'Права', '#admin/accounts'],
    );
  }
  return `<nav class="admin-nav" aria-label="Разделы панели">${items.map(([id, label, href]) => `<a href="${href}" class="${active === id ? 'active' : ''}" ${active === id ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</nav>`;
}

async function renderAdminApp() {
  const routeInfo = parseAdminRoute();
  const restricted = ['course', 'audit', 'accounts'].includes(routeInfo.screen) && !adminIsSuper();
  const screen = restricted ? 'denied' : routeInfo.screen;
  const navScreen = screen === 'student' || screen === 'task' ? 'queue' : screen;
  app.innerHTML = `<div class="page-shell admin-page">${adminNav(navScreen)}<div id="admin-screen"></div></div>`;
  const target = document.querySelector('#admin-screen');
  if (screen === 'queue') await renderAdminQueue(target);
  else if (screen === 'student') await renderAdminStudent(target, routeInfo.studentId);
  else if (screen === 'task') await renderAdminTask(target, routeInfo);
  else if (screen === 'export') renderAdminExport(target);
  else if (screen === 'course') await renderAdminCourse(target);
  else if (screen === 'audit') renderAdminAudit(target);
  else if (screen === 'accounts') {
    renderAdminAccounts(target);
  } else target.innerHTML = '<section class="admin-section card-surface"><h2>Этот раздел доступен администратору.</h2><p class="muted">Ассистент работает с очередью и выгрузками.</p><a class="secondary" href="#admin">К очереди</a></section>';
}

async function renderAdminQueue(target) {
  target.innerHTML = `<section class="admin-section card-surface"><div class="section-heading"><h2>Очередь</h2><span class="section-count" id="queue-count">…</span></div><p class="muted">Студенты, у которых уже есть проверка, а балл ещё не полный. Поиск находит любого студента, в том числе без попыток.</p><div class="admin-toolbar"><input id="queue-search" placeholder="Имя или логин"></div><div id="queue-list">Загружаем…</div></section>`;
  document.querySelector('#queue-search').addEventListener('input', paintQueue);
  try {
    queueStudents = await adminApi('/api/admin/students?sort=name');
    paintQueue();
  } catch (error) {
    document.querySelector('#queue-list').textContent = error.message;
  }
}

function paintQueue() {
  const query = document.querySelector('#queue-search')?.value.trim().toLowerCase() || '';
  const labels = { not_started: 'Нет попыток', complete: 'Полный балл', partial: 'Частично', zero: '0 баллов' };
  const rows = queueStudents.filter((student) => {
    const inQueue = student.status === 'partial' || student.status === 'zero';
    const matches = !query || `${student.full_name} ${student.username}`.toLowerCase().includes(query);
    return matches && (query || inQueue);
  });
  const count = document.querySelector('#queue-count');
  if (count) count.textContent = query ? `${rows.length}` : `${rows.length} в очереди`;
  const list = document.querySelector('#queue-list');
  if (!list) return;
  list.innerHTML = rows.length ? `<div class="queue-list">${rows.map((student) => {
    const tone = student.status === 'complete' ? 'is-complete' : student.status === 'partial' ? 'is-partial' : 'is-zero';
    return `<a class="queue-row" href="#admin/student/${student.id}"><span class="queue-person"><strong>${escapeHtml(student.full_name)}</strong><small>${escapeHtml(student.username)}</small></span><span class="score-figure ${tone}">${formatPoints(student.score)}</span><span class="status-pill ${tone}">${labels[student.status] || escapeHtml(student.status)}</span></a>`;
  }).join('')}</div>` : `<p class="muted">${query ? 'Такого студента нет.' : 'Очередь пуста: у всех с попытками уже полный балл.'}</p>`;
}

async function renderAdminStudent(target, studentId) {
  target.innerHTML = '<section class="admin-section card-surface"><p class="muted">Загружаем карточку…</p></section>';
  try {
    const detail = await adminApi(`/api/admin/students/${studentId}`);
    const student = detail.student;
    target.innerHTML = `<section class="admin-section card-surface"><p class="admin-crumb"><a href="#admin">Очередь</a></p><div class="student-detail-heading"><div><h2>${escapeHtml(student.full_name)}</h2><span>${escapeHtml(student.username)} · аккаунт с ${formatDate(student.created_at)}</span></div><button class="secondary" id="reset-student-password" type="button">Сбросить пароль</button></div><div class="temporary-password" id="temporary-password"></div></section>${detail.homeworks.map((homework) => `<section class="admin-section card-surface"><h3>${escapeHtml(homework.id.toUpperCase())} · ${escapeHtml(homework.title)}</h3><div class="student-task-list">${Object.values(homework.tasks).map((task) => {
      const tone = scoreTone(task.score, task.max_points);
      return `<a class="student-task-row" href="#admin/student/${studentId}/${encodeURIComponent(homework.id)}/${encodeURIComponent(task.id)}"><span class="task-nav-index">${escapeHtml(task.id)}</span><span>${escapeHtml(task.title || task.id)}</span><strong class="${tone}">${formatPoints(task.score || 0)} / ${formatPoints(task.max_points || 0)}</strong></a>`;
    }).join('')}</div><form class="deadline-form"><input type="hidden" name="student_id" value="${escapeHtml(studentId)}"><input type="hidden" name="homework" value="${escapeHtml(homework.id)}"><label class="field">Открыть проверку после жёсткого срока<input name="comment" required minlength="3" maxlength="2000" placeholder="Основание"></label><button class="secondary" type="submit">Открыть проверку</button><p class="notice" data-override-result></p></form></section>`).join('')}<section class="admin-section card-surface"><h3>Песочница</h3><div class="attempt-list">${detail.sandbox_runs.length ? detail.sandbox_runs.map((run, index) => runMarkup(run, detail.sandbox_runs.length - index)).join('') : '<p class="muted">Запусков пока нет.</p>'}</div></section>`;
    document.querySelector('#reset-student-password').addEventListener('click', () => resetStudentPassword(studentId, student.username));
    target.querySelectorAll('.deadline-form').forEach((form) => form.addEventListener('submit', overrideDeadline));
  } catch (error) {
    target.innerHTML = `<section class="admin-section card-surface"><p>${escapeHtml(error.message)}</p></section>`;
  }
}

async function renderAdminTask(target, routeInfo) {
  target.innerHTML = '<section class="admin-section card-surface"><p class="muted">Загружаем задачу…</p></section>';
  try {
    const detail = await adminApi(`/api/admin/students/${routeInfo.studentId}`);
    const student = detail.student;
    const homework = detail.homeworks.find((item) => item.id === routeInfo.homework);
    const task = homework ? Object.values(homework.tasks).find((item) => item.id === routeInfo.task) : null;
    if (!task) throw new Error('Задача не найдена');
    const tone = scoreTone(task.score, task.max_points);
    target.innerHTML = `<section class="admin-section card-surface"><p class="admin-crumb"><a href="#admin">Очередь</a><span>/</span><a href="#admin/student/${routeInfo.studentId}">${escapeHtml(student.full_name)}</a></p><div class="section-heading"><h2>${escapeHtml(task.id)} · ${escapeHtml(task.title || task.id)}</h2><strong class="score-figure ${tone}">${formatPoints(task.score || 0)} / ${formatPoints(task.max_points || 0)}</strong></div><p class="muted">${escapeHtml(homework.id.toUpperCase())} · ${escapeHtml(homework.title)}</p><form class="manual-grade-form" id="task-grade-form"><label>Баллы<input name="points" type="number" min="0" max="${escapeHtml(task.max_points || '100')}" step="0.01" value="${escapeHtml(task.score || '0')}" required></label><label>Комментарий<input name="comment" minlength="3" maxlength="2000" placeholder="Почему оценка поставлена вручную" required></label><button class="secondary" type="submit">Сохранить оценку</button></form>${task.manual_grades.map((grade) => `<p class="manual-grade-entry">Ручная оценка ${escapeHtml(grade.points)} · ${formatDate(grade.created_at)} · ${escapeHtml(grade.actor)}<br>${escapeHtml(grade.comment)}</p>`).join('')}</section><section class="admin-section card-surface"><h3>Проверки</h3><div class="attempt-list">${task.attempts.length ? task.attempts.map((attempt, index) => attemptMarkup(attempt, task.attempts.length - index)).join('') : '<p class="muted">Проверок пока нет.</p>'}</div></section><section class="admin-section card-surface"><h3>Запуски</h3><div class="attempt-list">${task.runs.length ? task.runs.map((run, index) => runMarkup(run, task.runs.length - index)).join('') : '<p class="muted">Запусков пока нет.</p>'}</div></section>`;
    document.querySelector('#task-grade-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.currentTarget));
      try {
        await adminApi(`/api/admin/students/${routeInfo.studentId}/manual-grade/${encodeURIComponent(routeInfo.homework)}`, { method: 'POST', body: JSON.stringify({ task: routeInfo.task, points: values.points, comment: values.comment }) });
        await renderAdminTask(target, routeInfo);
      } catch (error) {
        const note = document.createElement('p');
        note.className = 'notice error-notice';
        note.textContent = error.message;
        event.currentTarget.after(note);
      }
    });
  } catch (error) {
    target.innerHTML = `<section class="admin-section card-surface"><p>${escapeHtml(error.message)}</p></section>`;
  }
}

async function resetStudentPassword(studentId, username) {
  if (!confirm(`Создать новый пароль для ${username}? Текущие сессии завершатся.`)) return;
  const result = await adminApi(`/api/admin/students/${studentId}/reset-password`, { method: 'POST' });
  const box = document.querySelector('#temporary-password');
  box.innerHTML = `<label class="field">Новый временный пароль<input readonly value="${escapeHtml(result.temporary_password)}"></label><button type="button" class="secondary" id="copy-temp-password">Копировать пароль</button><span role="status"></span>`;
  box.querySelector('#copy-temp-password').addEventListener('click', async () => {
    const input = box.querySelector('input');
    try { await navigator.clipboard.writeText(input.value); }
    catch { input.select(); document.execCommand('copy'); }
    box.querySelector('[role="status"]').textContent = 'Скопировано';
  });
}

function homeworkSelectMarkup(manifest, id, includeAll = false) {
  const homeworks = Array.isArray(manifest?.homeworks) ? manifest.homeworks : [];
  const options = [
    includeAll ? '<option value="">Все домашние задания</option>' : '',
    ...homeworks.map((homework) => `<option value="${escapeHtml(homework.id)}">${escapeHtml(String(homework.id || '').toUpperCase())}${homework.title ? ` · ${escapeHtml(homework.title)}` : ''}</option>`),
  ].filter(Boolean).join('');
  return `<select id="${id}" ${options ? '' : 'disabled'}>${options || '<option value="">В курсе нет домашних заданий</option>'}</select>`;
}

async function renderAdminExport(target) {
  target.innerHTML = '<section class="admin-section card-surface"><p class="muted">Загружаем список домашних заданий…</p></section>';
  try {
    const manifestData = await adminApi('/api/admin/manifest');
    target.innerHTML = `<section class="admin-section card-surface"><div class="section-heading"><h2>Выгрузки</h2></div><p class="muted">Полная ведомость: логин, имя, балл по каждой задаче и сумма для одного домашнего задания.</p><form class="admin-inline" id="export-form"><label class="field">Домашнее задание${homeworkSelectMarkup(manifestData.manifest, 'export-hw')}</label><button class="secondary" type="submit">Скачать ведомость</button></form><div class="admin-actions export-all"><button id="export-course" class="secondary" type="button">Выгрузить всё</button><span class="muted">Два файла: ведомость всего курса и журнал всех проверок.</span></div><p id="admin-message" class="notice" role="status"></p></section>`;
    document.querySelector('#export-form').addEventListener('submit', (event) => {
      event.preventDefault();
      downloadCsv('results');
    });
    document.querySelector('#export-course').addEventListener('click', downloadCourseBundle);
  } catch (error) {
    target.innerHTML = `<section class="admin-section card-surface"><p>${escapeHtml(error.message)}</p></section>`;
  }
}

async function renderAdminCourse(target) {
  target.innerHTML = '<section class="admin-section card-surface"><p class="muted">Загружаем курс…</p></section>';
  let manifestData;
  try {
    manifestData = await adminApi('/api/admin/manifest');
  } catch (error) {
    target.innerHTML = `<section class="admin-section card-surface"><p>${escapeHtml(error.message)}</p></section>`;
    return;
  }
  const empty = !manifestData.manifest;
  const homeworks = manifestData.manifest?.homeworks || [];
  const status = empty
    ? 'Курса ещё нет'
    : manifestData.source === 'published'
      ? `Версия ${manifestData.version}`
      : 'Файл на диске';
  const lead = empty
    ? 'Файла курса на диске нет, и в базе ещё нет опубликованной версии. Вставьте JSON в поле ниже или выберите файл. Затем нажмите «Проверить манифест» и «Опубликовать версию». После публикации задания хранятся в базе кабинета.'
    : 'Задания, сроки и правильные ответы. Правка остаётся в JSON. Публикация проверяет правильные ответы и фиксирует новую версию.';
  const loaded = empty
    ? 'Курса пока нет. Вставьте JSON или выберите файл, затем проверьте манифест.'
    : manifestData.source === 'published'
      ? 'Загружена опубликованная версия.'
      : 'Загружен файл курса с диска. В базе эта версия ещё не опубликована.';
  const recheck = homeworks.length
    ? `<section class="admin-section card-surface"><h3>Перепроверить решения</h3><p class="muted">После изменения правильного ответа заново считает баллы по сохранённым запросам.</p><form class="admin-inline" id="recheck-form"><label class="field">Домашнее задание${homeworkSelectMarkup(manifestData.manifest, 'recheck-hw', true)}</label><button class="secondary" type="submit">Начать</button></form><p id="recheck-progress" class="notice"></p></section>`
    : '<section class="admin-section card-surface"><h3>Перепроверить решения</h3><p class="muted">Перепроверка появится после публикации курса.</p></section>';
  target.innerHTML = `<section class="admin-section card-surface"><div class="section-heading"><h2>Курс</h2><span class="section-count">${status}</span></div><div class="admin-stack"><p class="muted">${lead}</p><label class="field file-picker">Файл курса, JSON или старый ZIP<span class="file-picker-row"><input id="manifest-file" type="file" accept=".json,.zip"><span class="secondary">Выбрать файл</span><span class="file-picker-name">файл не выбран</span></span></label><details class="admin-fold"><summary>Сроки, если загружаете старый ZIP</summary><div class="admin-deadline-inputs"><label class="field">Мягкий срок<input id="zip-soft" type="datetime-local" value="2026-09-25T23:59"></label><label class="field">Жёсткий срок<input id="zip-hard" type="datetime-local" value="2026-10-10T23:59"></label></div><p class="muted">Для JSON эти сроки не нужны: они уже внутри файла.</p></details><textarea id="manifest-json" class="admin-json-editor" spellcheck="false" aria-label="JSON манифест" placeholder="Вставьте сюда JSON курса"></textarea><div class="admin-actions"><button id="manifest-preview" class="secondary" type="button">Проверить манифест</button><button id="manifest-publish" class="primary" type="button" disabled>Опубликовать версию</button></div><p id="manifest-result" class="admin-status">${loaded}</p></div></section>${recheck}`;
  if (!empty) document.querySelector('#manifest-json').value = JSON.stringify(manifestData.manifest, null, 2);
  document.querySelector('#manifest-preview').addEventListener('click', previewManifest);
  document.querySelector('#manifest-publish').addEventListener('click', publishManifest);
  document.querySelector('#manifest-file').addEventListener('change', readManifestFile);
  document.querySelector('#recheck-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    startRecheck();
  });
}

function renderAdminAudit(target) {
  target.innerHTML = `<section class="admin-section card-surface"><div class="section-heading"><h2>Журнал действий</h2></div><div class="admin-toolbar"><input id="audit-actor" placeholder="Исполнитель"><select id="audit-action"><option value="">Все действия</option>${Object.entries(auditActionTitles).map(([key, title]) => `<option value="${escapeHtml(key)}">${escapeHtml(title)}</option>`).join('')}</select><button id="audit-refresh" class="secondary" type="button">Обновить</button></div><div id="audit-list"></div></section>`;
  document.querySelector('#audit-refresh').addEventListener('click', () => loadAudit());
  loadAudit();
}

function renderAdminAccounts(target) {
  target.innerHTML = `<section class="admin-section card-surface"><div class="section-heading"><h2>Права</h2></div><p class="muted">Одна учётка на человека. Ассистент проверяет очередь, ставит оценку и скачивает ведомость. Администратор ещё публикует курс, видит журнал и выдаёт права.</p><div class="admin-toolbar"><input id="role-search" placeholder="Имя или логин"></div><div id="admin-account-list">Загружаем…</div></section>`;
  document.querySelector('#role-search').addEventListener('input', paintRoles);
  loadRoles();
}

async function loadAdmin() {
  await renderAdminApp();
}


async function readManifestFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  const chosen = document.querySelector('.file-picker-name');
  if (chosen) chosen.textContent = file.name;
  if (file.name.toLowerCase().endsWith('.zip')) {
    try {
      const form = new FormData();
      form.append('file', file);
      const query = new URLSearchParams({
        soft_deadline: new Date(document.querySelector('#zip-soft').value).toISOString(),
        hard_deadline: new Date(document.querySelector('#zip-hard').value).toISOString(),
      });
      const imported = await adminApi(`/api/admin/import/legacy-zip?${query}`, { method: 'POST', body: form });
      document.querySelector('#manifest-json').value = JSON.stringify(imported.manifest, null, 2);
      showManifestResult(JSON.stringify(imported.preview, null, 2), true);
    } catch (error) { showManifestResult(error.message, false); }
  } else {
    document.querySelector('#manifest-json').value = await file.text();
    showManifestResult('Файл подставлен в редактор. Сначала проверьте манифест.', false);
  }
  document.querySelector('#manifest-publish').disabled = true;
  window.manifestReady = false;
}

async function previewManifest() {
  try {
    const manifest = JSON.parse(document.querySelector('#manifest-json').value);
    const preview = await adminApi('/api/admin/import/preview', { method: 'POST', body: JSON.stringify({ manifest }) });
    showManifestResult(JSON.stringify(preview, null, 2), true);
    document.querySelector('#manifest-publish').disabled = false;
    window.manifestReady = true;
  } catch (error) {
    showManifestResult(error.message, false);
    document.querySelector('#manifest-publish').disabled = true;
    window.manifestReady = false;
  }
}

async function publishManifest() {
  if (!window.manifestReady) return;
  try {
    const manifest = JSON.parse(document.querySelector('#manifest-json').value);
    const result = await adminApi('/api/admin/publish', { method: 'POST', body: JSON.stringify({ manifest }) });
    showManifestResult(`Опубликована версия ${result.version}. Домашних заданий: ${result.homework_count}.`, false);
    document.querySelector('#manifest-publish').disabled = true;
    window.manifestReady = false;
  } catch (error) { showManifestResult(error.message, false); }
}

function showManifestResult(text, asCode) {
  const result = document.querySelector('#manifest-result');
  if (!result) return;
  result.className = asCode ? 'code admin-result' : 'admin-status';
  result.textContent = text;
}

async function loadStudents() {
  const query = new URLSearchParams();
  for (const [elementId, key] of [['filter-search', 'search'], ['filter-hw', 'homework'], ['filter-task', 'task'], ['filter-min', 'min_score'], ['filter-max', 'max_score'], ['filter-status', 'status'], ['filter-sort', 'sort']]) {
    const value = document.querySelector(`#${elementId}`)?.value;
    if (value) query.set(key, value);
  }
  try {
    const students = await adminApi(`/api/admin/students?${query}`);
    const statusLabels = { not_started: 'Нет попыток', complete: 'Выполнено', partial: 'Частично', zero: '0 баллов' };
    document.querySelector('#student-list').innerHTML = `<div class="admin-table-scroll"><table class="admin-table"><thead><tr><th>ID</th><th>Студент</th><th>Баллы</th><th>Статус</th><th>Карточка</th></tr></thead><tbody>${students.map((student) => {
      const tone = student.status === 'complete' ? 'is-complete' : student.status === 'partial' ? 'is-partial' : 'is-zero';
      return `<tr><td>${student.id}</td><td><strong>${escapeHtml(student.full_name)}</strong><small>${escapeHtml(student.username)}</small></td><td class="score-figure ${tone}">${formatPoints(student.score)}</td><td><span class="status-pill ${tone}">${statusLabels[student.status] || escapeHtml(student.status)}</span></td><td><button class="secondary compact student-detail-toggle" data-id="${student.id}">Открыть</button></td></tr><tr id="student-detail-${student.id}" class="hidden"><td colspan="5"></td></tr>`;
    }).join('')}</tbody></table></div>`;
    document.querySelectorAll('.student-detail-toggle').forEach((button) => button.addEventListener('click', () => studentDetail(button.dataset.id)));
  } catch (error) { document.querySelector('#student-list').textContent = error.message; }
}

const auditActionTitles = {
  register: 'Регистрация', login: 'Вход', login_failed: 'Ошибка входа', logout: 'Выход',
  admin_login: 'Вход преподавателя', admin_login_failed: 'Ошибка входа преподавателя',
  super_admin_login: 'Вход администратора', admin_logout: 'Выход из панели',
  assistant_login: 'Вход ассистента', assistant_login_denied: 'Отказ во входе ассистента',
  superadmin_claimed: 'Включены права администратора', superadmin_claim_failed: 'Ключ не подошёл',
  role_changed: 'Изменены права',
  profile_update: 'Изменение профиля', password_change: 'Смена пароля',
  password_change_failed: 'Неудачная смена пароля', draft_saved: 'Сохранение черновика',
  registration_failed: 'Неудачная регистрация', task_check_rejected: 'Проверка отклонена',
  manifest_publish_rejected: 'Публикация манифеста отклонена',
  deadline_override_rejected: 'Снятие срока отклонено', manual_grade_rejected: 'Ручная оценка отклонена',
  query_run: 'Запуск SQL', task_checked: 'Проверка задания', achievement_created: 'Улучшение результата',
  manifest_previewed: 'Предпросмотр манифеста', manifest_preview_rejected: 'Ошибка проверки манифеста',
  legacy_zip_previewed: 'Предпросмотр ZIP-архива', legacy_zip_preview_rejected: 'Ошибка импорта ZIP-архива',
  manifest_published: 'Публикация манифеста', csv_exported: 'Выгрузка CSV',
  student_password_reset: 'Сброс пароля студента',   admin_account_created: 'Создание учётной записи',
  admin_account_status_changed: 'Изменение статуса учётной записи',
  admin_password_reset: 'Новый пароль ассистента', recheck_started: 'Перепроверка запущена',
  homework_rechecked: 'Задача перепроверена', recheck_completed: 'Перепроверка завершена',
  recheck_failed: 'Ошибка перепроверки', deadline_override_granted: 'Снятие жёсткого дедлайна',
  manual_grade: 'Ручная оценка',
};
const auditFieldTitles = {
  username: 'Логин', full_name: 'ФИО', homework: 'Домашнее задание', task: 'Задача',
  points: 'Баллы', previous_points: 'До улучшения', max_points: 'Максимум', verdict: 'Вердикт',
  improved: 'Улучшен результат', query_status: 'Состояние запроса', elapsed_ms: 'Время выполнения, мс',
  rows: 'Строк', attempt_id: 'Попытка', query_run_id: 'Запуск', run_id: 'Запуск',
  job_id: 'Перепроверка', completed: 'Проверено задач', total: 'Всего задач',
  kind: 'Тип выгрузки', version: 'Версия', schema_version: 'Версия схемы',
  homework_count: 'Домашних заданий', filename: 'Файл', active: 'Статус учётной записи',
  comment: 'Комментарий', error: 'Ошибка', reason: 'Причина', sql: 'SQL-запрос',
  student_id: 'ID студента', items: 'Неподдерживаемые конструкции',
};
const auditValueTitles = {
  check: 'Проверка', task: 'Задача', sandbox: 'Песочница', ok: 'Выполнен', error: 'Ошибка',
  blocked: 'Заблокирован', rejected: 'Отклонён', OK: 'Верно', STRUCT: 'Частично',
  WRONG: 'Неверно', SYNTAX: 'Ошибка синтаксиса', ERROR: 'Ошибка выполнения',
  HARD_LATE: 'Жёсткий срок истёк', MANUAL: 'Ручная оценка', results: 'Ведомость',
  results_milestones: 'Достижения', results_details: 'Детализация',
  gradebook: 'Ведомость курса', journal: 'Журнал проверок',
  current_password_mismatch: 'Текущий пароль не подошёл', username_taken: 'Логин уже занят',
  points_out_of_range: 'Баллы вне допустимого диапазона', invalid_comment: 'Некорректная причина',
  missing_required_constructs: 'В правильном ответе не хватает обязательных конструкций', psql_command: 'Команда psql не оценивается',
};

function auditDetailsMarkup(detailsJson) {
  let details;
  try { details = JSON.parse(detailsJson || '{}'); }
  catch { return `<pre class="code">${escapeHtml(detailsJson)}</pre>`; }
  const entries = Object.entries(details || {});
  if (!entries.length) return '<p class="muted">Дополнительных сведений нет.</p>';
  return `<dl class="audit-detail-list">${entries.map(([key, rawValue]) => {
    let value = rawValue;
    if (key === 'improved') value = rawValue ? 'Да' : 'Нет';
    else if (key === 'active') value = rawValue ? 'Активна' : 'Отключена';
    else if (typeof rawValue === 'string') value = auditValueTitles[rawValue] || rawValue;
    else if (rawValue && typeof rawValue === 'object') value = JSON.stringify(rawValue, null, 2);
    const rendered = key === 'sql'
      ? `<pre class="code">${escapeHtml(value)}</pre>`
      : `<span>${escapeHtml(value)}</span>`;
    return `<div><dt>${escapeHtml(auditFieldTitles[key] || key.replaceAll('_', ' '))}</dt><dd>${rendered}</dd></div>`;
  }).join('')}</dl>`;
}

function auditEventMarkup(event, username = '') {
  const title = auditActionTitles[event.action] || event.action.replaceAll('_', ' ');
  return `<details class="attempt-card audit-event"><summary>${formatDate(event.created_at)} · ${escapeHtml(title)} · ${escapeHtml(event.actor)}${username ? ` · ${escapeHtml(username)}` : ''}</summary><div class="audit-event-details">${auditDetailsMarkup(event.details_json)}</div></details>`;
}

async function loadAudit(append = false) {
  const target = document.querySelector('#audit-list');
  if (!target) return;
  const query = new URLSearchParams();
  const actor = document.querySelector('#audit-actor').value.trim();
  const action = document.querySelector('#audit-action').value.trim();
  if (actor) query.set('actor_filter', actor);
  if (action) query.set('action', action);
  query.set('limit', '100');
  query.set('offset', String(append ? auditOffset : 0));
  try {
    const rows = await adminApi(`/api/admin/audit?${query}`);
    if (!append) {
      auditOffset = 0;
      target.innerHTML = `<div class="attempt-list" id="global-audit-events"></div><button id="global-audit-more" class="secondary compact hidden" type="button">Более ранние действия</button>`;
    }
    const list = document.querySelector('#global-audit-events');
    list.insertAdjacentHTML('beforeend', rows.map((event) => auditEventMarkup(event, event.student_username)).join(''));
    if (!auditOffset && !rows.length) list.innerHTML = '<p class="muted">Действий пока нет.</p>';
    auditOffset += rows.length;
    const more = document.querySelector('#global-audit-more');
    more.classList.toggle('hidden', rows.length < 100);
    more.onclick = () => loadAudit(true);
  } catch (error) { target.textContent = error.message; }
}

let rolePeople = [];

async function loadRoles() {
  const target = document.querySelector('#admin-account-list');
  if (!target) return;
  try {
    rolePeople = await adminApi('/api/admin/roles');
    paintRoles();
  } catch (error) { target.textContent = error.message; }
}

function paintRoles() {
  const target = document.querySelector('#admin-account-list');
  if (!target) return;
  const query = document.querySelector('#role-search')?.value.trim().toLowerCase() || '';
  const rows = rolePeople.filter((person) => !query || `${person.full_name} ${person.username}`.toLowerCase().includes(query));
  target.innerHTML = rows.length ? `<div class="admin-table-scroll"><table class="admin-table admin-table-fit"><thead><tr><th>Логин</th><th>ФИО</th><th>Ассистент</th><th>Администратор</th></tr></thead><tbody>${rows.map((person) => `<tr><td>${escapeHtml(person.username)}</td><td>${escapeHtml(person.full_name)}</td>${['is_assistant', 'is_superadmin'].map((flag) => `<td><button class="${person[flag] ? 'primary' : 'secondary'} compact role-toggle" type="button" data-id="${person.id}" data-flag="${flag}" data-value="${person[flag] ? 'false' : 'true'}">${person[flag] ? 'Включён' : 'Выключен'}</button></td>`).join('')}</tr>`).join('')}</tbody></table></div>` : '<p class="muted account-empty">Такой учётной записи нет.</p>';
  target.querySelectorAll('.role-toggle').forEach((button) => button.addEventListener('click', async () => {
    const enabling = button.dataset.value === 'true';
    const title = button.dataset.flag === 'is_superadmin' ? 'администратора' : 'ассистента';
    if (!confirm(`${enabling ? 'Включить' : 'Выключить'} права ${title}?`)) return;
    try {
      await adminApi(`/api/admin/students/${button.dataset.id}/role`, { method: 'PATCH', body: JSON.stringify({ [button.dataset.flag]: enabling }) });
      if (String(currentUser?.id) === String(button.dataset.id)) currentUser = { ...currentUser, [button.dataset.flag]: enabling };
      if (currentUser && !currentUser.is_assistant && !currentUser.is_superadmin) {
        await showAdmin();
        return;
      }
      if (!currentUser?.is_superadmin && location.hash.startsWith('#admin/accounts')) {
        location.hash = '#admin';
        return;
      }
      await loadRoles();
    } catch (error) { target.textContent = error.message; }
  }));
}

async function studentDetail(studentId) {
  const row = document.querySelector(`#student-detail-${studentId}`);
  const cell = row.querySelector('td');
  if (!row.classList.contains('hidden') && row.dataset.loaded === 'true') {
    row.classList.add('hidden');
    return;
  }
  row.classList.remove('hidden');
  cell.textContent = 'Загружаем историю…';
  try {
    const detail = await adminApi(`/api/admin/students/${studentId}`);
    const student = detail.student;
    cell.innerHTML = `<div class="student-detail"><div class="student-detail-heading"><div><strong>${escapeHtml(student.full_name)}</strong><span>${escapeHtml(student.username)} · аккаунт с ${formatDate(student.created_at)}</span></div><button class="secondary reset-student-password" data-id="${studentId}">Сбросить пароль</button></div><div class="temporary-password" id="temporary-password-${studentId}"></div>${detail.homeworks.map((homework) => `<details class="student-homework"><summary>${escapeHtml(homework.id.toUpperCase())} · ${escapeHtml(homework.title)}</summary><div>${Object.values(homework.tasks).map((task) => `<details class="student-task"><summary>${escapeHtml(task.id)} · ${escapeHtml(task.title)} <span class="${Number(task.score) > 0 ? '' : 'is-zero'}">${task.score ?? '0'}${task.max_points ? ` / ${task.max_points}` : ''}</span></summary><div class="student-task-detail"><form class="manual-grade-form" data-student="${studentId}" data-homework="${escapeHtml(homework.id)}" data-task="${escapeHtml(task.id)}"><label>Баллы<input name="points" type="number" min="0" max="${escapeHtml(task.max_points || '100')}" step="0.01" value="${escapeHtml(task.score || '0')}" required></label><label>Комментарий<input name="comment" minlength="3" maxlength="2000" placeholder="Причина ручной оценки" required></label><button class="secondary">Сохранить оценку</button></form>${task.manual_grades.map((grade) => `<p class="manual-grade-entry">Ручная оценка ${escapeHtml(grade.points)} · ${formatDate(grade.created_at)} · ${escapeHtml(grade.actor)}<br>${escapeHtml(grade.comment)}</p>`).join('')}<h4>Проверки</h4><div class="attempt-list">${task.attempts.length ? task.attempts.map((attempt, index) => attemptMarkup(attempt, task.attempts.length - index)).join('') : '<p class="muted">Проверок пока нет.</p>'}</div><h4>Запуски</h4><div class="attempt-list">${task.runs.length ? task.runs.map((run, index) => runMarkup(run, task.runs.length - index)).join('') : '<p class="muted">Запусков пока нет.</p>'}</div></div></details>`).join('')}</div></details>`).join('')}<details class="student-homework"><summary>Песочница</summary><div class="attempt-list">${detail.sandbox_runs.map((run, index) => runMarkup(run, detail.sandbox_runs.length - index)).join('') || '<p class="muted">Запусков пока нет.</p>'}</div></details><details class="student-homework"><summary>Действия по студенту (${detail.audit_count})</summary><div class="attempt-list student-audit-events">${detail.audit.map((event) => auditEventMarkup(event)).join('') || '<p class="muted">Событий пока нет.</p>'}</div>${detail.audit_has_more ? '<button type="button" class="secondary compact student-audit-more">Более ранние действия</button>' : ''}</details></div>`;
    let studentAuditOffset = detail.audit.length;
    cell.querySelector('.student-audit-more')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const older = await adminApi(`/api/admin/students/${studentId}/history?offset=${studentAuditOffset}&limit=100`);
        cell.querySelector('.student-audit-events').insertAdjacentHTML('beforeend', older.map((item) => auditEventMarkup(item)).join(''));
        studentAuditOffset += older.length;
        if (older.length < 100 || studentAuditOffset >= detail.audit_count) button.remove();
        else button.disabled = false;
      } catch (error) {
        button.disabled = false;
        button.textContent = error.message;
      }
    });
    row.dataset.loaded = 'true';
    cell.querySelector('.reset-student-password').addEventListener('click', async () => {
      if (!confirm(`Создать новый пароль для ${student.username}? Текущие сессии завершатся.`)) return;
      const result = await adminApi(`/api/admin/students/${studentId}/reset-password`, { method: 'POST' });
      const target = document.querySelector(`#temporary-password-${studentId}`);
      target.innerHTML = `<label>Новый временный пароль<input readonly value="${escapeHtml(result.temporary_password)}"></label><button type="button" class="secondary copy-temp-password">Копировать пароль</button><span role="status"></span>`;
      target.querySelector('.copy-temp-password').addEventListener('click', async () => {
        const input = target.querySelector('input');
        try { await navigator.clipboard.writeText(input.value); }
        catch { input.select(); document.execCommand('copy'); }
        target.querySelector('[role="status"]').textContent = 'Скопировано';
      });
    });
    cell.querySelectorAll('.manual-grade-form').forEach((form) => form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      try {
        await adminApi(`/api/admin/students/${studentId}/manual-grade/${encodeURIComponent(form.dataset.homework)}`, { method: 'POST', body: JSON.stringify({ task: form.dataset.task, points: values.points, comment: values.comment }) });
        await loadStudents();
        await studentDetail(studentId);
      } catch (error) { alert(error.message); }
    }));
  } catch (error) { cell.textContent = error.message; }
}

async function overrideDeadline(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const details = Object.fromEntries(new FormData(form));
  const status = form.querySelector('[data-override-result]');
  try {
    const result = await adminApi(`/api/admin/students/${details.student_id}/deadline-override/${encodeURIComponent(details.homework)}?comment=${encodeURIComponent(details.comment)}`, { method: 'POST' });
    if (status) status.textContent = `Проверка открыта · ${result.homework}`;
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

async function startRecheck() {
  const status = document.querySelector('#recheck-progress');
  try {
    const homework = document.querySelector('#recheck-hw').value;
    const query = homework ? `?homework=${encodeURIComponent(homework)}` : '';
    const job = await adminApi(`/api/admin/recheck${query}`, { method: 'POST' });
    status.textContent = `Перепроверка ${job.job_id}: 0 из ${job.total}`;
    let progress;
    do {
      await new Promise((resolve) => setTimeout(resolve, 500));
      progress = await adminApi(`/api/admin/recheck/${job.job_id}`);
      status.textContent = `Перепроверка ${job.job_id}: ${progress.completed} из ${progress.total} · ${progress.status}`;
    } while (progress.status === 'queued' || progress.status === 'running');
    if (progress.error) status.textContent += ` · ${progress.error}`;
  } catch (error) { status.textContent = error.message; }
}

async function saveCsv(path, filename) {
  const authHeader = token ? { Authorization: `Bearer ${token}` } : {};
  const response = await fetch(path, { headers: authHeader });
  if (!response.ok) {
    let detail = 'Ошибка выгрузки';
    try { detail = errorText(await response.json()); } catch { /* ответ без JSON */ }
    throw new Error(detail || 'Ошибка выгрузки');
  }
  const blob = await response.blob();
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(blob);
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(anchor.href);
}

async function downloadCsv(kind) {
  const message = document.querySelector('#admin-message');
  try {
    const homework = document.querySelector('#export-hw').value;
    if (!homework) throw new Error('Выберите домашнее задание');
    await saveCsv(`/api/admin/export/${encodeURIComponent(homework)}/${kind}.csv`, `ведомость-${homework}.csv`);
    if (message) { message.className = 'notice'; message.textContent = ''; }
  } catch (error) {
    if (message) { message.className = 'notice error-notice'; message.textContent = error.message; }
  }
}

async function downloadCourseBundle() {
  const message = document.querySelector('#admin-message');
  try {
    await saveCsv('/api/admin/course-export/gradebook.csv', 'ведомость-курса.csv');
    await saveCsv('/api/admin/course-export/journal.csv', 'журнал-проверок.csv');
    if (message) { message.className = 'notice'; message.textContent = 'Скачаны ведомость курса и журнал проверок.'; }
  } catch (error) {
    if (message) { message.className = 'notice error-notice'; message.textContent = error.message; }
  }
}

if (token) route();
else showAuth();
