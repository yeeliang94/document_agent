// Markdown files, like the agent's notes.md, shown formatted. Each line of the file stays its own piece, as in any
// text file, so a comment, a change or a found value can still point at one line; the line is only drawn by its
// role: a heading, a list item, a quote, code or plain text. Everything is escaped before any formatting is added,
// and the formatting adds only fixed tags, so nothing in the file can reach the page as HTML. Links show their
// words only.

// The role of each line: { role, level, marker, text }, with `text` the part of the line that is shown.
export function lineRoles(lines) {
  let fence = false;
  return lines.map((line) => {
    if (/^\s*(```|~~~)/.test(line)) {
      fence = !fence;
      return { role: 'blank', text: '' };
    }
    if (fence) return { role: 'code', text: line };
    let m;
    if (/^\s*$/.test(line)) return { role: 'blank', text: '' };
    if ((m = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) return { role: 'head', level: Math.min(m[1].length, 3), text: m[2] };
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) return { role: 'rule', text: '' };
    if ((m = /^(\s*)[-*+]\s+(?:\[([ xX])\]\s+)?(.*)$/.exec(line))) {
      const marker = m[2] ? (m[2] === ' ' ? '☐' : '☑') : '•';
      return { role: 'item', level: Math.min(Math.floor(m[1].length / 2), 4), marker, text: m[3] };
    }
    if ((m = /^(\s*)(\d{1,9}[.)])\s+(.*)$/.exec(line))) return { role: 'item', level: Math.min(Math.floor(m[1].length / 2), 4), marker: m[2], text: m[3] };
    if ((m = /^\s{0,3}>\s?(.*)$/.exec(line))) return { role: 'quote', text: m[1] };
    if (/^\s*\|.*\|\s*$/.test(line)) return { role: 'table', text: line };
    return { role: 'text', text: line };
  });
}

// Bold, italic, inline code and link words, added to text that is already escaped (and may hold the find bar's
// marks). `plain` skips it, for a search that holds the marks themselves, so a mark never splits a tag.
export function inline(html, plain = false) {
  if (plain) return html;
  return html
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(?=\S)(.+?)\*\*|__(?=\S)(.+?)__/g, (_, a, b) => `<strong>${a ?? b}</strong>`)
    .replace(/(^|[^*\w])\*(?=\S)([^*]+?)\*(?!\*)|(^|\W)_(?=\S)([^_]+?)_(?!\w)/g, (_, p1, a, p2, b) => `${p1 ?? p2}<em>${a ?? b}</em>`)
    .replace(/!?\[([^\]]+)\]\([^)\s]*\)/g, '$1');
}

// The class and opening of one line's piece, by its role.
export function lineLook(role) {
  switch (role.role) {
    case 'head':
      return { cls: `md-h${role.level}`, attrs: ` role="heading" aria-level="${role.level + 1}"`, before: '' };
    case 'item':
      return { cls: `md-item md-l${role.level}`, attrs: '', before: `<span class="md-mark" aria-hidden="true">${role.marker}</span>` };
    default:
      return { cls: `md-${role.role}`, attrs: '', before: '' };
  }
}

// Formatting markers in a search would be split by the marks, so such a search shows the lines unformatted.
export const plainFor = (query) => /[*_`[\]]/.test(query || '');
