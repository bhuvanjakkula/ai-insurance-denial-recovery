(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let account = null;

  async function whoami() {
    try {
      const r = await fetch('/api/account/me');
      if (r.ok) account = await r.json();
    } catch (e) { /* signed out */ }
    if (account) {
      $('navSignin').style.display = 'none';
      $('navSignup').style.display = 'none';
      $('eName').value = account.name || '';
      $('eEmail').value = account.email;
      $('eMobile').value = account.mobile || '';
      $('trialBanner').textContent = account.status === 'trial'
        ? `Your free trial: ${account.days_left} days left. You keep full access until it ends.`
        : account.status === 'expired' ? 'Your free trial has ended. Choose a plan to continue.' : `Current plan: ${account.plan}.`;
    } else {
      $('navApp').style.display = 'none';
    }
  }

  async function choose(planId) {
    if (!account) { location.href = '/signup?plan=' + encodeURIComponent(planId); return; }
    const r = await fetch('/api/billing/select-plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Owner-Session': '1' },
      body: JSON.stringify({ plan: planId })
    });
    const d = await r.json().catch(() => ({}));
    const m = $('welcomeMsg');
    m.style.display = '';
    m.textContent = r.ok ? d.message : (d.detail || 'Could not select plan.');
    if (r.ok && d.checkout_url) location.href = d.checkout_url;
    m.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async function loadPlans() {
    const p = await (await fetch('/api/pricing')).json();
    $('plans').innerHTML = p.plans.map(pl => `
      <article class="plan-card ${pl.popular ? 'popular' : ''}">
        ${pl.popular ? '<span class="plan-badge">Most popular</span>' : ''}
        <h2 class="plan-name">${esc(pl.name)}</h2>
        <p class="stat-caption">${esc(pl.tagline)}</p>
        <div class="plan-price"><span>$</span>${pl.price_usd}<small>/month USD</small></div>
        <div class="plan-free">First 3 months free</div>
        <ul>${pl.features.map(f => `<li>✓ ${esc(f)}</li>`).join('')}</ul>
        <button class="btn ${pl.popular ? 'btn-primary' : 'btn-secondary'}" data-plan="${esc(pl.id)}" style="width:100%;">
          ${account ? 'Choose ' + esc(pl.name) : 'Start free trial'}</button>
      </article>`).join('');
    $('plans').querySelectorAll('button[data-plan]').forEach(b => b.addEventListener('click', () => choose(b.dataset.plan)));
    $('supportMail').textContent = p.support_email;
    $('supportMail').href = 'mailto:' + p.support_email;
  }

  $('enqForm').addEventListener('submit', async e => {
    e.preventDefault();
    const st = $('enqStatus');
    st.className = 'auth-error';
    const body = {
      name: $('eName').value.trim(), email: $('eEmail').value.trim(), mobile: $('eMobile').value.trim(),
      topic: $('eTopic').value, message: $('eMessage').value.trim(), website: $('eWebsite').value
    };
    if (body.name.length < 2) { st.textContent = 'Please enter your name.'; return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(body.email)) { st.textContent = 'Enter a valid e-mail so we can reply.'; return; }
    if (body.message.length < 10) { st.textContent = 'Please write at least a sentence (10+ characters).'; return; }
    $('btnEnquire').disabled = true;
    try {
      const r = await fetch('/api/support', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        st.className = 'auth-ok';
        st.textContent = d.message || 'Thank you, we will be in touch.';
        $('eMessage').value = '';
      } else {
        st.textContent = typeof d.detail === 'string' ? d.detail : 'Please check your details.';
      }
    } catch (ex) { st.textContent = 'Network error. You can e-mail us directly.'; }
    $('btnEnquire').disabled = false;
  });

  const q = new URLSearchParams(location.search);
  whoami().then(loadPlans).then(() => {
    if (q.get('welcome')) {
      const m = $('welcomeMsg');
      m.style.display = '';
      m.textContent = 'Welcome! Your 3-month free trial has started. Pick a plan below, or open the app and start recovering revenue.';
    }
  });
})();
