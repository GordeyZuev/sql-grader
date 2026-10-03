(() => {
  function mount(container, { id, value = '', onChange, onRun, onCheck, onFormat, sandbox = false, checkOpen = true }) {
    const savedResultMode = localStorage.getItem('cabinet_result_mode');
    let resultMode = savedResultMode === 'simple' || savedResultMode === 'psql' ? 'simple' : 'pretty';
    let requestInFlight = false;
    let lastResult = null;
    let lastCheck = null;
    container.innerHTML = `
      <div class="editor-card">
        <div class="editor-heading">
          <div><span class="eyebrow">SQL WORKSPACE</span><h2>${sandbox ? 'Песочница' : 'Ваш запрос'}</h2></div>
          <div class="editor-tools">
            <button type="button" class="tool-button" data-format title="Форматировать SQL (⌘/Ctrl + Shift + F)">Форматировать</button>
            <button type="button" class="tool-button" data-copy>Копировать</button>
            <details class="shortcut-menu"><summary aria-label="Сочетания клавиш" title="Сочетания клавиш"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg></summary><div><kbd>⌘/Ctrl + Shift + F</kbd> Форматировать<br><kbd>⌘/Ctrl + Enter</kbd> Выполнить запрос${sandbox ? '' : '<br><kbd>⌘/Ctrl + Shift + Enter</kbd> Проверить'}</div></details>
          </div>
        </div>
        <div class="code-editor" data-editor-shell>
          <div class="line-gutter" data-gutter aria-hidden="true"></div>
          <textarea id="${id}" class="sql-input" data-sql-editor spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off" wrap="off" aria-label="SQL-запрос" placeholder="SELECT ...">${window.CabinetApp.escape(value)}</textarea>
        </div>
        <div class="editor-statusbar"><span data-cursor>Строка 1, столбец 1</span><span data-draft>${sandbox ? 'Запуск не идёт в оценку' : 'Черновик сохраняется автоматически'}</span><span data-count>0 строк</span></div>
        <div class="query-actions">
          <div class="action-buttons"><button type="button" class="secondary" data-run>Выполнить запрос</button>${sandbox ? '' : `<button type="button" class="primary" data-check ${checkOpen ? '' : 'disabled-check'}" ${checkOpen ? '' : 'disabled title="Проверка закрыта после жёсткого срока"'}>${checkOpen ? 'Проверить' : 'Проверка закрыта'}</button>`}</div>
        </div>
        <section class="query-output" data-output aria-live="polite"><div class="output-empty"><span class="output-icon">↳</span><strong>Результат появится здесь</strong></div></section>
      </div>`;

    const editor = container.querySelector('[data-sql-editor]');
    const gutter = container.querySelector('[data-gutter]');
    const draftStatus = container.querySelector('[data-draft]');
    const cursorStatus = container.querySelector('[data-cursor]');
    const countStatus = container.querySelector('[data-count]');
    const output = container.querySelector('[data-output]');
    const runButton = container.querySelector('[data-run]');
    const checkButton = container.querySelector('[data-check]');

    function syncCheckAvailability() {
      const psqlCommand = /^\s*\\/.test(editor.value);
      if (!checkButton) return;
      checkButton.disabled = !checkOpen || requestInFlight || psqlCommand;
      checkButton.title = psqlCommand
        ? 'Команды psql доступны только через «Выполнить запрос»'
        : checkOpen ? '' : 'Проверка закрыта после жёсткого срока';
    }

    function updateMetrics() {
      const lines = editor.value.split('\n').length;
      gutter.innerHTML = Array.from({ length: lines }, (_, index) => `<span>${index + 1}</span>`).join('');
      countStatus.textContent = `${lines} строк · ${editor.value.length} символов`;
      const beforeCursor = editor.value.slice(0, editor.selectionStart).split('\n');
      cursorStatus.textContent = `Строка ${beforeCursor.length}, столбец ${beforeCursor.at(-1).length + 1}`;
    }

    function setDraftStatus(message, state = '') {
      draftStatus.textContent = message;
      draftStatus.dataset.state = state;
    }

    function showResult(result) {
      lastResult = result;
      lastCheck = null;
      if (result.status === 'error') {
        output.innerHTML = `<div class="result-message error-message"><div><strong>Ошибка выполнения</strong><p>${window.CabinetApp.escape(result.message)}</p></div></div>`;
        return;
      }
      output.innerHTML = `<div class="result-summary"><strong>Выполнен</strong><span class="result-stat">${result.row_count} строк</span><span class="result-stat">${result.elapsed_ms} мс</span></div>${resultView(result)}`;
    }

    function resultView(result, showEmpty = true) {
      if (result.error) return '';
      if (!result.columns?.length) return showEmpty ? '<p class="muted output-note">Запрос не вернул таблицу.</p>' : '';
      const toggles = `<div class="result-view-switch" role="group" aria-label="Формат результата"><button type="button" data-result-mode="pretty" class="${resultMode === 'pretty' ? 'active' : ''}">Pretty</button><button type="button" data-result-mode="simple" class="${resultMode === 'simple' ? 'active' : ''}">Simple</button></div>`;
      const content = resultMode === 'simple'
        ? `<pre class="psql-output">${window.CabinetApp.escape(window.CabinetApp.textTable(result.columns, result.rows, result.row_count))}</pre>`
        : window.CabinetApp.dataTable(result.columns, result.rows);
      return `<div class="result-data-heading">${toggles}<span>${result.truncated ? 'Показаны первые 200 строк' : ''}</span></div>${content}`;
    }

    output.addEventListener('click', (event) => {
      const button = event.target.closest('[data-result-mode]');
      if (!button) return;
      resultMode = button.dataset.resultMode;
      localStorage.setItem('cabinet_result_mode', resultMode);
      if (lastCheck) showCheckResult(lastCheck);
      else if (lastResult) showResult(lastResult);
    });

    async function run() {
      if (!onRun || requestInFlight) return;
      requestInFlight = true;
      runButton.disabled = true;
      if (checkButton) checkButton.disabled = true;
      setDraftStatus('Запрос выполняется…', 'busy');
      output.innerHTML = '<div class="output-empty"><span class="spinner"></span><strong>Выполняем запрос…</strong></div>';
      try {
        const result = await onRun(editor.value);
        showResult(result);
        setDraftStatus(sandbox ? 'Запрос выполнен' : 'Черновик сохранён', 'saved');
      } catch (error) {
        showResult({ status: 'error', message: error.message });
        setDraftStatus('Не удалось выполнить запрос', 'error');
      } finally {
        requestInFlight = false;
        runButton.disabled = false;
        syncCheckAvailability();
      }
    }

    async function check() {
      if (!onCheck || !checkOpen || requestInFlight || /^\s*\\/.test(editor.value)) return;
      requestInFlight = true;
      runButton.disabled = true;
      if (checkButton) checkButton.disabled = true;
      setDraftStatus('Идёт проверка…', 'busy');
      output.innerHTML = '<div class="output-empty"><span class="spinner"></span><strong>Проверяем запрос…</strong></div>';
      try {
        const result = await onCheck(editor.value);
        lastCheck = result;
        showCheckResult(result);
        setDraftStatus('Проверка сохранена', 'saved');
      } catch (error) {
        output.innerHTML = `<div class="result-message error-message"><div><strong>Проверка не выполнена</strong><p>${window.CabinetApp.escape(error.message)}</p></div></div>`;
        setDraftStatus('Не удалось проверить запрос', 'error');
      } finally {
        requestInFlight = false;
        runButton.disabled = false;
        syncCheckAvailability();
      }
    }

    function showCheckResult(result) {
      const state = result.verdict === 'OK' ? 'success' : result.verdict === 'STRUCT' ? 'partial' : 'error';
      const labels = { OK: 'Верно', STRUCT: 'Частично', WRONG: 'Неверно', SYNTAX: 'Ошибка синтаксиса', ERROR: 'Ошибка выполнения', EMPTY: 'Неверно', FORBID: 'Запрещённая команда', HARD_LATE: 'Проверка закрыта' };
      const label = labels[result.verdict] || result.verdict;
      const explanation = result.verdict === 'OK' ? '' : `<p>${window.CabinetApp.escape(result.message)}</p>`;
      const queryResult = result.result ? resultView(result.result, false) : '';
      const pointsLine = result.score_locked
        ? `Запрос набрал ${window.CabinetApp.escape(result.points)} из ${window.CabinetApp.escape(result.max_points)}. В кабинете остаётся оценка преподавателя: ${window.CabinetApp.escape(result.score)}`
        : `Баллы: <b>${window.CabinetApp.escape(result.points)} из ${window.CabinetApp.escape(result.max_points)}</b>${result.improved ? ' · улучшено' : ''}`;
      output.innerHTML = `<div class="result-message ${state}-message"><div><strong>${window.CabinetApp.escape(label)}</strong>${explanation}<p>${pointsLine} · ${result.row_count ?? 0} строк · ${result.elapsed_ms ?? 0} мс</p></div></div>${queryResult}`;
    }

    function insertIndent() {
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      editor.setRangeText('    ', start, end, 'end');
      editor.dispatchEvent(new Event('input', { bubbles: true }));
    }

    editor.addEventListener('input', () => {
      updateMetrics();
      syncCheckAvailability();
      if (onChange) {
        setDraftStatus('Изменения сохраняются…', 'busy');
        onChange(editor.value);
      }
    });
    editor.addEventListener('click', updateMetrics);
    editor.addEventListener('keyup', updateMetrics);
    editor.addEventListener('scroll', () => { gutter.scrollTop = editor.scrollTop; });
    editor.addEventListener('keydown', (event) => {
      const command = event.metaKey || event.ctrlKey;
      if (event.key === 'Tab') {
        event.preventDefault();
        insertIndent();
      } else if (command && event.shiftKey && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        container.querySelector('[data-format]').click();
      } else if (command && event.shiftKey && event.key === 'Enter') {
        event.preventDefault();
        check();
      } else if (command && event.key === 'Enter') {
        event.preventDefault();
        run();
      }
    });

    container.querySelector('[data-run]').addEventListener('click', run);
    container.querySelector('[data-check]')?.addEventListener('click', check);
    container.querySelector('[data-format]').addEventListener('click', async () => {
      try {
        const formatted = await onFormat(editor.value);
        editor.value = formatted;
        updateMetrics();
        editor.dispatchEvent(new Event('input', { bubbles: true }));
        setDraftStatus('SQL отформатирован', 'saved');
      } catch (error) {
        setDraftStatus(error.message, 'error');
      }
    });
    container.querySelector('[data-copy]').addEventListener('click', async (event) => {
      try {
        await navigator.clipboard.writeText(editor.value);
        event.currentTarget.textContent = 'Скопировано';
        setTimeout(() => { event.currentTarget.textContent = 'Копировать'; }, 1500);
      } catch {
        setDraftStatus('Браузер не разрешил копирование', 'error');
      }
    });

    updateMetrics();
    syncCheckAvailability();
    editor.focus({ preventScroll: true });
    return {
      editor,
      setDraftStatus,
      setValue(valueToSet) {
        editor.value = valueToSet;
        updateMetrics();
        editor.dispatchEvent(new Event('input', { bubbles: true }));
        editor.focus();
        editor.setSelectionRange(valueToSet.length, valueToSet.length);
      },
    };
  }

  window.CabinetSQLEditor = { mount };
})();
