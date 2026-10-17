(function () {
  const status = document.getElementById('loginStatus');
  const box = document.getElementById('tokenBox');

  async function login(token) {
    token = (token || '').trim();
    if (!token) { status.textContent = 'No token found. Run: python tools/owner_access.py'; return; }
    status.textContent = 'Verifying post-quantum signature…';
    try {
      const res = await fetch('/api/owner/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token })
      });
      if (!res.ok) { status.textContent = 'Sign-in refused: token invalid, expired or already used. Generate a new one.'; return; }
      box.value = '';
      status.textContent = 'Signed in as owner. Redirecting…';
      window.location.replace('/');
    } catch (e) {
      status.textContent = 'Network error. Is the server running?';
    }
  }

  document.getElementById('btnPasteLogin').addEventListener('click', () => login(box.value));
  document.getElementById('btnClipboardLogin').addEventListener('click', async () => {
    try { login(await navigator.clipboard.readText()); }
    catch (e) { status.textContent = 'Clipboard blocked by the browser. Paste the token into the box instead.'; }
  });

  // Optional: token delivered in the URL fragment (never sent to the server or logged).
  const m = /^#t=([\w\-.]+)$/.exec(window.location.hash);
  if (m) { history.replaceState(null, '', window.location.pathname); login(m[1]); }
})();
