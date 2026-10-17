/**
 * MedRecover AI — Enterprise Clinic Denial Recovery & Appeals Console
 * Client Application Logic — High-Tech Action-Oriented Operations
 */

// Application State
const state = {
  apiKey: sessionStorage.getItem('denial_demo_key') || 'change-me-for-local-demo',
  claims: [],
  metrics: null,
  activeClaim: null,
  activeAppeal: null,
  activeLevel: 1,
  activeCategory: 'all',
  searchQuery: '',
  statusFilter: 'all',
  sortBy: 'priority',
  selectedClaimIds: new Set(),
  clinic: localStorage.getItem('medrecover_clinic') || 'all',
  lastUpdated: null
};

const $ = id => document.getElementById(id);

// Formatters
const money = n => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n || 0);

function formatDate(isoStr) {
  if (!isoStr) return 'Not set';
  try {
    const parts = isoStr.slice(0, 10).split('-');
    if (parts.length === 3) {
      const d = new Date(parts[0], parts[1] - 1, parts[2]);
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    }
    return isoStr;
  } catch {
    return isoStr;
  }
}

// Toast Notifications
function showToast(message, type = 'info') {
  const toast = $('toast');
  if (!toast) return;
  toast.textContent = (type === 'success' ? '✓ ' : type === 'error' ? '✕ ' : 'ℹ ') + message;
  toast.className = `toast ${type}`;
  toast.style.display = 'flex';
  setTimeout(() => {
    toast.style.display = 'none';
  }, 4000);
}

// API Communication Helper
async function api(path, opts = {}) {
  const key = state.apiKey.trim();
  if (!key && !state.owner) {
    showToast('Sign in as owner (python tools/owner_access.py) or enter an API key.', 'error');
    throw new Error('Authentication required');
  }

  const headers = {
    // Owner session rides on the HttpOnly cookie; the custom header is the CSRF guard for writes.
    ...(state.owner ? { 'X-Owner-Session': '1' } : { 'X-API-Key': key }),
    ...(opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
    ...(opts.headers || {})
  };

  const response = await fetch('/api' + path, { ...opts, headers });
  const data = await response.json().catch(() => null);

  if (!response.ok) {
    const errorMsg = typeof data?.detail === 'string' 
      ? data.detail 
      : JSON.stringify(data?.detail || `Server returned error ${response.status}`);
    showToast(errorMsg, 'error');
    throw new Error(errorMsg);
  }

  return data;
}

// Post-quantum security actions
async function verifyPqcAttestation() {
  try {
    const a = await api('/security/attestation');
    showToast(a.self_check
      ? `ML-DSA-65 signature valid over audit head. Key fingerprint ${a.public_key_sha3_256.slice(0, 16)}…`
      : 'Attestation signature did NOT verify.',
      a.self_check ? 'success' : 'error');
  } catch (err) { /* surfaced by api() */ }
}

async function ownerLogout() {
  await fetch('/api/owner/logout', { method: 'POST' });
  window.location.replace('/owner-login');
}

async function detectOwner() {
  try {
    const r = await fetch('/api/owner/session');
    const s = await r.json();
    state.owner = !!s.owner;
    if (state.owner) {
      document.body.classList.add('is-owner');
      if ($('ownerLabel')) { $('ownerLabel').textContent = 'Owner · ' + s.email; $('ownerLabel').style.display = ''; }
      const card = document.querySelector('.sidebar-card');
      if (card) card.style.display = 'none';
    }
  } catch (e) { state.owner = false; }
  const lo = $('btnOwnerLogout');
  if (lo) lo.style.display = state.owner ? '' : 'none';
}

// Authentication
function saveKey() {
  const inputKey = $('key').value.trim();
  if (!inputKey) {
    showToast('Please enter an API key.', 'error');
    return;
  }
  state.apiKey = inputKey;
  sessionStorage.setItem('denial_demo_key', inputKey);
  showToast('API Key configured.', 'success');
  refresh();
}

// Navigation
function showSection(sectionId) {
  const sections = ['actions', 'remit', 'dashboard', 'intake', 'audit'];
  sections.forEach(s => {
    const el = $(s);
    if (el) el.style.display = (s === sectionId) ? 'block' : 'none';
  });

  $('navActions').classList.toggle('active', sectionId === 'actions');
  $('navRemit').classList.toggle('active', sectionId === 'remit');
  $('navDashboard').classList.toggle('active', sectionId === 'dashboard');
  $('navIntake').classList.toggle('active', sectionId === 'intake');
  $('navAudit').classList.toggle('active', sectionId === 'audit');

  if (sectionId === 'audit') {
    loadAudit();
  }
  if (sectionId === 'actions') {
    loadActionCenter();
  }
}

// =============================================================================
// Action Center — ranked, one-click recovery actions
// =============================================================================
async function loadActionCenter() {
  try {
    const cc = await api('/analytics/command-center');
    state.commandCenter = cc;
    renderActionCenter(cc);
  } catch (err) {
    $('acSubtitle').textContent = 'Could not load analytics. Check your API key.';
  }
}

function renderActionCenter(cc) {
  const k = cc.kpis || {};
  const plays = cc.todays_plays || [];

  $('acSubtitle').textContent = plays.length
    ? `${plays.length} actions worth ${money(plays.reduce((s, p) => s + p.expected_value, 0))} in expected recovery. Start at the top.`
    : 'No open claims need action. Seed or import claims to begin.';

  const kpi = (label, value, caption, tone) => `
    <article class="stat-card">
      <div class="stat-header"><span class="stat-label">${label}</span></div>
      <div class="stat-value" style="${tone ? `color: var(--accent-${tone});` : ''}">${value}</div>
      <div class="stat-caption">${caption}</div>
    </article>`;
  $('acKpis').innerHTML = [
    kpi('Expected recovery', money(k.expected_recoverable), `${k.open_claims || 0} open claims`, 'emerald'),
    kpi('Expiring in 7 days', money(k.expiring_7d), 'Appeal now or lose it', 'rose'),
    kpi('Idle & at risk', money(k.idle_expected_value_at_risk), 'Untouched, deadline within 14d', 'amber'),
    kpi('Recovered to date', money(k.recovered_to_date), k.recovery_rate != null ? `${k.recovery_rate}% recovery rate` : 'No resolved claims yet', 'emerald')
  ].join('');

  $('acPlays').innerHTML = plays.length ? plays.map((p, i) => {
    const hot = p.days !== null && p.days >= 0 && p.days <= 7;
    const needsDraft = p.status === 'new' || p.status === 'in_review';
    return `
      <div class="ac-play ${hot ? 'hot' : ''}">
        <div class="ac-rank">${i + 1}</div>
        <div class="ac-play-body">
          <div><strong>${escapeHtml(p.claim_number)}</strong> · ${escapeHtml(p.payer)}
            <span class="badge badge-category">${escapeHtml(p.category)}</span></div>
          <div class="stat-caption">${escapeHtml(p.why)}</div>
        </div>
        <div class="ac-play-money">${money(p.expected_value)}<small>of ${money(p.denied_amount)}</small></div>
        <button class="btn ${needsDraft ? 'btn-primary' : 'btn-secondary'} btn-sm" onclick="actOnPlay(${p.id})">
          ${needsDraft ? 'Draft & review →' : 'Open →'}
        </button>
      </div>`;
  }).join('') : '<p class="stat-caption">Nothing to do right now 🎉</p>';

  const weeks = cc.forecast || [];
  const max = Math.max(1, ...weeks.map(w => w.expected));
  $('acForecast').innerHTML = weeks.map(w => `
    <div class="ac-bar" title="Week ${w.week}: ${money(w.expected)} (${w.claims} claims)">
      <div class="ac-bar-fill" style="height: ${Math.round(100 * w.expected / max)}%"></div>
      <span>${w.week}</span>
    </div>`).join('');
  $('acForecastNote').textContent = (cc.assumptions && cc.assumptions.note) || '';

  const prev = (cc.prevention || []).slice(0, 4);
  $('acPrevention').innerHTML = prev.length ? prev.map(p => `
    <div class="ac-prev">
      <div><strong>${escapeHtml(p.headline)}</strong>
        <span class="badge badge-code">${money(p.preventable)} preventable</span></div>
      <div class="stat-caption">${escapeHtml(p.playbook)} Owner: ${escapeHtml(p.owner)}.</div>
    </div>`).join('') : '<p class="stat-caption">No patterns yet.</p>';
}

async function actOnPlay(claimId) {
  await openStudio(claimId);
}

async function draftAllPlays() {
  const plays = ((state.commandCenter || {}).todays_plays || [])
    .filter(p => p.status === 'new' || p.status === 'in_review');
  if (!plays.length) {
    showToast('Every top play already has a draft.', 'info');
    return;
  }
  try {
    const res = await api('/claims/batch-appeals', {
      method: 'POST',
      body: JSON.stringify({ claim_ids: plays.map(p => p.id), level: 1 })
    });
    showToast(`Drafted ${res.generated_count} appeals. Review and approve each before sending.`, 'success');
    await refresh();
    loadActionCenter();
  } catch (err) {
    showToast('Batch drafting failed.', 'error');
  }
}

async function verifyAuditChain() {
  try {
    const v = await api('/compliance/audit/verify', { method: 'POST' });
    showToast(v.intact
      ? `Audit trail intact: ${v.chained} events cryptographically verified.`
      : 'Audit trail integrity check FAILED. Investigate immediately.',
      v.intact ? 'success' : 'error');
  } catch (err) {
    showToast('Audit verification failed.', 'error');
  }
}

// =============================================================================
// Remittance AI — parse 835/EOB, score, import
// =============================================================================
async function loadRemitSample(key) {
  try {
    const s = await api('/remittance/samples');
    $('remitText').value = s[key] || '';
    showToast('Sample loaded. Click Parse & score.', 'info');
  } catch (err) { /* surfaced by api() */ }
}

async function parseRemittance() {
  const text = $('remitText').value.trim();
  if (!text) { showToast('Paste remittance text or load a sample first.', 'error'); return; }
  const source_type = /^ISA\*/.test(text) ? '835' : (/EXPLANATION OF BENEFITS/i.test(text) ? 'eob' : 'auto');
  try {
    const res = await api('/remittance/parse', { method: 'POST', body: JSON.stringify({ text, source_type }) });
    state.remitLines = res.lines || [];
    renderRemitResults(res);
  } catch (err) {
    state.remitLines = [];
    $('btnRemitImport').disabled = true;
  }
}

function renderRemitResults(res) {
  const lines = res.lines || [];
  const total = lines.reduce((s, l) => s + l.denied_amount, 0);
  const ev = lines.reduce((s, l) => s + (l.intel ? l.intel.expected_value : 0), 0);
  $('remitSummary').textContent = `${res.format.toUpperCase()} · ${lines.length} lines · ${money(total)} denied · ${money(ev)} expected recovery`;
  $('btnRemitImport').disabled = lines.length === 0;
  $('remitResults').innerHTML = lines.length ? lines.map(l => `
    <div class="ac-play">
      <div class="ac-play-body">
        <div><strong>${escapeHtml(l.claim_number)}</strong> · ${escapeHtml(l.payer)}
          <span class="badge badge-code">${escapeHtml(l.denial_code)}</span>
          <span class="badge badge-category">${escapeHtml(l.category)}</span></div>
        <div class="stat-caption">${escapeHtml(l.carc_description || '')} · ${l.intel ? escapeHtml(l.intel.path_label) + ' at ' + l.intel.probability + '%' : ''}</div>
      </div>
      <div class="ac-play-money">${money(l.intel ? l.intel.expected_value : 0)}<small>of ${money(l.denied_amount)}</small></div>
    </div>`).join('') : '<p class="stat-caption">No denial lines found.</p>';
}

async function importRemittance() {
  const lines = state.remitLines || [];
  if (!lines.length) return;
  const claims = lines.map(l => ({
    claim_number: l.claim_number, clinic_name: l.clinic_name || undefined, payer: l.payer,
    service_date: l.service_date || new Date().toISOString().slice(0, 10),
    denial_code: l.denial_code, denied_amount: l.denied_amount,
    appeal_deadline: l.appeal_deadline || null, notes: l.carc_description || ''
  }));
  try {
    const res = await api('/remittance/import', { method: 'POST', body: JSON.stringify({ claims }) });
    showToast(`Imported ${res.imported} of ${res.total_submitted} claims` + (res.imported < res.total_submitted ? ' (rest already existed).' : '.'), 'success');
    await refresh();
    showSection('actions');
  } catch (err) { /* surfaced by api() */ }
}

// =============================================================================
// Live clinic selector + auto-refresh
// =============================================================================
function populateClinics() {
  const sel = $('clinicSelect');
  if (!sel) return;
  const names = [...new Set(state.claims.map(c => c.clinic_name).filter(Boolean))].sort();
  if (state.clinic !== 'all' && !names.includes(state.clinic)) state.clinic = 'all';
  sel.innerHTML = '<option value="all">All clinics (' + state.claims.length + ')</option>' +
    names.map(n => {
      const count = state.claims.filter(c => c.clinic_name === n).length;
      return `<option value="${escapeHtml(n)}">${escapeHtml(n)} (${count})</option>`;
    }).join('');
  sel.value = state.clinic;
}

function selectClinic(name) {
  state.clinic = name || 'all';
  localStorage.setItem('medrecover_clinic', state.clinic);
  state.selectedClaimIds.clear();
  renderClaimsTable();
  updateBatchBar();
  showToast(state.clinic === 'all' ? 'Showing all clinics.' : `Showing ${state.clinic}.`, 'info');
}

function markLive(ok) {
  const dot = $('liveDot'), label = $('liveStatus');
  if (ok) state.lastUpdated = new Date();
  if (dot) dot.style.background = ok ? '' : '#fb7185';
  if (label) {
    label.textContent = ok
      ? 'Live · ' + state.lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : 'Offline · retrying';
  }
}

function startLive(intervalMs = 15000) {
  setInterval(async () => {
    if (document.hidden) return;
    const studioOpen = $('studioModal') && $('studioModal').style.display === 'flex';
    if (studioOpen) return;            // never refresh under a user who is editing an appeal
    await refresh();
    if ($('actions') && $('actions').style.display !== 'none') loadActionCenter();
  }, intervalMs);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
}

// Theme Toggle
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('medrecover_theme', next);
}

// Refresh Data & Render UI
async function refresh() {
  try {
    const [claims, metricsData] = await Promise.all([
      api('/claims'),
      api('/metrics')
    ]);

    state.claims = claims || [];
    populateClinics();
    markLive(true);
    state.metrics = metricsData || {};

    renderMetrics();
    renderClaimsTable();
    updateBatchBar();
  } catch (err) {
    console.error('Failed to refresh data:', err);
    markLive(false);
  }
}

function renderMetrics() {
  const m = state.metrics || {};
  $('amount').textContent = money(m.denied_total || 0);
  $('pending').textContent = m.pending_count ?? 0;
  $('totalRecordsLabel').textContent = `${m.claim_count || 0} total claims registered`;
  $('criticalCount').textContent = m.critical_count ?? 0;
  
  if ($('recoveredTotal')) {
    $('recoveredTotal').textContent = money(m.recovered_total || 0);
  }
  if ($('resolvedCountLabel')) {
    $('resolvedCountLabel').textContent = `${m.resolved_count || 0} claims overturned / paid`;
  }

  // Render Top Payer Velocity Chips
  const payerContainer = $('payerChips');
  if (payerContainer && m.by_payer) {
    const entries = Object.entries(m.by_payer);
    if (entries.length === 0) {
      payerContainer.innerHTML = '<span class="stat-caption">No payer exposure recorded yet.</span>';
    } else {
      payerContainer.innerHTML = entries.map(([payer, amt]) => `
        <div class="payer-chip">
          <span>${escapeHtml(payer)}:</span>
          <strong>${money(amt)}</strong>
        </div>
      `).join('');
    }
  }
}

// Table Rendering & Checkboxes
function renderClaimsTable() {
  const tbody = $('claimsTableBody');
  if (!tbody) return;

  let list = [...state.claims];

  if (state.clinic !== 'all') {
    list = list.filter(c => c.clinic_name === state.clinic);
  }

  if (state.activeCategory !== 'all') {
    list = list.filter(c => c.analysis?.category?.toLowerCase() === state.activeCategory.toLowerCase());
  }

  if (state.statusFilter !== 'all') {
    list = list.filter(c => c.status === state.statusFilter);
  }

  if (state.searchQuery.trim()) {
    const q = state.searchQuery.toLowerCase().trim();
    list = list.filter(c => 
      c.claim_number.toLowerCase().includes(q) ||
      c.payer.toLowerCase().includes(q) ||
      c.denial_code.toLowerCase().includes(q) ||
      c.clinic_name.toLowerCase().includes(q)
    );
  }

  if (state.sortBy === 'priority') {
    list.sort((a, b) => (b.analysis?.priority_score || 0) - (a.analysis?.priority_score || 0));
  } else if (state.sortBy === 'amount') {
    list.sort((a, b) => (b.denied_amount || 0) - (a.denied_amount || 0));
  } else if (state.sortBy === 'deadline') {
    list.sort((a, b) => (a.appeal_deadline || '9999-99-99').localeCompare(b.appeal_deadline || '9999-99-99'));
  }

  if (list.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="9" style="text-align: center; padding: 40px; color: var(--text-dim);">
          <div style="font-size: 28px; margin-bottom: 8px;">📭</div>
          <p>No claims found matching the current filters.</p>
          <button class="btn btn-secondary btn-sm" style="margin-top: 12px;" onclick="seedDemoClaims()">
            Seed 10 Sample Claims
          </button>
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = list.map(c => {
    const ana = c.analysis || {};
    const days = ana.days_to_deadline;
    let deadlineBadgeClass = 'badge-safe';
    let deadlineText = formatDate(c.appeal_deadline);

    if (days !== null && days !== undefined) {
      if (days < 0) {
        deadlineBadgeClass = 'badge-expired';
        deadlineText = `Expired (${Math.abs(days)}d ago)`;
      } else if (days <= 7) {
        deadlineBadgeClass = 'badge-critical';
        deadlineText = `🔥 ${days}d left (${formatDate(c.appeal_deadline)})`;
      } else if (days <= 21) {
        deadlineBadgeClass = 'badge-urgent';
        deadlineText = `⏳ ${days}d left (${formatDate(c.appeal_deadline)})`;
      } else {
        deadlineText = `${days}d left (${formatDate(c.appeal_deadline)})`;
      }
    }

    const pScore = ana.priority_score || 50;
    const pTier = ana.priority_label || 'MED';
    const meterClass = pScore >= 75 ? 'crit' : pScore >= 50 ? 'high' : pScore >= 30 ? 'med' : 'low';
    const isChecked = state.selectedClaimIds.has(c.id);

    return `
      <tr>
        <td>
          <input type="checkbox" style="cursor: pointer; accent-color: var(--primary);" ${isChecked ? 'checked' : ''} onchange="toggleClaimSelect(${c.id}, this.checked)">
        </td>
        <td>
          <div class="claim-title">${escapeHtml(c.claim_number)}</div>
          <div class="clinic-subtitle">${escapeHtml(c.clinic_name)} · DOS: ${formatDate(c.service_date)}</div>
        </td>
        <td>
          <strong>${escapeHtml(c.payer)}</strong>
        </td>
        <td>
          <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px;">
            <span class="badge badge-code">${escapeHtml(c.denial_code)}</span>
            <span class="badge badge-category">${escapeHtml(ana.category || 'Review')}</span>
          </div>
          <div class="clinic-subtitle" style="max-width: 260px; text-overflow: ellipsis; overflow: hidden; white-space: nowrap;" title="${escapeHtml(c.notes || '')}">
            ${escapeHtml(c.notes || ana.recommended_action || '')}
          </div>
        </td>
        <td>
          <strong style="font-size: 14.5px; font-family: var(--font-heading);">${money(c.denied_amount)}</strong>
          ${c.recovered_amount ? `<div style="font-size: 11px; color: var(--accent-emerald);">Won: ${money(c.recovered_amount)}</div>` : ''}
        </td>
        <td>
          <span class="badge ${deadlineBadgeClass}">${deadlineText}</span>
        </td>
        <td>
          <div class="priority-meter">
            <span style="font-weight: 700; font-size: 13px; font-family: var(--font-mono); width: 28px;">${pScore}</span>
            <div class="meter-track">
              <div class="meter-fill ${meterClass}" style="width: ${pScore}%;"></div>
            </div>
            <span style="font-size: 10px; font-weight: 700; color: var(--text-dim);">${pTier}</span>
          </div>
        </td>
        <td>
          <span class="status-pill status-${escapeHtml(c.status)}">${escapeHtml(c.status)}</span>
        </td>
        <td style="text-align: right;">
          <button class="btn btn-primary btn-sm" onclick="openStudio(${c.id})">
            Review & Appeal →
          </button>
        </td>
      </tr>
    `;
  }).join('');
}

// Batch Operations
function toggleClaimSelect(id, isSelected) {
  if (isSelected) {
    state.selectedClaimIds.add(id);
  } else {
    state.selectedClaimIds.delete(id);
  }
  updateBatchBar();
}

function toggleSelectAll(masterCheckbox) {
  if (masterCheckbox.checked) {
    state.claims.forEach(c => state.selectedClaimIds.add(c.id));
  } else {
    state.selectedClaimIds.clear();
  }
  renderClaimsTable();
  updateBatchBar();
}

function updateBatchBar() {
  const bar = $('batchBar');
  const countBadge = $('batchCountBadge');
  if (!bar || !countBadge) return;

  const count = state.selectedClaimIds.size;
  countBadge.textContent = `${count} Selected`;
  bar.style.display = count > 0 ? 'flex' : 'none';
}

function clearBatchSelection() {
  state.selectedClaimIds.clear();
  if ($('selectAllCheckbox')) $('selectAllCheckbox').checked = false;
  renderClaimsTable();
  updateBatchBar();
}

async function executeBatchAppeals() {
  const ids = Array.from(state.selectedClaimIds);
  if (ids.length === 0) return;
  try {
    const res = await api('/claims/batch-appeals', {
      method: 'POST',
      body: JSON.stringify({ claim_ids: ids, level: 1 })
    });
    showToast(`Batch generated ${res.generated_count} Level 1 appeal drafts!`, 'success');
    clearBatchSelection();
    refresh();
  } catch (err) {
    showToast('Failed to execute batch appeals.', 'error');
  }
}

async function executeBatchStatus(newStatus) {
  const ids = Array.from(state.selectedClaimIds);
  if (ids.length === 0) return;
  try {
    const res = await api('/claims/batch-status', {
      method: 'POST',
      body: JSON.stringify({ claim_ids: ids, status: newStatus })
    });
    showToast(`Batch updated ${res.updated_count} claims to '${newStatus}'!`, 'success');
    clearBatchSelection();
    refresh();
  } catch (err) {
    showToast('Failed to execute batch status update.', 'error');
  }
}

// Filter Handlers
function filterByCategory(cat) {
  state.activeCategory = cat;
  document.querySelectorAll('#categoryPills .pill-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.cat === cat);
  });
  renderClaimsTable();
}

function handleSearch() {
  state.searchQuery = $('searchInput').value;
  renderClaimsTable();
}

function applyFilters() {
  state.statusFilter = $('statusFilter').value;
  state.sortBy = $('sortFilter').value;
  renderClaimsTable();
}

// =============================================================================
// Appeal Studio (Modal)
// =============================================================================
async function openStudio(claimId) {
  try {
    const data = await api(`/claims/${claimId}`);
    state.activeClaim = data.claim;
    state.activeLevel = 1;
    const ana = data.analysis;
    const dispatch = ana.payer_dispatch || {};

    $('modalClaimNumber').textContent = data.claim.claim_number;
    $('modalCategoryBadge').textContent = ana.category;
    $('modalPriorityBadge').textContent = `Priority ${ana.priority_score}/100 · ${ana.priority_label}`;
    $('modalSubtitle').textContent = `Payer: ${data.claim.payer} | Amount: ${money(data.claim.denied_amount)} | Deadline: ${formatDate(data.claim.appeal_deadline)}`;

    // Payer Dispatch Box
    $('modalPortalLink').href = dispatch.appeals_portal || '#';
    $('modalFaxNumber').textContent = dispatch.appeals_fax || 'N/A';
    $('modalAddress').textContent = dispatch.appeals_address || 'Payer Claims Appeals Dept';

    $('modalDenialCode').textContent = data.claim.denial_code;
    $('modalCategoryText').textContent = ana.category;
    $('modalReversalRate').textContent = `${ana.benchmark_reversal_rate || 60}% Benchmark`;
    $('modalDaysToDeadline').textContent = ana.days_to_deadline !== null ? `${ana.days_to_deadline} days` : 'Not provided';
    $('modalRecommendedAction').textContent = ana.recommended_action;
    $('modalCitation').textContent = ana.statutory_citation || 'CMS / Standard RCM Regulations';

    // Populate Evidence Checklist
    const evidenceList = $('modalEvidenceList');
    evidenceList.innerHTML = (ana.required_documents || []).map((doc, i) => `
      <label class="checklist-item">
        <input type="checkbox" id="doc_check_${i}" onchange="updateEvidenceProgress()">
        <span class="checklist-text">${escapeHtml(doc)}</span>
      </label>
    `).join('');

    // Level buttons state
    $('btnLevel1').classList.add('active');
    $('btnLevel2').classList.remove('active');

    // Status dropdown
    $('modalStatusSelect').value = data.claim.status;

    // Check for existing appeal draft
    if (data.appeals && data.appeals.length > 0) {
      state.activeAppeal = data.appeals[0];
      $('modalLetterTextarea').value = state.activeAppeal.body;
      $('modalConfirmReview').checked = state.activeAppeal.approved;
    } else {
      state.activeAppeal = null;
      $('modalLetterTextarea').value = '';
      $('modalConfirmReview').checked = false;
      generateAppealDraft(1);
    }

    $('studioModal').style.display = 'flex';
    resetRadarPanel();
    loadLatestRadar(claimId);
  } catch (err) {
    showToast('Failed to load claim detail.', 'error');
  }
}

function closeStudio() {
  $('studioModal').style.display = 'none';
  state.activeClaim = null;
  state.activeAppeal = null;
}

function copyPayerFax() {
  const fax = $('modalFaxNumber').textContent;
  navigator.clipboard.writeText(fax);
  showToast(`Copied payer fax: ${fax}`, 'success');
}

function switchAppealLevel(level) {
  state.activeLevel = level;
  $('btnLevel1').classList.toggle('active', level === 1);
  $('btnLevel2').classList.toggle('active', level === 2);
  generateAppealDraft(level);
}

function updateEvidenceProgress() {
  const total = document.querySelectorAll('#modalEvidenceList input[type="checkbox"]').length;
  const checked = document.querySelectorAll('#modalEvidenceList input[type="checkbox"]:checked').length;
  showToast(`Evidence verified: ${checked} of ${total} items`, 'info');
}

async function generateAppealDraft(level = state.activeLevel) {
  if (!state.activeClaim) return;
  try {
    const appeal = await api(`/claims/${state.activeClaim.id}/appeals?level=${level}`, { method: 'POST' });
    state.activeAppeal = appeal;
    $('modalLetterTextarea').value = appeal.body;
    $('modalStatusSelect').value = 'appeal_drafted';
    showToast(`Level ${level} appeal draft synthesized.`, 'success');
    refresh();
  } catch (err) {
    showToast('Failed to generate draft.', 'error');
  }
}

async function saveEditedLetter() {
  if (!state.activeAppeal) {
    showToast('Please generate an appeal draft first.', 'error');
    return;
  }
  const updatedText = $('modalLetterTextarea').value;
  try {
    await api(`/appeals/${state.activeAppeal.id}`, {
      method: 'PUT',
      body: JSON.stringify({ body: updatedText })
    });
    showToast('Letter edits saved.', 'success');
  } catch (err) {
    showToast('Failed to save letter edits.', 'error');
  }
}

async function recordApproval() {
  if (!state.activeAppeal) {
    showToast('Please generate an appeal draft first.', 'error');
    return;
  }
  if (!$('modalConfirmReview').checked) {
    showToast('Please check the "Human Clinical Sign-Off" confirmation box first.', 'error');
    return;
  }
  try {
    await api(`/appeals/${state.activeAppeal.id}/approval`, {
      method: 'PATCH',
      body: JSON.stringify({ approved: true })
    });
    showToast('Human approval successfully recorded.', 'success');
    refresh();
  } catch (err) {
    showToast('Failed to record approval.', 'error');
  }
}

async function saveClaimStatusFromModal() {
  if (!state.activeClaim) return;
  const newStatus = $('modalStatusSelect').value;
  try {
    await api(`/claims/${state.activeClaim.id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status: newStatus })
    });
    showToast(`Status updated to ${newStatus}.`, 'success');
    refresh();
  } catch (err) {
    showToast('Failed to update status.', 'error');
  }
}

async function copyModalLetter() {
  const text = $('modalLetterTextarea').value;
  if (!text) return;
  await navigator.clipboard.writeText(text);
  showToast('Appeal letter copied to clipboard.', 'success');
}

function downloadModalLetter() {
  const text = $('modalLetterTextarea').value;
  if (!text || !state.activeClaim) return;
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `Appeal_${state.activeClaim.claim_number}_L${state.activeLevel}.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

function printModalLetter() {
  window.print();
}

// =============================================================================
// Evidence Gap Radar
// =============================================================================
const RADAR_COLORS = { MET: '#10b981', PARTIAL: '#f59e0b', MISSING: '#f43f5e' };

function switchStudioTab(tab) {
  const isRadar = tab === 'radar';
  $('tabTriage').style.display = isRadar ? 'none' : 'block';
  $('tabRadar').style.display = isRadar ? 'block' : 'none';
  $('tabBtnTriage').classList.toggle('active', !isRadar);
  $('tabBtnRadar').classList.toggle('active', isRadar);
}

function resetRadarPanel() {
  switchStudioTab('triage');
  $('radarNoteInput').value = '';
  $('radarResult').innerHTML = '';
  $('radarWeeks').value = 6;
  const badge = $('tabRadarBadge');
  badge.style.display = 'none';
  badge.textContent = '';
}

async function loadLatestRadar(claimId) {
  try {
    const res = await api(`/claims/${claimId}/evidence-radar`);
    if (res.scan && state.activeClaim && state.activeClaim.id === claimId) {
      renderRadar(res.scan);
    }
  } catch (err) {
    // api() already surfaced the error
  }
}

async function loadRadarSample() {
  if (!state.activeClaim) return;
  try {
    const res = await api(`/claims/${state.activeClaim.id}/evidence-radar/sample`);
    $('radarNoteInput').value = res.clinical_text;
    showToast(`Loaded synthetic ${res.category.toLowerCase()} note (intentionally incomplete).`, 'info');
  } catch (err) {
    // handled in api()
  }
}

async function runEvidenceRadar() {
  if (!state.activeClaim) return;
  const text = $('radarNoteInput').value.trim();
  if (text.length < 40) {
    showToast('Paste at least a few sentences of de-identified chart text first.', 'error');
    return;
  }
  const btn = $('btnRadarRun');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    const result = await api(`/claims/${state.activeClaim.id}/evidence-radar`, {
      method: 'POST',
      body: JSON.stringify({
        clinical_text: text,
        conservative_weeks: parseInt($('radarWeeks').value, 10) || 6
      })
    });
    renderRadar(result);
    showToast(`Evidence coverage ${result.coverage}/100 — ${result.verdict}.`, result.verdict === 'READY' ? 'success' : 'info');
  } catch (err) {
    // handled in api()
  } finally {
    btn.disabled = false;
    btn.textContent = '📡 Scan Evidence';
  }
}

function wrapLabel(text, max = 14) {
  const lines = [];
  let cur = '';
  text.split(' ').forEach(word => {
    if ((cur + ' ' + word).trim().length > max && cur) {
      lines.push(cur);
      cur = word;
    } else {
      cur = (cur + ' ' + word).trim();
    }
  });
  if (cur) lines.push(cur);
  return lines;
}

function scoreColor(score) {
  return score >= 80 ? RADAR_COLORS.MET : score >= 50 ? RADAR_COLORS.PARTIAL : RADAR_COLORS.MISSING;
}

function buildRadarSvg(axes) {
  const W = 460, H = 310, cx = W / 2, cy = H / 2 + 2, R = 96, n = axes.length;
  const angle = i => -Math.PI / 2 + (2 * Math.PI * i) / n;
  const pt = (i, f) => [cx + Math.cos(angle(i)) * R * f, cy + Math.sin(angle(i)) * R * f];
  const poly = f => axes.map((_, i) => pt(i, f).join(',')).join(' ');

  const rings = [0.25, 0.5, 0.75, 1].map(f =>
    `<polygon class="rc-ring ${f === 1 ? 'outer' : ''}" points="${poly(f)}"/>`).join('');
  const spokes = axes.map((_, i) => {
    const [x, y] = pt(i, 1);
    return `<line class="rc-spoke" x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"/>`;
  }).join('');
  const shape = axes.map((a, i) => pt(i, Math.max(a.score, 3) / 100).join(',')).join(' ');
  const dots = axes.map((a, i) => {
    const [x, y] = pt(i, Math.max(a.score, 3) / 100);
    return `<circle class="rc-dot" cx="${x}" cy="${y}" r="4.5" fill="${scoreColor(a.score)}"/>`;
  }).join('');
  const labels = axes.map((a, i) => {
    const [x, y] = pt(i, 1.2);
    const cos = Math.cos(angle(i));
    const anchor = cos > 0.25 ? 'start' : cos < -0.25 ? 'end' : 'middle';
    const lines = wrapLabel(a.axis);
    const startY = y - ((lines.length) * 12) / 2 + 9;
    const tspans = lines.map((ln, k) =>
      `<tspan x="${x}" y="${startY + k * 12}">${escapeHtml(ln)}</tspan>`).join('');
    return `<text class="rc-label" text-anchor="${anchor}">${tspans}` +
      `<tspan class="rc-score" x="${x}" y="${startY + lines.length * 12}" fill="${scoreColor(a.score)}">${a.score}%</tspan></text>`;
  }).join('');

  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Evidence coverage radar chart">` +
    `${rings}${spokes}` +
    `<polygon class="rc-shape" style="transform-origin: ${cx}px ${cy}px;" points="${shape}"/>` +
    `${dots}${labels}</svg>`;
}

function excerptHtml(ev, contra = false) {
  if (!ev) return '';
  return `<div class="crit-excerpt ${contra ? 'contra' : ''}">${escapeHtml(ev.before)}<mark>${escapeHtml(ev.match)}</mark>${escapeHtml(ev.after)}</div>`;
}

function renderRadar(r) {
  const w = r.win_likelihood;
  const verdictClass = r.verdict === 'READY' ? 'verdict-ready' : r.verdict === 'STRENGTHEN' ? 'verdict-strengthen' : 'verdict-risk';
  const verdictLabel = r.verdict === 'READY' ? 'Appeal-ready' : r.verdict === 'STRENGTHEN' ? 'Strengthen before filing' : 'High risk — gaps block appeal';
  const circ = 2 * Math.PI * 36;
  const ringColor = scoreColor(r.coverage);
  const uplift = w.potential - w.current;

  const badge = $('tabRadarBadge');
  badge.textContent = `${r.coverage}`;
  badge.style.display = 'inline-block';

  const hero = `
    <div class="radar-hero">
      <div class="coverage-ring">
        <svg width="86" height="86" viewBox="0 0 86 86">
          <circle class="ring-bg" cx="43" cy="43" r="36"/>
          <circle class="ring-fg" cx="43" cy="43" r="36" stroke="${ringColor}"
            stroke-dasharray="${circ}" stroke-dashoffset="${circ * (1 - r.coverage / 100)}" style="--circ: ${circ};"/>
        </svg>
        <div class="ring-num">${r.coverage}<small>COVERAGE</small></div>
      </div>
      <div class="radar-hero-text">
        <span class="radar-verdict ${verdictClass}">${verdictLabel}</span>
        <div class="radar-counts">
          <b style="color: var(--accent-emerald);">${r.counts.MET}</b> met ·
          <b style="color: var(--accent-amber);">${r.counts.PARTIAL}</b> partial ·
          <b style="color: var(--accent-rose);">${r.counts.MISSING}</b> missing
        </div>
        <div class="radar-odds">
          Win likelihood <strong>${w.current}%</strong> → <strong>${w.potential}%</strong>
          <span class="uplift">(+${uplift} pts if gaps closed)</span><br>
          Expected recovery <strong>${money(w.expected_usd_now)}</strong> → <strong>${money(w.expected_usd_potential)}</strong>
        </div>
      </div>
    </div>`;

  const chart = r.axes.length >= 3
    ? `<div class="radar-chart">${buildRadarSvg(r.axes)}</div>` : '';

  const blockers = r.blockers.length ? `
    <div class="radar-blockers">
      <h4>⛔ Blocking gaps (critical)</h4>
      <ul>${r.blockers.map(b => `<li><strong>${escapeHtml(b.label)}</strong> — ${escapeHtml(b.action)}</li>`).join('')}</ul>
    </div>` : '';

  const icon = { MET: '✓', PARTIAL: '~', MISSING: '!' };
  const criteria = r.criteria.map(c => {
    const cls = c.status.toLowerCase();
    const ev = (c.evidence || []).map(e => excerptHtml(e)).join('') + excerptHtml(c.contradiction, true);
    const skipped = c.optional && c.status === 'MISSING';
    return `
      <div class="criterion ${cls}" style="${skipped ? 'opacity: 0.55;' : ''}">
        <div class="crit-icon">${icon[c.status]}</div>
        <div class="crit-body">
          <div class="crit-title">
            ${escapeHtml(c.label)}
            ${c.critical ? '<span class="crit-tag">Critical</span>' : ''}
            ${c.optional ? '<span class="crit-tag axis">If applicable</span>' : ''}
            <span class="crit-tag axis">${escapeHtml(c.axis)}</span>
          </div>
          <div class="crit-detail">${escapeHtml(c.detail)}</div>
          ${ev}
        </div>
      </div>`;
  }).join('');

  const plan = r.gap_plan.length ? r.gap_plan.map(g => `
    <div class="gap-item">
      <div class="gap-rank">${g.rank}</div>
      <div class="gap-main">
        <div class="gap-title">${escapeHtml(g.label)}</div>
        <div class="gap-action">${escapeHtml(g.action)}</div>
        <div class="gap-chips">
          ${g.critical ? '<span class="gap-chip" style="color:#fb7185;">Critical</span>' : ''}
          <span class="gap-chip">Owner: ${escapeHtml(g.owner)}</span>
          <span class="gap-chip">Effort: ${escapeHtml(g.effort)}</span>
        </div>
      </div>
      <div class="gap-gain"><strong>+${g.gain_pts} pts</strong><span>≈ ${money(g.gain_usd)}</span></div>
    </div>`).join('')
    : '<p class="stat-caption">No open gaps — the chart satisfies every in-scope criterion.</p>';

  const scanned = r.scanned_at ? `Last scan ${r.scanned_at.replace('T', ' ').slice(0, 16)} UTC · note SHA-256 ${r.note_sha256.slice(0, 12)}…` : '';

  $('radarResult').innerHTML = `
    ${hero}
    ${chart}
    ${blockers}
    <div class="studio-section-title"><span>🧾</span> Criteria Audit</div>
    ${criteria}
    <div class="studio-section-title" style="margin-top: 18px;"><span>🛠</span> Gap-Closure Plan <span class="stat-caption">(ranked: blockers first, then recovery impact)</span></div>
    ${plan}
    <div class="radar-actions">
      <button class="btn btn-success btn-sm" onclick="regenerateAppealWithEvidence()">✉ Regenerate appeal with evidence map</button>
    </div>
    <p class="radar-disclaimer">${escapeHtml(r.policy_basis)}<br>${escapeHtml(r.disclaimer)}<br>${escapeHtml(scanned)}</p>`;
}

async function regenerateAppealWithEvidence() {
  if (!state.activeClaim) return;
  await generateAppealDraft(state.activeLevel);
  showToast('Appeal regenerated with Evidence Map and open-gap checklist.', 'success');
}

// =============================================================================
// Outcome Recording
// =============================================================================
function openOutcomeModal() {
  if (!state.activeClaim) return;
  $('outcome_recovered_amount').value = state.activeClaim.denied_amount;
  $('outcome_notes').value = '';
  $('outcomeModal').style.display = 'flex';
}

function closeOutcomeModal() {
  $('outcomeModal').style.display = 'none';
}

async function submitClaimOutcome() {
  if (!state.activeClaim) return;
  const outcomeStatus = $('outcome_status_select').value;
  const recAmt = parseFloat($('outcome_recovered_amount').value) || 0.0;
  const notes = $('outcome_notes').value.trim();

  try {
    const res = await api(`/claims/${state.activeClaim.id}/outcome`, {
      method: 'POST',
      body: JSON.stringify({
        outcome_status: outcomeStatus,
        recovered_amount: recAmt,
        notes: notes
      })
    });
    showToast(res.message, 'success');
    closeOutcomeModal();
    closeStudio();
    refresh();
  } catch (err) {
    showToast('Failed to record outcome.', 'error');
  }
}

// =============================================================================
// Intake & Upload Handlers
// =============================================================================
$('createClaimForm')?.addEventListener('submit', async e => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData);
  payload.denied_amount = parseFloat(payload.denied_amount);
  if (!payload.appeal_deadline) payload.appeal_deadline = null;

  if (/\b\d{3}-\d{2}-\d{4}\b/.test(payload.notes || '')) {
    showToast('Safety Alert: Notes resemble an SSN. Real PHI is prohibited.', 'error');
    return;
  }

  try {
    await api('/claims', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    e.target.reset();
    showToast(`Claim ${payload.claim_number} created and triaged!`, 'success');
    showSection('dashboard');
    refresh();
  } catch (err) {
    // Handled in api()
  }
});

function handleFileSelected(input) {
  if (input.files && input.files[0]) {
    $('fileNamePreview').textContent = `Selected: ${input.files[0].name} (${Math.round(input.files[0].size / 1024)} KB)`;
  }
}

$('uploadCsvForm')?.addEventListener('submit', async e => {
  e.preventDefault();
  const fileInput = $('csvFileInput');
  if (!fileInput.files || !fileInput.files[0]) {
    showToast('Please select a CSV file first.', 'error');
    return;
  }
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);

  try {
    const res = await api('/import/csv', {
      method: 'POST',
      body: formData
    });
    showToast(`Imported ${res.imported} claims successfully!`, 'success');
    fileInput.value = '';
    $('fileNamePreview').textContent = 'No file chosen yet';
    showSection('dashboard');
    refresh();
  } catch (err) {
    // Handled in api()
  }
});

async function seedDemoClaims() {
  try {
    const res = await api('/claims/seed-demo', { method: 'POST' });
    showToast(`Seeded ${res.seeded} realistic synthetic claims into worklist.`, 'success');
    refresh();
  } catch (err) {
    showToast('Failed to seed demo claims.', 'error');
  }
}

async function exportCsv() {
  try {
    const key = state.apiKey.trim();
    const res = await fetch('/api/export/claims.csv', {
      headers: state.owner ? {} : { 'X-API-Key': key }
    });
    if (!res.ok) throw new Error('Export failed');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `claims_export_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('Claims exported to CSV.', 'success');
  } catch (err) {
    showToast('Export failed.', 'error');
  }
}

// =============================================================================
// Audit Log
// =============================================================================
async function loadAudit() {
  const container = $('auditList');
  if (!container) return;
  try {
    const events = await api('/audit');
    if (!events || events.length === 0) {
      container.innerHTML = '<p style="color: var(--text-dim);">No audit events recorded yet.</p>';
      return;
    }

    container.innerHTML = events.map(ev => `
      <div class="info-card" style="margin-bottom: 0; display: flex; justify-content: space-between; align-items: center;">
        <div>
          <span class="badge badge-code" style="margin-right: 8px;">#${ev.id}</span>
          <strong style="color: var(--text-main);">${escapeHtml(ev.action)}</strong>
          <span style="color: var(--text-dim); margin-left: 8px;">Entity ID: ${ev.entity_id ?? 'N/A'}</span>
        </div>
        <span style="font-family: var(--font-mono); font-size: 11.5px; color: var(--text-dim);">
          ${escapeHtml(ev.timestamp.replace('T', ' ').slice(0, 19))} UTC
        </span>
      </div>
    `).join('');
  } catch (err) {
    container.innerHTML = '<p style="color: var(--accent-rose);">Failed to load audit events.</p>';
  }
}

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));
}

// Initialization
window.addEventListener('DOMContentLoaded', () => {
  const savedTheme = localStorage.getItem('medrecover_theme');
  if (savedTheme) {
    document.documentElement.setAttribute('data-theme', savedTheme);
  }

  if ($('key')) $('key').value = state.apiKey;

  if ($('form_service_date')) {
    $('form_service_date').value = new Date().toISOString().slice(0, 10);
  }

  detectOwner().then(() => { refresh(); showSection('actions'); startLive(); });
});
