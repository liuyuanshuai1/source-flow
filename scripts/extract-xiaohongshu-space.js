(() => {
  const rows = window.__INITIAL_STATE__?.user?.notes?._value?.[0] || [];
  return JSON.stringify(rows.slice(0, 20).map(item => ({
    id: item.noteCard?.noteId || item.id,
    title: item.noteCard?.displayTitle || item.noteCard?.title || '',
    time: item.noteCard?.time || null,
    sticky: Boolean(item.noteCard?.interactInfo?.sticky),
    type: item.noteCard?.type || null,
    author: item.noteCard?.user?.userId || null,
    xsec_token: item.xsecToken || item.noteCard?.xsecToken || null
  })));
})()
