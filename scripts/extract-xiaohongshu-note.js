(() => {
  const detailMap = window.__INITIAL_STATE__?.note?.noteDetailMap || {};
  const wrapped = Object.values(detailMap)[0] || {};
  const note = wrapped.note || wrapped;
  const images = note.imageList || [];
  const video = note.video || note.videoConsumer || null;
  return JSON.stringify({
    id: note.noteId || note.id || location.pathname.split('/').filter(Boolean).at(-1),
    title: note.title || '',
    desc: note.desc || '',
    time: note.time || null,
    last_update_time: note.lastUpdateTime || null,
    type: note.type || null,
    author: note.user?.userId || null,
    media_count: images.length || (video ? 1 : 0)
  });
})()
