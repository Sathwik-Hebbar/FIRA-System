/* auth.js – Frontend authentication utilities for FIRA */

const API = 'http://localhost:8000';

/* ── JWT helpers ──────────────────────────────────────────────────────── */
function _b64Decode(str) {
  str = str.replace(/-/g, '+').replace(/_/g, '/');
  const pad = str.length % 4;
  if (pad) str += '='.repeat(4 - pad);
  return decodeURIComponent(
    atob(str).split('').map(c => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)).join('')
  );
}

function _escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
}

function getToken() {
  return localStorage.getItem('fira_token');
}

function getUser() {
  const raw = localStorage.getItem('fira_user');
  if (raw) {
    try { return JSON.parse(raw); } catch (_) {}
  }
  // fall back to decoding the JWT
  const token = getToken();
  if (!token) return null;
  try {
    return JSON.parse(_b64Decode(token.split('.')[1]));
  } catch (_) {
    return null;
  }
}

function isExpired(user) {
  if (!user || !user.exp) return true;
  return Math.floor(Date.now() / 1000) >= user.exp;
}

function logout() {
  localStorage.removeItem('fira_token');
  localStorage.removeItem('fira_user');
  window.location.href = 'login.html';
}

function errorMessage(err, fallback = 'Something went wrong. Please try again.') {
  const message = String(err && err.message || '');
  if (message.includes('401')) return 'Your session has expired. Please sign in again.';
  if (message.includes('403')) return 'You do not have permission to perform that action.';
  if (message.includes('404')) return 'That record could not be found.';
  if (message.includes('409')) return 'That incident is not ready for this status change.';
  if (message.includes('400')) return 'Please check the information and try again.';
  if (message.includes('Failed') || message.includes('NetworkError')) return 'The service is unavailable. Please try again.';
  return fallback;
}

/* ── Authenticated fetch – injects Bearer token ───────────────────────── */
async function apiFetch(path, opts = {}) {
  const token = getToken();
  const headers = { ...(opts.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { logout(); return; }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* ── Route guard ─────────────────────────────────────────────────────── */
function enforceAuth(requiredRole) {
  const user = getUser();

  if (!user || isExpired(user)) {
    logout();
    return null;
  }

  const role = user.role || '';

  if (requiredRole && role !== requiredRole) {
    // Cross-role redirect
    if (role === 'citizen') {
      window.location.href = 'index.html';
    } else if (role === 'command_center') {
      window.location.href = 'command.html';
    } else {
      logout();
    }
    return null;
  }

  // Build navigation
  _buildNav(role, user);
  return user;
}

function _buildNav(role, user) {
  const nav = document.getElementById('nav');
  if (!nav) return;

  const citizenLinks = [
    { href: 'index.html',    label: '🗺 Map' },
    { href: 'report.html',   label: '⚠️ Report Flood' },
    { href: 'my-reports.html', label: '📋 My Reports' },
  ];
  const commandLinks = [
    { href: 'dashboard.html', label: '📊 Dashboard' },
    { href: 'index.html',     label: '🗺 Live Map' },
    { href: 'command.html',   label: '🚨 Incidents' },
  ];

  const links = role === 'citizen' ? citizenLinks : commandLinks;

  nav.innerHTML = `
    <div class="nav-bar">
      <span class="nav-brand">🌊 FIRA</span>
      <div class="nav-links">
        ${links.map(l => `<a href="${l.href}" class="nav-link">${l.label}</a>`).join('')}
      </div>
      <div class="nav-user">
        <span class="nav-username">${_escapeHtml(user.name || user.sub || 'User')}</span>
        <button class="btn btn-ghost nav-logout" onclick="window.auth.logout()">Logout</button>
      </div>
    </div>
  `;
}

/* ── Expose globally ─────────────────────────────────────────────────── */
window.auth = { getToken, getUser, logout, enforceAuth, apiFetch, errorMessage };
