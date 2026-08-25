// Shared fetch helper: same-origin cookie auth, auto-redirect to login
// on 401 (except on the login/signup pages themselves).
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: options.body instanceof FormData
      ? options.headers
      : { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (res.status === 401 && !location.pathname.endsWith('login.html') && !location.pathname.endsWith('signup.html')) {
    location.href = '/login.html';
    return null;
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function requireAuth() {
  try {
    return await api('/api/auth/me');
  } catch (e) {
    location.href = '/login.html';
    return null;
  }
}

async function logout() {
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/login.html';
}

// Escape text pulled from external sources (news calendar titles, broker
// position fields) before it's ever inserted via innerHTML, so a
// compromised or malformed upstream feed can't inject markup/script
// into the page.
function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
