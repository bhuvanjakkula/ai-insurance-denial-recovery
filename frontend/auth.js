(function () {
  const $ = id => document.getElementById(id);
  let mode = location.pathname === '/signup' ? 'signup' : 'signin';
  const plan = new URLSearchParams(location.search).get('plan');

  function setMode(m) {
    mode = m;
    const up = m === 'signup';
    $('tabSignin').classList.toggle('active', !up);
    $('tabSignup').classList.toggle('active', up);
    $('signupOnly').style.display = up ? '' : 'none';
    $('signupTerms').style.display = up ? '' : 'none';
    $('pwMeter').style.display = up ? '' : 'none';
    $('authTitle').textContent = up ? 'Start your 3-month free trial' : 'Welcome back';
    $('btnSubmit').textContent = up ? 'Create account — free for 3 months' : 'Sign in';
    $('fPassword').autocomplete = up ? 'new-password' : 'current-password';
    $('pwHint').textContent = up ? 'At least 10 characters, with 3 of: lowercase, uppercase, number, symbol.' : '';
    $('authError').textContent = '';
    history.replaceState(null, '', (up ? '/signup' : '/signin') + (plan ? '?plan=' + encodeURIComponent(plan) : ''));
    document.title = (up ? 'Start your free trial' : 'Sign in') + ' | MedRecover AI';
  }

  function strength(pw) {
    let s = 0;
    if (pw.length >= 10) s++;
    if (pw.length >= 14) s++;
    [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].forEach(r => { if (r.test(pw)) s++; });
    return Math.min(4, Math.floor(s * 4 / 6));
  }

  $('fPassword').addEventListener('input', e => {
    const lvl = strength(e.target.value);
    const bar = $('pwMeter').firstElementChild;
    bar.style.width = (lvl * 25) + '%';
    bar.style.background = ['#fb7185', '#fb7185', '#f59e0b', '#34d399', '#34d399'][lvl];
  });
  $('btnShowPw').addEventListener('click', () => {
    const f = $('fPassword'), show = f.type === 'password';
    f.type = show ? 'text' : 'password';
    $('btnShowPw').textContent = show ? 'Hide' : 'Show';
  });
  $('tabSignin').addEventListener('click', () => setMode('signin'));
  $('tabSignup').addEventListener('click', () => setMode('signup'));
  $('linkDemo').addEventListener('click', e => {
    e.preventDefault();
    sessionStorage.setItem('denial_demo_key', 'change-me-for-local-demo');
    location.replace('/');
  });

  function clientCheck() {
    const email = $('fEmail').value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) return 'Enter a valid e-mail address.';
    if (!$('fPassword').value) return 'Enter your password.';
    if (mode === 'signup') {
      if (!/^\+?[0-9 ()\-.]{8,20}$/.test($('fMobile').value.trim())) return 'Enter your mobile number with country code, e.g. +14155550123.';
      if ($('fPassword').value.length < 10) return 'Password must be at least 10 characters.';
    }
    return '';
  }

  $('authForm').addEventListener('submit', async e => {
    e.preventDefault();
    const err = clientCheck();
    $('authError').textContent = err;
    if (err) return;
    const btn = $('btnSubmit');
    btn.disabled = true;
    const body = { email: $('fEmail').value.trim(), password: $('fPassword').value };
    if (mode === 'signup') {
      body.mobile = $('fMobile').value.trim();
      body.name = $('fName').value.trim();
      body.clinic_name = $('fClinic').value.trim();
    }
    try {
      const res = await fetch('/api/auth/' + mode, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        $('authError').textContent = typeof data.detail === 'string' ? data.detail : 'Check your details and try again.';
        btn.disabled = false;
        return;
      }
      $('fPassword').value = '';
      sessionStorage.removeItem('denial_demo_key');
      location.replace(plan && mode === 'signup' ? '/pricing?welcome=1&plan=' + encodeURIComponent(plan) : '/');
    } catch (ex) {
      $('authError').textContent = 'Network error. Is the server running?';
      btn.disabled = false;
    }
  });

  setMode(mode);
})();
