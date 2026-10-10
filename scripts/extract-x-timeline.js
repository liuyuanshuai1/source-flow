(() => {
  const handle = location.pathname.split('/').filter(Boolean)[0] || '';
  const escaped = handle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`^/${escaped}/status/(\\d+)$`, 'i');
  const rows = [];
  for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
    const time = article.querySelector('time');
    const statusLink = [...article.querySelectorAll('a[href]')]
      .map(a => a.getAttribute('href'))
      .find(href => pattern.test((href || '').split('?')[0]));
    if (!time || !statusLink) continue;
    const match = statusLink.split('?')[0].match(pattern);
    const text = article.querySelector('[data-testid="tweetText"]')?.innerText || '';
    rows.push({
      handle,
      id: match[1],
      url: new URL(statusLink.split('?')[0], location.origin).href,
      published_at: time.dateTime || time.getAttribute('datetime'),
      text,
      timeline: location.pathname.endsWith('/with_replies') ? 'with_replies' : 'posts'
    });
  }
  return JSON.stringify([...new Map(rows.map(row => [row.id, row])).values()]);
})()
