(() => {
  const escapeHtml = (value) => String(value).replaceAll('\u2014', '–').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[char]);

  function inlineMarkdown(value) {
    const fragments = [];
    const token = (html) => {
      const key = `\u0000MD${fragments.length}\u0000`;
      fragments.push(html);
      return key;
    };

    let text = String(value).replace(/`([^`]+)`/g, (_, code) => token(`<code>${escapeHtml(code)}</code>`));
    text = text.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+["']([^"']*)["'])?\)/g, (_, label, destination, title) => {
      let url;
      try {
        url = new URL(destination, window.location.href);
      } catch {
        return label;
      }
      if (!['http:', 'https:', 'mailto:'].includes(url.protocol)) return label;
      const titleAttribute = title ? ` title="${escapeHtml(title)}"` : '';
      const target = url.protocol === 'mailto:' ? '' : ' target="_blank" rel="noopener noreferrer"';
      return token(`<a href="${escapeHtml(url.href)}"${titleAttribute}${target}>${inlineMarkdown(label)}</a>`);
    });

    text = escapeHtml(text)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__(.+?)__/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/(^|[^_])_([^_\n]+)_/g, '$1<em>$2</em>')
      .replace(/~~(.+?)~~/g, '<del>$1</del>');

    return text.replace(/\u0000MD(\d+)\u0000/g, (_, index) => fragments[Number(index)]);
  }

  function splitTableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown ?? '').replaceAll('\u2014', '–').replace(/\r\n?/g, '\n').split('\n');
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*(```+|~~~+)(.*)$/);
      if (fence) {
        const code = [];
        const marker = fence[1][0];
        index += 1;
        while (index < lines.length && !new RegExp(`^\\s*${marker}{3,}\\s*$`).test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const language = fence[2].trim().split(/\s+/)[0];
        const className = /^[a-z0-9_-]+$/i.test(language) ? ` class="language-${language}"` : '';
        blocks.push(`<pre class="markdown-code"><code${className}>${escapeHtml(code.join('\n'))}</code></pre>`);
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const level = heading[1].length;
        blocks.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        blocks.push('<hr>');
        index += 1;
        continue;
      }

      if (index + 1 < lines.length && line.includes('|') && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])) {
        const headers = splitTableRow(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        blocks.push(`<div class="markdown-table-wrap"><table class="markdown-table"><thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${headers.map((_, column) => `<td>${inlineMarkdown(row[column] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }

      if (/^\s*>/.test(line)) {
        const quote = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          quote.push(lines[index].replace(/^\s*>\s?/, ''));
          index += 1;
        }
        blocks.push(`<blockquote>${renderMarkdown(quote.join('\n'))}</blockquote>`);
        continue;
      }

      const listMatch = line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
      if (listMatch) {
        const ordered = Boolean(listMatch[2]);
        const items = [];
        while (index < lines.length) {
          const item = lines[index].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
          if (!item || Boolean(item[2]) !== ordered) break;
          items.push(`<li>${inlineMarkdown(item[3])}</li>`);
          index += 1;
        }
        const tag = ordered ? 'ol' : 'ul';
        blocks.push(`<${tag}>${items.join('')}</${tag}>`);
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && lines[index].trim() && !/^\s{0,3}(?:#{1,6}\s|```|~~~|>|([-+*]|\d+[.)])\s)/.test(lines[index])) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      blocks.push(`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`);
    }

    return blocks.join('\n');
  }

  window.CabinetMarkdown = { render: renderMarkdown };
})();
