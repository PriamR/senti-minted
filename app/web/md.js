// Minimal Markdown for agent answers: paragraphs, **bold**, *italic*, "- " lists,
// "#" headings and pipe tables. Everything is HTML-escaped before any markup is added.
function md(src) {
  const esc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, '$1<i>$2</i>');
  const isRow = l => /^\s*\|.*\|\s*$/.test(l);
  const isRule = l => /^\s*\|[\s:|-]+\|\s*$/.test(l);
  const isItem = l => /^\s*[-*] /.test(l);
  const cells = r => r.trim().slice(1, -1).split('|').map(c => c.trim());
  const lines = String(src ?? '').split('\n');
  let out = '';
  for (let i = 0; i < lines.length;) {
    const l = lines[i];
    if (isRow(l)) {
      const rows = [];
      while (i < lines.length && isRow(lines[i])) rows.push(lines[i++]);
      out += '<table class="md">' + rows.filter(r => !isRule(r)).map((r, k) =>
        '<tr>' + cells(r).map(c => k ? `<td>${inline(c)}</td>` : `<th>${inline(c)}</th>`).join('') + '</tr>'
      ).join('') + '</table>';
    } else if (isItem(l)) {
      const items = [];
      while (i < lines.length && isItem(lines[i])) items.push(lines[i++].replace(/^\s*[-*] /, ''));
      out += '<ul>' + items.map(t => `<li>${inline(t)}</li>`).join('') + '</ul>';
    } else {
      if (/^#{1,4} /.test(l)) out += `<p><b>${inline(l.replace(/^#+ /, ''))}</b></p>`;
      else if (l.trim()) out += `<p>${inline(l)}</p>`;
      i++;
    }
  }
  return out;
}
