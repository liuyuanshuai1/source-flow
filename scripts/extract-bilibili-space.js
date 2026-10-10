(() => {
  const expectedMid = location.pathname.split('/').filter(Boolean)[0] || '';
  const rows = [];
  for (const anchor of document.querySelectorAll('a[href*="/video/BV"]')) {
    const href = anchor.getAttribute('href') || '';
    const match = href.match(/\/video\/(BV[0-9A-Za-z]+)/);
    if (!match) continue;
    const card = anchor.closest('li, .small-item, .video-card, .bili-video-card, [class*="video"]') || anchor.parentElement;
    const title = anchor.getAttribute('title') || anchor.querySelector('img')?.getAttribute('alt') || anchor.innerText.trim();
    rows.push({expected_mid: expectedMid, bvid: match[1], url: `https://www.bilibili.com/video/${match[1]}/`, title, card_text: card?.innerText || ''});
  }
  return JSON.stringify([...new Map(rows.map(row => [row.bvid, row])).values()]);
})()
