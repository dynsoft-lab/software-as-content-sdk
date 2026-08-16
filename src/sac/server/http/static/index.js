import { SaCRenderer } from '/renderer/sac-renderer.js';

// ─── Renderer setup ───────────────────────────────────────────

const iframe = document.getElementById('preview');
const placeholder = document.getElementById('placeholder');
const previewNotice = document.getElementById('preview-notice');
const showChangesBtn = document.getElementById('show-changes-btn');
let renderer = createRenderer();

function createRenderer() {
  const r = new SaCRenderer(iframe);
  r.on('render', () => {
    placeholder.classList.add('hidden');
    iframe.classList.remove('hidden');
    hidePreviewNotice();
  });
  r.on('error', (err) => {
    showPreviewNotice('Generated app needs a revision', `${err.type || 'render'}: ${err.message || err}`, { fixable: !!conversationId });
  });
  r.on('action', ({ intent }) => {
    if (!intent) return;
    // Fill input so user can review/edit before sending
    intentInput.value = intent;
    resizeIntentInput();
    intentInput.focus();
  });
  return r;
}

function resetRenderer() {
  // Destroy old renderer and recreate
  if (renderer && renderer.destroy) renderer.destroy();
  renderer = createRenderer();
}

// ─── Show Changes button ─────────────────────────────────────

let _changeIndex = 0;   // cycles through highlighted elements
let _changeCount = 0;

function countChangeMarkers(code) {
  return (String(code || '').match(/\bdata-sac-changed\b/g) || []).length;
}

function setChangeCount(count) {
  _changeCount = Math.max(0, Number(count) || 0);
  if (_changeCount > 0) {
    showChangesBtn.textContent = `Changes ${_changeCount}`;
    showChangesBtn.disabled = false;
    showChangesBtn.classList.remove('changes-btn-empty');
    _changeIndex = 0;
  } else {
    showChangesBtn.textContent = 'Changes';
    showChangesBtn.disabled = true;
    showChangesBtn.classList.add('changes-btn-empty');
  }
}

showChangesBtn.addEventListener('click', () => {
  if (_changeCount <= 0) return;
  const iframeWin = iframe.contentWindow;
  if (!iframeWin) return;
  iframeWin.postMessage({ type: 'scroll-to-change', index: _changeIndex }, '*');
  _changeIndex++;
});

// Listen for change-count and visibility updates from iframe
window.addEventListener('message', (ev) => {
  if (ev.data?.type === 'sac-change-count') {
    const codeCount = countChangeMarkers(codeDisplay?.textContent || '');
    setChangeCount(Math.max(ev.data.count || 0, codeCount));
  }
  if (ev.data?.type === 'sac-highlights-visible') {
    if (!ev.data.visible && _changeCount > 0) {
      // Highlights dismissed — update button text
      showChangesBtn.textContent = `Check Changes (${_changeCount})`;
      _changeIndex = 0;
    }
  }
});

// ─── State ────────────────────────────────────────────────────

let conversationId = null;
let currentVersion = 0;
let callbackUrl = null;       // null = standalone mode; set = external-agent mode
let eventSource = null;        // SSE subscription to /c/{id}/events
let pendingAction = false;     // true while waiting for agent/stream response
let appVersions = [];          // successful generation/growth events with code snapshots
let viewedVersion = 0;         // version currently shown in the iframe
let statusTimer = null;
const callbackCards = new Map(); // run_id -> { el, eventsEl, rawEl, seen }
const finalizedRunIds = new Set(); // run IDs already absorbed into version cards
let lastUserIntent = '';         // tracks the most recent user intent for pending cards
let currentModel = null;         // selected model id (null = server default)
let availableModels = [];        // fetched from /models

// ─── Streaming preview state ────────────────────────────────
// Matches the product's approach: accumulate chunks in a buffer,
// throttle-flush to the iframe with silent=true. No autoClose or
// parent-side Babel gate — the iframe's own Babel decides; failures
// are silenced and the last good frame stays visible.
let streamBuffer = '';
let streamFlushTimer = null;
const STREAM_FLUSH_MS = 150;
let pendingSearchSources = [];   // search results accumulated during a generation cycle
let pendingStages = [];          // stage events accumulated during a generation cycle
let processingCardEl = null;     // live processing card for MCP pull mode

function streamPush(chunk) {
  if (!streamBuffer) {
    // First chunk — ensure iframe is loaded before we try to render into it
    renderer._ensureIframe();
  }
  streamBuffer += chunk;
  if (!streamFlushTimer) {
    streamFlushTimer = setTimeout(streamFlush, STREAM_FLUSH_MS);
  }
}

function streamFlush() {
  streamFlushTimer = null;
  if (!streamBuffer) return;
  // Strip fences and rewrite imports. The iframe's own repairPartialTsx +
  // render scheduler handles partial code repair and last-good-frame fallback.
  // Skip auto-import during streaming — partial code may still be missing
  // its own imports that haven't arrived yet.
  const processed = renderer._processCode(streamBuffer, { skipAutoImport: true });
  if (processed && processed.trim()) {
    renderer._sendToIframe(processed, true);
  }
}

function streamEnd() {
  if (streamFlushTimer) {
    clearTimeout(streamFlushTimer);
    streamFlushTimer = null;
  }
  if (streamBuffer) {
    // Final render with full code — non-silent so errors surface
    renderer.render(streamBuffer);
  }
  streamBuffer = '';
}

function streamReset() {
  if (streamFlushTimer) {
    clearTimeout(streamFlushTimer);
    streamFlushTimer = null;
  }
  streamBuffer = '';
}

// ─── History modal ────────────────────────────────────────────

const historyModal = document.getElementById('history-modal');
document.getElementById('history-btn').addEventListener('click', () => {
  historyModal.classList.remove('hidden');
  loadConversations();
});
document.getElementById('history-close').addEventListener('click', () => {
  historyModal.classList.add('hidden');
});
historyModal.addEventListener('click', (e) => {
  if (e.target === historyModal) historyModal.classList.add('hidden');
});

// ─── Preview tab switching (App / Code) ─────────────────────

const previewCodePanel = document.getElementById('preview-code-panel');

document.querySelectorAll('.preview-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.preview-tab').forEach(t => {
      t.classList.remove('active');
      t.setAttribute('aria-selected', 'false');
    });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    const mode = tab.dataset.ptab;
    if (mode === 'code') {
      iframe.classList.add('hidden');
      placeholder.classList.add('hidden');
      previewCodePanel.classList.remove('hidden');
    } else {
      previewCodePanel.classList.add('hidden');
      // Show iframe or placeholder depending on state
      if (viewedVersion > 0) {
        iframe.classList.remove('hidden');
      } else {
        placeholder.classList.remove('hidden');
      }
    }
  });
});

// ─── Sidebar toggle ─────────────────────────────────────────

const mainEl = document.querySelector('.main');
const sidebarEl = document.querySelector('.sidebar');
const sidebarResizer = document.getElementById('sidebar-resizer');
const sidebarToggle = document.getElementById('sidebar-toggle');
const savedSidebarWidth = Number(localStorage.getItem('sac-sidebar-width') || 0);
if (savedSidebarWidth) {
  sidebarEl.style.flexBasis = `${Math.max(320, Math.min(680, savedSidebarWidth))}px`;
}

sidebarToggle.addEventListener('click', (event) => {
  const collapsed = sidebarEl.classList.toggle('collapsed');
  sidebarResizer.classList.toggle('hidden', collapsed);
  const button = event.currentTarget;
  button.classList.toggle('is-collapsed', collapsed);
  button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  button.setAttribute('aria-label', collapsed ? 'Show activity panel' : 'Hide activity panel');
  button.title = collapsed ? 'Show activity panel' : 'Hide activity panel';
});

function isNarrowMode() {
  return window.matchMedia('(max-width: 900px)').matches;
}

const savedSidebarHeight = Number(localStorage.getItem('sac-sidebar-height') || 0);
if (savedSidebarHeight && isNarrowMode()) {
  sidebarEl.style.flexBasis = `${Math.max(80, Math.min(window.innerHeight * 0.7, savedSidebarHeight))}px`;
}

sidebarResizer.addEventListener('pointerdown', (event) => {
  if (sidebarEl.classList.contains('collapsed')) return;
  event.preventDefault();
  sidebarResizer.setPointerCapture?.(event.pointerId);
  mainEl.classList.add('is-resizing');
  const narrow = isNarrowMode();

  const onMove = (moveEvent) => {
    if (narrow) {
      const mainRect = mainEl.getBoundingClientRect();
      const nextHeight = Math.max(80, Math.min(mainRect.height * 0.7, mainRect.bottom - moveEvent.clientY));
      sidebarEl.style.flexBasis = `${nextHeight}px`;
    } else {
      const nextWidth = Math.max(320, Math.min(680, window.innerWidth - moveEvent.clientX));
      sidebarEl.style.flexBasis = `${nextWidth}px`;
    }
  };
  const onUp = (upEvent) => {
    mainEl.classList.remove('is-resizing');
    sidebarResizer.releasePointerCapture?.(upEvent.pointerId);
    if (narrow) {
      const height = Math.round(sidebarEl.getBoundingClientRect().height);
      localStorage.setItem('sac-sidebar-height', String(height));
    } else {
      const width = Math.round(sidebarEl.getBoundingClientRect().width);
      localStorage.setItem('sac-sidebar-width', String(width));
    }
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
  };

  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
});

// ─── Feedback modal ──────────────────────────────────────────

const feedbackModal = document.getElementById('feedback-modal');
const feedbackOpen = document.getElementById('feedback-open');
const feedbackClose = document.getElementById('feedback-close');

function openFeedbackModal() {
  feedbackModal.classList.remove('hidden');
}

function closeFeedbackModal() {
  feedbackModal.classList.add('hidden');
}

feedbackOpen.addEventListener('click', openFeedbackModal);
feedbackClose.addEventListener('click', closeFeedbackModal);
feedbackModal.addEventListener('click', (event) => {
  if (event.target === feedbackModal) closeFeedbackModal();
});
window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !feedbackModal.classList.contains('hidden')) closeFeedbackModal();
});

// ─── Send handler (unified entry point) ──────────────────────

const sendBtn = document.getElementById('send-btn');
const intentInput = document.getElementById('intent');
const codeDisplay = document.getElementById('code-display');
const codeMeta = document.getElementById('code-meta');
const copyCodeBtn = document.getElementById('copy-code-btn');
let intentInputComposing = false;
sendBtn.addEventListener('click', () => {
  if (pendingAction) {
    // Cancel mode
    hideStatus();
    setPending(false);
    removeProcessingCard();
    addChatMsg('system', 'Cancelled.');
    return;
  }
  handleSend();
});
intentInput.addEventListener('compositionstart', () => {
  intentInputComposing = true;
});
intentInput.addEventListener('compositionend', () => {
  intentInputComposing = false;
  resizeIntentInput();
});
intentInput.addEventListener('keydown', (e) => {
  const composing = intentInputComposing || e.isComposing || e.keyCode === 229;
  if (e.key === 'Enter' && !e.shiftKey && !composing) {
    e.preventDefault();
    handleSend();
  }
});
intentInput.addEventListener('input', resizeIntentInput);
document.querySelectorAll('#example-prompts button').forEach((button) => {
  button.addEventListener('click', () => {
    if (pendingAction) return;
    intentInput.value = button.dataset.prompt || button.textContent || '';
    resizeIntentInput();
    intentInput.focus();
    handleSend();
  });
});
// "Back to Latest" now handled programmatically — no header button
copyCodeBtn.addEventListener('click', async () => {
  const code = codeDisplay.textContent || '';
  if (!code.trim()) return;
  try {
    await navigator.clipboard.writeText(code);
    flashStatus('Code copied', 'success', 1200);
  } catch {
    flashStatus('Copy failed', 'error', 1800);
  }
});

document.getElementById('new-conv-btn').addEventListener('click', () => {
  conversationId = null;
  currentVersion = 0;
  viewedVersion = 0;
  appVersions = [];
  callbackUrl = null;
  if (eventSource) { eventSource.close(); eventSource = null; }
  hidePreviewNotice();
  document.getElementById('chat-area').innerHTML =
    '<div class="chat-msg system"><div class="chat-bubble">New conversation started.</div></div>';
  callbackCards.clear();
  placeholder.classList.remove('hidden');
  iframe.classList.add('hidden');
  resetRenderer();
  hideSuggestions();
  hideStatus();
  codeDisplay.textContent = '';
  codeMeta.textContent = 'No app version selected';
  setChangeCount(0);
  resizeIntentInput();
});

async function handleSend() {
  const message = intentInput.value.trim();
  if (!message || pendingAction) return;

  setPending(true);
  beginNewAttempt();
  intentInput.value = '';
  resizeIntentInput();
  addChatMsg('user', message);
  _knownMsgCount++;

  // Agent mode (callback or MCP pull): forward to /c/{id}/action.
  // callbackUrl is set for callback mode; for MCP pull mode the server
  // queues the action (returns type:"queued") without a callback_url.
  // Both paths: SSE delivers the result when the agent responds via /inbox.
  if (callbackUrl && conversationId) {
    showStatus('Thinking...', 'running');
    try {
      const res = await fetch(`/c/${conversationId}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intent: message }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        addChatMsg('system', `Forwarding failed (HTTP ${res.status}): ${detail}`);
        hideStatus();
        setPending(false);
      } else {
        const data = await res.json().catch(() => ({}));
        if (data.type === 'chat') {
          // Pre-classified as chat — NL reply delivered via SSE
          return;
        }
        showStatus('Sent to agent, waiting...', 'running');
        showAgentWorking();
      }
      // On success (non-chat): don't clear pending — wait for SSE event
    } catch (err) {
      addChatMsg('system', 'Error: ' + err.message);
      hideStatus();
      setPending(false);
    }
    return;
  }

  // Standalone mode (no callback registered): legacy classify + local stream
  showStatus('Thinking...', 'running');
  try {
    const res = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, conversation_id: conversationId, model: currentModel }),
    });
    const data = await res.json();

    if (data.conversation_id && data.conversation_id !== conversationId) {
      conversationId = data.conversation_id;
      setupEventSource(conversationId);
      history.replaceState(null, '', `/c/${conversationId}`);
    }

    if (data.type === 'chat') {
      addChatMsg('assistant', data.reply || '...');
      hideStatus();
      setPending(false);
    } else {
      await streamGenerate({ intent: message, conversation_id: conversationId });
      // streamGenerate clears pending on complete/error
    }
  } catch (err) {
    addChatMsg('system', 'Error: ' + err.message);
    hideStatus();
    setPending(false);
  }
}

// Routes any user-originated intent (button click in App, suggestion click,
// etc.) to the agent (via /c/{id}/action) or the local stream pipeline.
// Always tries /c/{id}/action first — works for callback mode, MCP pull
// mode, and pre-classify chat intercept. Falls back to local stream only
// if the server can't handle it.
async function routeUserIntent(intent, context = null) {
  if (pendingAction) return;  // debounce — wait for current action to finish
  setPending(true);
  beginNewAttempt();
  addChatMsg('user', intent);
  _knownMsgCount++;

  if (conversationId) {
    showStatus('Sent to agent, waiting...', 'running');
    showAgentWorking();
    try {
      const res = await fetch(`/c/${conversationId}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intent, context }),
      });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        if (data.type === 'chat') { removeAgentWorking(); return; }
        // callback / queued: wait for SSE version event
        return;
      }
      // Non-OK: fall through to local stream
    } catch {
      // Network error: fall through to local stream
    }
  }

  // Standalone fallback: no agent, evolve locally
  await streamGenerate({ intent, conversation_id: conversationId });
  // streamGenerate clears pending on complete/error
}

// ─── SSE: subscribe to /c/{id}/events ────────────────────────

function setupEventSource(convId) {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (!convId) return;

  const es = new EventSource(`/c/${convId}/events`);
  eventSource = es;

  es.addEventListener('version', async (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    // Finalize any in-progress stream before applying the version.
    streamReset();
    removeProcessingCard();

    // New App version pushed by an agent — fetch latest code and re-render.
    try {
      const res = await fetch('/conversations/' + convId);
      const { conversation, events = [] } = await res.json();
      if (conversation && conversation.latest_code) {
        appVersions = extractAppVersions(events);
        const latest = appVersions[appVersions.length - 1];
        currentVersion = latest?.version || data.version || currentVersion;
        if (latest) {
          if (pendingSearchSources.length > 0) {
            latest.sources = [...(latest.sources || []), ...pendingSearchSources];
            pendingSearchSources = [];
          }
          if (pendingStages.length > 0) {
            latest.stages = [...(latest.stages || []), ...pendingStages];
            pendingStages = [];
          }

          // Convert any pending callback card into the final version card
          const pendingRuns = finalizePendingCards(data.version);
          ensureVersionCard(latest, pendingRuns);
          applyAppVersion(latest, { announce: true });
          flashStatus(`App updated to v${latest.version}`, 'success');
        } else {
          // conv info removed from header
          codeDisplay.textContent = conversation.latest_code;
          setChangeCount(countChangeMarkers(conversation.latest_code));
          codeMeta.textContent = `Updated to v${currentVersion} · ${formatCodeSize(conversation.latest_code)}`;
          placeholder.classList.add('hidden');
          iframe.classList.remove('hidden');
          renderer.render(conversation.latest_code);
          flashStatus(`App updated to v${currentVersion}`, 'success');
        }
        setPending(false);

        // Refresh suggestions from the latest event
        const last = [...events].reverse().find(
          (ev) => (ev.type === 'generation' || ev.type === 'growth') && ev.status === 'success'
        );
        renderSuggestions(last?.intent_suggestions || []);
      }
    } catch (err) {
      console.warn('Failed to apply version event', err);
    }
  });

  es.addEventListener('chat', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    console.log('[SSE] chat event received:', data);
    if (/^Codex callback failed/i.test(data.content || '')) {
      showStatus(humanizeCallbackError(data.content), 'error');
      setPending(false);
      return;
    }
    removeAgentWorking();
    addChatMsg(data.role || 'assistant', data.content || '');
    _knownMsgCount++;
    hideStatus();
    setPending(false);
  });

  es.addEventListener('callback_run', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    // Agent is actively working — cancel the "no agent" timeout and remove waiting indicator
    if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
    removeAgentWorking();
    renderCallbackRun(data);
    if (data.status === 'queued') {
      showStatus(`Callback queued (${adapterLabel(data.adapter)})`, 'running');
    } else if (data.status === 'running') {
      showStatus(`Callback running (${adapterLabel(data.adapter)})`, 'running');
    } else if (data.status === 'failed') {
      showStatus(`Callback failed (${adapterLabel(data.adapter)})`, 'error');
      setPending(false);
    } else if (data.status === 'no_update') {
      flashStatus(`${adapterLabel(data.adapter)} finished — app unchanged`, 'error');
      setPending(false);
    } else if (data.status === 'succeeded') {
      flashStatus(`Callback completed (${adapterLabel(data.adapter)})`, 'success');
      setPending(false);
    }
  });

  es.addEventListener('callback_log', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    renderCallbackLog(data);
  });

  es.addEventListener('action_timeout', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    removeProcessingCard();
    showStatus(data.message || 'Agent did not respond. Tell your agent to "restart sac mcp with wait_for_action".', 'error');
    setPending(false);
  });

  // ─── Streaming events (from /inbox generation) ──────────────

  es.addEventListener('stage', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    // Agent is actively working — cancel the "no agent" timeout and remove waiting indicator
    if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
    removeAgentWorking();
    showStatus(`${data.name}: ${data.status}`, 'running');
    if ((data.status === 'complete' || data.status === 'success') && data.duration) {
      pendingStages.push({ name: data.name, duration: data.duration });
    }
    // Show processing card in MCP pull mode (no callback cards active)
    if (callbackCards.size === 0) {
      updateProcessingStage(data.name, data.status);
    }
  });

  es.addEventListener('search', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    for (const r of (data.results || [])) {
      for (const src of (r.sources || [])) {
        pendingSearchSources.push({ title: src.title || src.url, url: src.url, query: r.query });
      }
    }
    if (data.results?.length > 0) {
      showStatus(`Searched: ${data.results.map(r => r.query).join(', ')}`, 'running');
      if (callbackCards.size === 0) {
        updateProcessingStage(`search: ${data.results.length} result${data.results.length > 1 ? 's' : ''}`, 'success');
      }
    }
  });

  es.addEventListener('chunk', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    const chunk = data.data;
    if (!chunk) return;

    // First chunk: prepare the UI for streaming
    if (!streamBuffer) {
      placeholder.classList.add('hidden');
      iframe.classList.remove('hidden');
      codeDisplay.textContent = '';
      codeMeta.textContent = 'Streaming generated code...';
      showStatus('Generating...', 'running');
    }
    codeDisplay.textContent += chunk;
    streamPush(chunk);
  });

  es.addEventListener('snapshot', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    const code = data.code;
    if (!code) return;

    // First snapshot: prepare UI
    if (!streamBuffer) {
      placeholder.classList.add('hidden');
      iframe.classList.remove('hidden');
      codeMeta.textContent = 'Applying changes...';
      showStatus('Evolving...', 'running');
    }

    // REPLACE buffer (not append) — snapshot is the full updated code
    streamBuffer = code;
    codeDisplay.textContent = code;
    setChangeCount(countChangeMarkers(code));

    // Flush to iframe immediately with scroll-to-change flag
    if (streamFlushTimer) {
      clearTimeout(streamFlushTimer);
      streamFlushTimer = null;
    }
    const processed = renderer._processCode(code);
    if (processed && processed.trim()) {
      renderer._ensureIframe();
      iframe.contentWindow?.postMessage({
        type: 'render',
        code: processed,
        shimUrl: renderer._designSystem,
        silent: true,
        scrollToChange: true,
      }, '*');
    }
  });

  es.addEventListener('error', (e) => {
    // Server-sent error event (not SSE connection error)
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    streamReset();
    removeProcessingCard();
    showStatus('Error: ' + (data.error || 'unknown'), 'error');
    addChatMsg('system', 'Error: ' + (data.error || 'unknown'));
    setPending(false);
  });

  es.addEventListener('ping', () => {
    // keepalive — ignore
  });

  es.onerror = () => {};  // EventSource auto-reconnects; silence console noise
}

/** Poll for agent response when SSE is unreliable (MCP pull mode). */
async function _pollPendingResult() {
  if (!conversationId || !pendingAction) return;
  try {
    const res = await fetch('/conversations/' + conversationId);
    if (!res.ok) return;
    const { events = [] } = await res.json();
    const msgs = events.filter(e => e.type === 'message');
    // New chat messages? (e.g. send_chat response)
    if (msgs.length > _knownMsgCount) {
      const newMsgs = msgs.slice(_knownMsgCount);
      for (const m of newMsgs) addChatMsg(m.role, m.content);
      _knownMsgCount = msgs.length;
      if (newMsgs.some(m => m.role === 'assistant')) {
        hideStatus();
        setPending(false);
        return;
      }
    }
    // New app version? (e.g. evolve_app response)
    const versions = extractAppVersions(events);
    if (versions.length > appVersions.length) {
      const latest = versions[versions.length - 1];
      appVersions = versions;
      currentVersion = latest?.version || currentVersion;
      if (latest && latest.version > viewedVersion) {
        ensureVersionCard(latest, []);
        applyAppVersion(latest);
        viewedVersion = latest.version;
        flashStatus(`v${latest.version} ready`, 'info');
        removeProcessingCard();
        setPending(false);
      }
    }
  } catch { /* server down — ignore */ }
}

function renderSuggestions(suggestions) {
  const area = document.getElementById('suggestions-area');
  const list = document.getElementById('suggestions-list');
  if (!suggestions || suggestions.length === 0) {
    area.classList.add('hidden');
    return;
  }
  area.classList.remove('hidden');
  const label = document.querySelector('.suggestions-label');
  if (label) label.textContent = 'Next actions';
  list.innerHTML = suggestions.map(s =>
    `<button class="suggestion-btn" data-prompt="${escHtml(s.prompt)}" title="${escHtml(s.label)}">${escHtml(s.label)}</button>`
  ).join('');
  list.querySelectorAll('.suggestion-btn').forEach(b => {
    b.addEventListener('click', () => {
      if (pendingAction) return;
      intentInput.value = b.dataset.prompt;
      resizeIntentInput();
      intentInput.focus();
    });
  });
}

// ─── Streaming generation (POST-based SSE) ───────────────────

async function streamGenerate(body) {
  beginNewAttempt();
  showStatus('Generating...', 'running');

  codeDisplay.textContent = '';
  codeDisplay.style.color = '';
  codeMeta.textContent = 'Streaming generated code...';
  setChangeCount(0);

  placeholder.classList.add('hidden');
  iframe.classList.remove('hidden');

  streamReset();

  if (currentModel) body.model = currentModel;

  try {
    const response = await fetch('/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let currentEvent = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ') && currentEvent) {
          try {
            const data = JSON.parse(line.slice(6));
            handleSSEEvent(currentEvent, data);
          } catch {}
          currentEvent = null;
        }
      }
    }
  } catch (err) {
    showStatus('Connection error: ' + err.message, 'error');
    streamReset();
    setPending(false);
  }
}

function handleSSEEvent(eventType, data) {
  switch (eventType) {
    case 'stage':
      showStatus(`${data.name}: ${data.status}`, 'running');
      if (data.status === 'complete' || data.status === 'success') {
        pendingStages.push({ name: data.name, duration: data.duration });
      }
      if (callbackCards.size === 0) {
        updateProcessingStage(data.name, data.status);
      }
      break;

    case 'search': {
      const results = data.results || [];
      for (const r of results) {
        for (const src of (r.sources || [])) {
          pendingSearchSources.push({ title: src.title || src.url, url: src.url, query: r.query });
        }
      }
      if (results.length > 0) {
        showStatus(`Searched: ${results.map(r => r.query).join(', ')}`, 'running');
        if (callbackCards.size === 0) {
          updateProcessingStage(`search: ${results.length} result${results.length > 1 ? 's' : ''}`, 'success');
        }
      }
      break;
    }

    case 'snapshot': {
      // Progressive evolve: full code snapshot replaces buffer
      streamBuffer = data.code;
      codeDisplay.textContent = data.code;
      setChangeCount(countChangeMarkers(data.code));
      if (streamFlushTimer) { clearTimeout(streamFlushTimer); streamFlushTimer = null; }
      const proc = renderer._processCode(data.code);
      if (proc && proc.trim()) {
        renderer._ensureIframe();
        iframe.contentWindow?.postMessage({
          type: 'render',
          code: proc,
          shimUrl: renderer._designSystem,
          silent: true,
          scrollToChange: true,
        }, '*');
      }
      break;
    }

    case 'chunk': {
      let chunk = data.data;
      // Display raw in code panel
      codeDisplay.textContent += chunk;
      // Accumulate in stream buffer — throttled flush to iframe
      streamPush(chunk);
      break;
    }

    case 'complete': {
      // Final render with the authoritative code from the complete event
      streamReset();
      removeProcessingCard();
      const app = data.app;
      renderer.render(app.code);

      if (data.conversation_id) conversationId = data.conversation_id;
      currentVersion = app.version;
      const stagesData = (app.stages || []).length > 0 ? app.stages : pendingStages;
      const version = {
        version: app.version,
        title: app.intent || `Version ${app.version}`,
        code: app.code,
        kind: app.parent_version ? 'evolved' : 'generated',
        createdAt: app.created_at,
        sources: [...pendingSearchSources],
        stages: stagesData,
      };
      pendingSearchSources = [];
      pendingStages = [];
      upsertAppVersion(version);

      // Update header
      applyAppVersion(version);

      // Show completion in chat
      ensureVersionCard(version);

      // Show suggestions
      renderSuggestions(app.suggestions || []);

      const stagesSummary = (stagesData || []).map(s =>
        `${s.name}: ${s.duration ? s.duration.toFixed(1) + 's' : s.status}`
      ).join(' → ');
      flashStatus(stagesSummary || `App updated to v${version.version}`, 'success');
      setPending(false);
      break;
    }

    case 'error':
      showStatus('Error: ' + data.error, 'error');
      addChatMsg('system', 'Error: ' + data.error);
      streamReset();
      setPending(false);
      break;
  }
}

// ─── Chat UI helpers ─────────────────────────────────────────

function addChatMsg(role, content) {
  if (role !== 'user') removeAgentWorking();
  if (role === 'user') lastUserIntent = content;
  const area = document.getElementById('chat-area');
  const msg = document.createElement('div');
  msg.className = `chat-msg ${role}`;
  msg.innerHTML = `<div class="chat-bubble">${escHtml(content)}</div>`;
  area.appendChild(msg);
  scrollChatToBottom();
}

// ─── Agent working indicator ─────────────────────────────
// Lightweight "waiting" message shown after action dispatch, before pipeline starts.
function showAgentWorking() {
  removeAgentWorking();
  const area = document.getElementById('chat-area');
  const el = document.createElement('div');
  el.className = 'chat-msg system agent-working-indicator';
  el.id = 'agent-working';
  el.innerHTML = '<div class="chat-bubble">Agent working...</div>';
  area.appendChild(el);
  scrollChatToBottom();
}
function removeAgentWorking() {
  const el = document.getElementById('agent-working');
  if (el) el.remove();
}

// ─── Processing card (MCP pull mode) ─────────────────────
// Shows pipeline stages in the chat timeline when no callback card exists.

function ensureProcessingCard() {
  if (processingCardEl) return processingCardEl;
  const area = document.getElementById('chat-area');
  const el = document.createElement('div');
  el.className = 'version-card pending processing-card';
  el.id = 'mcp-processing-card';
  const title = lastUserIntent || 'Processing...';
  el.innerHTML = `
    <div class="vc-header">
      <span class="version-card-title" title="${escHtml(title)}">${escHtml(compactText(title, 140))}</span>
      <span class="version-card-kind processing-kind"><span class="processing-kind-dot"></span>SaC</span>
    </div>
    <div class="processing-stages"></div>`;
  area.appendChild(el);
  processingCardEl = el;
  scrollChatToBottom();
  return el;
}

function updateProcessingStage(name, status) {
  const card = ensureProcessingCard();
  const stagesEl = card.querySelector('.processing-stages');
  let stageEl = stagesEl.querySelector(`[data-stage="${CSS.escape(name)}"]`);
  if (!stageEl) {
    stageEl = document.createElement('div');
    stageEl.className = 'processing-stage-item';
    stageEl.dataset.stage = name;
    stagesEl.appendChild(stageEl);
  }
  const cls = status === 'running' ? 'stage-running' : (status === 'completed' || status === 'complete' || status === 'success') ? 'stage-done' : status === 'error' ? 'stage-error' : '';
  stageEl.className = `processing-stage-item ${cls}`;
  const iconClass = status === 'running' ? 'running' : (status === 'completed' || status === 'complete' || status === 'success') ? 'done' : status === 'error' ? 'error' : 'idle';
  const iconLabel = iconClass === 'done' ? '✓' : iconClass === 'error' ? '!' : '';
  stageEl.innerHTML = `<span class="processing-stage-icon ${iconClass}">${iconLabel}</span><span>${escHtml(name)}</span>`;
  // Update card header kind
  const kindEl = card.querySelector('.version-card-kind');
  if (kindEl) {
    kindEl.innerHTML = `<span class="processing-kind-dot"></span>SaC · ${escHtml(name)}`;
  }
  scrollChatToBottom();
}

function removeProcessingCard() {
  if (processingCardEl) {
    processingCardEl.remove();
    processingCardEl = null;
  }
}

function extractAppVersions(events) {
  const versions = [];
  let pendingSources = [];
  for (const event of events || []) {
    if (event.type === 'search' && event.status === 'success') {
      for (const r of (event.results || [])) {
        for (const src of (r.sources || [])) {
          pendingSources.push({ title: src.title || src.url, url: src.url });
        }
      }
    }
    if ((event.type === 'generation' || event.type === 'growth') && event.status === 'success' && event.code) {
      versions.push({
        version: versions.length + 1,
        title: event.intent || `Version ${versions.length + 1}`,
        code: event.code,
        kind: event.type === 'generation' ? 'generated' : 'evolved',
        createdAt: event.timestamp,
        suggestions: event.intent_suggestions || [],
        sources: [...pendingSources],
        stages: event.stages || [],
      });
      pendingSources = [];
    }
  }
  return versions;
}

function upsertAppVersion(version) {
  const index = appVersions.findIndex(v => v.version === version.version);
  if (index >= 0) {
    appVersions[index] = version;
  } else {
    appVersions.push(version);
  }
}

function getLatestVersion() {
  return appVersions[appVersions.length - 1] || null;
}

function applyAppVersion(version, opts = {}) {
  if (!version || !version.code) return;
  viewedVersion = version.version;
  codeDisplay.textContent = version.code;
  codeMeta.textContent = `v${version.version} · ${formatCodeSize(version.code)}`;
  setChangeCount(countChangeMarkers(version.code));
  // Ensure App tab is active in preview
  previewCodePanel.classList.add('hidden');
  placeholder.classList.add('hidden');
  iframe.classList.remove('hidden');
  document.querySelectorAll('.preview-tab').forEach(t => {
    const isApp = t.dataset.ptab === 'app';
    t.classList.toggle('active', isApp);
    t.setAttribute('aria-selected', isApp ? 'true' : 'false');
  });
  hidePreviewNotice();
  renderer.render(version.code);
  markActiveVersionCard(version.version);
  if (opts.announce) {
    flashVersionCard(version.version);
  }
}

function ensureVersionCard(version, callbackRuns) {
  const area = document.getElementById('chat-area');
  const id = `version-card-${version.version}`;
  let wrapper = document.getElementById(id);
  if (!wrapper) {
    wrapper = document.createElement('div');
    wrapper.id = id;
    wrapper.className = 'version-card';
    // If there's an absorb reference (from a merged callback card), insert there
    const absorbRef = area.querySelector('[data-absorb-ref]');
    if (absorbRef) {
      area.insertBefore(wrapper, absorbRef);
      absorbRef.remove();
    } else {
      area.appendChild(wrapper);
    }
  }
  const kindLabel = version.kind === 'generated' ? 'generated' : 'evolved';
  const timeStr = version.createdAt ? formatTime(version.createdAt) : '';
  const sources = version.sources || [];
  const stages = version.stages || [];
  const runs = callbackRuns || [];

  // Build collapsible detail sections
  let detailsHtml = '';
  const sections = [];

  // Agent activity section (from callback runs)
  if (runs.length > 0) {
    const agentEvents = [];
    for (const run of runs) {
      for (const log of (run.logs || []).slice(-10)) {
        const kind = log.kind || 'log';
        const detail = log.detail || '';
        // Skip noise
        if (kind === 'raw') continue;
        if (/ignoring interface\.|stream disconnected|manifest|loader/.test(detail)) continue;
        // Skip transient/post-publish noise
        if (kind === 'turn_started' || kind === 'turn_completed') continue;
        if (kind === 'command_completed' && detail.includes('/inbox')) continue;
        // Skip agent messages that are just URL confirmations
        if (kind === 'agent_message' && detail.includes('Updated the SaC app')) continue;

        // Two-role rendering: sac vs agent
        let role, text;
        if (kind === 'thread_started') {
          role = 'sac';
          text = `Started agent thread ${detail ? detail.slice(0, 12) + '...' : ''}`;
        } else if (kind === 'agent_message') {
          role = 'agent';
          text = compactText(detail, 200);
        } else if (kind === 'command_started') {
          const fmt = formatCommand(detail);
          if (fmt.includes('/inbox')) continue; // skip — replaced by "sac: App updated"
          role = 'agent-sub';
          text = fmt;
        } else if (kind === 'app_updated') {
          role = 'sac';
          text = 'App updated';
        } else if (kind === 'warning') {
          role = 'sac';
          text = `Warning: ${compactText(detail, 200)}`;
        } else {
          role = 'agent-sub';
          text = compactText(detail || log.label || kind, 200);
        }

        const roleLabel = role === 'agent-sub' ? '' : (role === 'sac' ? 'SaC' : 'Agent');
        const isSub = role === 'agent-sub';
        agentEvents.push(`<div class="cb-step cb-step-${escClass(role)}"><span class="cb-step-role">${roleLabel}</span><span class="cb-step-text${isSub ? ' cb-step-sub' : ''}" title="${escHtml(detail || text)}">${escHtml(text)}</span></div>`);
      }
      // Fire-and-forget adapters (OpenClaw, HTTP) have no streaming logs.
      // Synthesize the same steps shown during live rendering.
      if (run.status === 'succeeded' && run.loop_closed && (run.logs || []).length === 0) {
        agentEvents.push(`<div class="cb-step cb-step-agent-sub"><span class="cb-step-role"></span><span class="cb-step-text cb-step-sub" title="Dispatched to agent">Dispatched to agent</span></div>`);
        agentEvents.push(`<div class="cb-step cb-step-agent-sub"><span class="cb-step-role"></span><span class="cb-step-text cb-step-sub" title="Message delivered, agent is working...">Message delivered, agent is working...</span></div>`);
      }
      // Ensure "App updated" appears as the final step (it's added client-side
      // by finalizePendingCards but not persisted to the backend).
      if (run.status === 'succeeded' && run.loop_closed) {
        const lastEvt = agentEvents[agentEvents.length - 1] || '';
        if (!lastEvt.includes('App updated')) {
          agentEvents.push(`<div class="cb-step cb-step-sac"><span class="cb-step-role">SaC</span><span class="cb-step-text" title="App updated">App updated</span></div>`);
        }
      }
    }
    if (agentEvents.length > 0) {
      const adapter = runs[0].adapter ? adapterLabel(runs[0].adapter) : 'Agent';
      sections.push(`
        <div class="vc-detail-section">
          <div class="vc-detail-toggle" data-section="agent">
            <span class="vc-detail-arrow">›</span>
            ${escHtml(adapter)} (${agentEvents.length} events)
          </div>
          <div class="vc-detail-body" data-section-body="agent">
            ${agentEvents.join('')}
          </div>
        </div>`);
    }
  }

  if (sources.length > 0) {
    sections.push(`
      <div class="vc-detail-section">
        <div class="vc-detail-toggle" data-section="sources">
          <span class="vc-detail-arrow">›</span>
          Sources (${sources.length})
        </div>
        <div class="vc-detail-body" data-section-body="sources">
          ${sources.slice(0, 10).map(s => `<div class="vc-source-item">${s.url ? `<a href="${escHtml(s.url)}" target="_blank" rel="noopener">${escHtml(compactText(s.title || s.url, 60))}</a>` : escHtml(compactText(s.title, 60))}</div>`).join('')}
          ${sources.length > 10 ? `<div class="vc-source-item vc-source-more">+${sources.length - 10} more</div>` : ''}
        </div>
      </div>`);
  }

  if (stages.length > 0) {
    sections.push(`
      <div class="vc-detail-section">
        <div class="vc-detail-toggle" data-section="stages">
          <span class="vc-detail-arrow">›</span>
          Pipeline (${stages.length} stages)
        </div>
        <div class="vc-detail-body" data-section-body="stages">
          ${stages.map(s => `<div class="vc-stage-item"><span class="vc-stage-name">${escHtml(s.name)}</span>${s.duration ? `<span class="vc-stage-dur">${s.duration.toFixed(1)}s</span>` : ''}</div>`).join('')}
        </div>
      </div>`);
  }

  if (sections.length > 0) detailsHtml = `<div class="vc-details">${sections.join('')}</div>`;

  // Status indicator
  const statusDot = runs.length > 0
    ? (runs.every(r => r.status === 'succeeded') ? 'success' : runs.some(r => r.status === 'failed') ? 'failed' : 'success')
    : 'success';

  wrapper.innerHTML = `
    <div class="vc-header vc-header-version">
      <span class="vc-dot vc-dot-${statusDot}"></span>
      <span class="version-card-badge">v${version.version}</span>
      <span class="version-card-kind">${escHtml(kindLabel)}${timeStr ? ' · ' + escHtml(timeStr) : ''}</span>
    </div>
    ${detailsHtml}
  `;

  // Click header to switch version
  wrapper.querySelector('.vc-header').addEventListener('click', () => applyAppVersion(version));

  // Toggle detail sections
  wrapper.querySelectorAll('.vc-detail-toggle').forEach(toggle => {
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const section = toggle.dataset.section;
      const body = wrapper.querySelector(`[data-section-body="${section}"]`);
      const arrow = toggle.querySelector('.vc-detail-arrow');
      if (body.classList.contains('expanded')) {
        body.classList.remove('expanded');
        arrow.textContent = '›';
      } else {
        body.classList.add('expanded');
        arrow.textContent = '⌄';
      }
    });
  });

  markActiveVersionCard(viewedVersion);
  scrollChatToBottom();
}

function markActiveVersionCard(versionNumber) {
  document.querySelectorAll('.version-card').forEach(card => {
    card.classList.toggle('active', card.id === `version-card-${versionNumber}`);
  });
}

function flashVersionCard(versionNumber) {
  const card = document.getElementById(`version-card-${versionNumber}`);
  if (!card) return;
  card.classList.remove('just-updated');
  void card.offsetWidth;
  card.classList.add('just-updated');
  window.setTimeout(() => card.classList.remove('just-updated'), 1500);
}

function renderCallbackRun(run) {
  const status = run.status || 'unknown';

  // If this run was already finalized into a version card, skip completely
  if (finalizedRunIds.has(run.id)) return;

  // If this run already has a version card, skip completely
  if (run.result_version && document.getElementById(`version-card-${run.result_version}`)) {
    if (callbackCards.has(run.id)) {
      callbackCards.get(run.id).el.remove();
      callbackCards.delete(run.id);
    }
    return;
  }

  // If this run already has a pending card that was converted to a version card, skip
  if (!document.getElementById(`pending-card-${run.id}`) && callbackCards.has(run.id)) {
    // Card was already converted — the el was replaced by ensureVersionCard
    if (status === 'succeeded' || status === 'failed' || status === 'no_update') {
      callbackCards.delete(run.id);
    }
    return;
  }

  const card = ensureCallbackCard(run);
  const hasStreamingLogs = run.adapter === 'codex_exec_resume';

  // Update pending card status display
  const kindEl = card.el.querySelector('.version-card-kind');
  const dotEl = card.el.querySelector('.vc-dot');
  if (status === 'running' || status === 'queued') {
    if (kindEl) kindEl.textContent = `${adapterLabel(run.adapter)} · ${status === 'running' ? 'running' : 'queued'}`;
    if (dotEl) { dotEl.className = 'vc-dot vc-dot-pending'; }
    card.el.className = 'version-card pending';
    // Non-streaming adapters (OpenClaw, HTTP) don't emit callback_log events,
    // so add a generic waiting indicator.
    if (!hasStreamingLogs && status === 'running' && card.eventsEl.children.length === 0) {
      addCallbackEvent(card, 'sac', 'Dispatched to agent');
      addCallbackEvent(card, 'agent-sub', 'waiting for response...');
    }
  } else if (status === 'succeeded') {
    if (kindEl) kindEl.textContent = `${adapterLabel(run.adapter)} · success`;
    if (dotEl) { dotEl.className = 'vc-dot vc-dot-success'; }
    // For fire-and-forget adapters (OpenClaw, HTTP), "succeeded" means the message
    // was delivered — agent is now working. Keep card pending until /inbox POST arrives.
    if (!hasStreamingLogs && !run.loop_closed) {
      card.el.className = 'version-card pending';
      if (dotEl) { dotEl.className = 'vc-dot vc-dot-pending'; }
      if (kindEl) kindEl.textContent = `${adapterLabel(run.adapter)} · running`;
      addCallbackEvent(card, 'sac', 'Message delivered, agent is working...');
    } else {
      card.el.classList.remove('pending');
    }
  } else if (status === 'failed' || status === 'no_update') {
    if (kindEl) kindEl.textContent = `${adapterLabel(run.adapter)} · ${status}`;
    if (dotEl) { dotEl.className = 'vc-dot vc-dot-failed'; }
    card.el.classList.remove('pending');
  }

  // Replay persisted logs
  if (Array.isArray(run.logs)) {
    for (const log of run.logs.slice(-12)) renderCallbackLog(log);
  }

  // Add error events
  if (run.error) {
    addCallbackEvent(card, 'sac', `Error: ${humanizeCallbackError(run.error)}`);
  }

  // Clean up transient states on terminal
  if (status === 'succeeded' || status === 'failed' || status === 'no_update') {
    card.eventsEl.querySelectorAll('.cb-step-transient').forEach(el => el.remove());
  }
}

function renderCallbackLog(log) {
  if (!log.run_id) return;

  // If this run was already finalized into a version card, ignore late events
  if (finalizedRunIds.has(log.run_id)) return;

  const card = ensureCallbackCard({ id: log.run_id, adapter: 'codex_exec_resume', status: 'running' });

  const kind = log.kind || 'log';
  const label = log.label || `${log.stream || 'callback'} log`;
  const detail = log.detail || '';

  // Dedup
  const eventKey = `${log.timestamp || ''}:${kind}:${label}:${detail}`;
  if (card.seen.has(eventKey)) return;
  card.seen.add(eventKey);

  // Skip pure noise (stderr spam, loader warnings)
  if (/ignoring interface\.|stream disconnected|manifest|loader/.test(detail)) return;
  if (kind === 'raw') return;

  // Once SaC has published, suppress post-publish noise (agent confirmation,
  // duplicate command_completed, turn_completed). These just echo what SaC
  // already reported via the version event.
  if (card.published && kind !== 'version_updated' && kind !== 'warning') {
    if (log.line) appendCallbackRaw(card, label, log.line);
    return;
  }

  // Route to two-role display: sac (orchestrator) vs agent (external)
  switch (kind) {
    case 'thread_started':
      addCallbackEvent(card, 'sac', `Started agent thread ${detail ? detail.slice(0, 12) + '...' : ''}`);
      break;
    case 'agent_message':
      addCallbackEvent(card, 'agent', detail);
      break;
    case 'turn_started':
      addCallbackEvent(card, 'agent-sub', 'thinking...');
      break;
    case 'turn_completed':
      // transient — turn_started already removed, nothing to show
      break;
    case 'command_started': {
      const fmt = formatCommand(detail);
      if (fmt.includes('/inbox')) {
        // /inbox POST started — SaC is about to receive content and run evolve.
        // Show "Updating app..." now (not on command_completed, which arrives AFTER
        // the version event due to HTTP response timing).
        card.published = true;
        addCallbackEvent(card, 'sac', 'Updating app...');
      } else {
        addCallbackEvent(card, 'agent-sub', fmt);
      }
      break;
    }
    case 'command_completed': {
      // For /inbox: already handled by command_started. Just capture raw output.
      if (log.line) appendCallbackRaw(card, label, log.line);
      break;
    }
    case 'version_updated':
      card.published = true;
      break;
    case 'warning':
      addCallbackEvent(card, 'sac', `Warning: ${humanizeCallbackError(detail || label)}`);
      if (log.line) appendCallbackRaw(card, label, log.line);
      break;
    default:
      if (label === 'stderr log') {
        if (log.line) appendCallbackRaw(card, label, log.line);
      } else if (log.raw_visible && log.line) {
        appendCallbackRaw(card, label, log.line);
      }
  }
  scrollChatToBottom();
}

function addCallbackEvent(card, role, text) {
  // role: 'sac' | 'agent' | 'agent-sub'
  // 'agent-sub' = indented sub-step under agent (thinking, commands)

  // Remove transient states when new events arrive
  // "thinking..." (agent-sub transient) — remove when next non-sub event arrives
  if (role !== 'agent-sub') {
    card.eventsEl.querySelectorAll('.cb-step-transient').forEach(el => el.remove());
  }

  const el = document.createElement('div');
  const isTransient = (role === 'agent-sub' && /thinking/i.test(text))
                   || (role === 'sac' && /updating app/i.test(text));
  el.className = `cb-step cb-step-${escClass(role)}${isTransient ? ' cb-step-transient' : ''}`;

  if (role === 'agent-sub') {
    // Indented sub-step — no role label
    el.innerHTML = `<span class="cb-step-role"></span><span class="cb-step-text cb-step-sub" title="${escHtml(text)}">${escHtml(compactText(text, 240))}</span>`;
  } else {
    const roleLabel = role === 'sac' ? 'SaC' : 'Agent';
    el.innerHTML = `<span class="cb-step-role">${roleLabel}</span><span class="cb-step-text" title="${escHtml(text)}">${escHtml(compactText(text, 420))}</span>`;
  }
  card.eventsEl.appendChild(el);

  // Keep last 12 visible (exclude transient from count)
  const steps = card.eventsEl.querySelectorAll('.cb-step:not(.cb-step-transient)');
  while (steps.length > 12) card.eventsEl.removeChild(card.eventsEl.querySelector('.cb-step:not(.cb-step-transient)'));
}

function formatCommand(text) {
  const cmd = String(text || '');
  if (cmd.includes('/inbox')) return 'POST /inbox';
  if (cmd.includes('curl')) return cmd.replace(/^.*?(curl\s)/, '$1').slice(0, 120);
  return compactText(cmd, 120);
}

function ensureCallbackCard(run, afterEl) {
  let card = callbackCards.get(run.id);
  if (card) return card;

  const area = document.getElementById('chat-area');
  const adapter = adapterLabel(run.adapter);
  const title = lastUserIntent || 'Processing...';
  const status = run.status || 'pending';
  const el = document.createElement('div');
  el.className = 'version-card pending';
  el.id = `pending-card-${run.id}`;
  el.dataset.runId = run.id;
  el.innerHTML = `
    <div class="vc-header">
      <span class="vc-dot vc-dot-pending"></span>
      <span class="version-card-title" title="${escHtml(title)}">${escHtml(compactText(title, 140))}</span>
      <span class="version-card-kind">${escHtml(adapter)} · ${escHtml(runStatusLabel(status).toLowerCase())}</span>
    </div>
    <div class="vc-details">
      <div class="vc-detail-section">
        <div class="vc-detail-toggle vc-steps-toggle" data-section="steps">
          <span class="vc-detail-arrow">⌄</span>
          Steps
        </div>
        <div class="vc-detail-body expanded" data-section-body="steps">
        </div>
      </div>
    </div>`;
  // Toggle steps section
  el.querySelector('.vc-steps-toggle').addEventListener('click', (e) => {
    e.stopPropagation();
    const body = el.querySelector('[data-section-body="steps"]');
    const arrow = el.querySelector('.vc-detail-arrow');
    if (body.classList.contains('expanded')) {
      body.classList.remove('expanded');
      arrow.textContent = '›';
    } else {
      body.classList.add('expanded');
      arrow.textContent = '⌄';
    }
  });
  // Insert after a specific element if provided, otherwise append
  if (afterEl && afterEl.nextSibling) {
    area.insertBefore(el, afterEl.nextSibling);
  } else {
    area.appendChild(el);
  }
  card = { el, eventsEl: el.querySelector('[data-section-body="steps"]'), rawEl: null, rawCount: 0, seen: new Set(), published: false };
  callbackCards.set(run.id, card);
  scrollChatToBottom();
  return card;
}

function appendCallbackRaw(card, label, raw) {
  if (!card || !raw) return;
  if (!card.rawEl) {
    card.rawEl = document.createElement('details');
    card.rawEl.className = 'cb-raw';
    card.rawEl.innerHTML = '<summary>Raw output</summary><pre></pre>';
    card.el.appendChild(card.rawEl);
  }
  card.rawCount += 1;
  card.rawEl.querySelector('summary').textContent = `Raw output (${card.rawCount})`;
  const pre = card.rawEl.querySelector('pre');
  pre.textContent = `${pre.textContent}${pre.textContent ? '\n' : ''}${String(raw).slice(0, 2000)}`.slice(-6000);
}

// Convert live pending cards into data for ensureVersionCard.
// The pending card's DOM element is reused as the version card container.
function finalizePendingCards(version) {
  const runs = [];
  for (const [runId, card] of callbackCards) {
    if (!card.el.classList.contains('pending') && !card.el.id.startsWith('pending-card-')) continue;

    // Remove transient steps and add final "App updated" before extracting
    card.eventsEl.querySelectorAll('.cb-step-transient').forEach(el => el.remove());
    addCallbackEvent(card, 'sac', 'App updated');

    // Extract logs from the card's step events for version card rebuild
    const logs = [];
    card.eventsEl.querySelectorAll('.cb-step').forEach(ev => {
      const roleEl = ev.querySelector('.cb-step-role');
      const textEl = ev.querySelector('.cb-step-text');
      const role = roleEl?.textContent || '';
      const text = textEl?.textContent || '';
      // Map back to callback log kinds for version card rendering
      let kind = 'log';
      if ((role === 'sac' || role === 'SaC') && text.startsWith('Started agent')) kind = 'thread_started';
      else if ((role === 'sac' || role === 'SaC') && text === 'App updated') kind = 'app_updated';
      else if (role === 'agent' || role === 'Agent') kind = 'agent_message';
      else if (role === '' && ev.classList.contains('cb-step-agent-sub')) kind = 'command_started';
      logs.push({ kind, label: role, detail: text });
    });

    const adapterText = card.el.querySelector('.version-card-kind')?.textContent || '';
    const adapterKey = adapterText.includes('Codex') ? 'codex_exec_resume' :
                       adapterText.includes('OpenClaw') ? 'openclaw_gateway' : 'default';
    runs.push({ id: runId, adapter: adapterKey, status: 'succeeded', result_version: version, logs });

    // Remove the pending card — ensureVersionCard will create the final one in its place
    const ref = document.createElement('div');
    ref.className = 'hidden';
    ref.dataset.absorbRef = runId;
    card.el.parentNode.insertBefore(ref, card.el);
    card.el.remove();
    callbackCards.delete(runId);
    finalizedRunIds.add(runId);
  }
  return runs.length > 0 ? runs : null;
}

// ─── Conversations tab ───────────────────────────────────────

window.loadConversations = async function() {
  const container = document.getElementById('conv-list');
  container.innerHTML = '<div class="empty-list">Loading conversations...</div>';
  try {
    const res = await fetch('/conversations');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const convs = data.conversations || [];

    if (convs.length === 0) {
      container.innerHTML = '<div class="empty-list">No conversations yet. Start from the Activity tab.</div>';
      return;
    }

    container.innerHTML = convs.map(c => `
      <div class="conv-card ${c.id === conversationId ? 'active' : ''}" onclick="loadConv('${c.id}')">
        <div class="conv-card-header">
          <span class="conv-card-title">${escHtml(c.title || 'Untitled')}</span>
          <span class="conv-card-meta">${c.event_count} events</span>
        </div>
        <div class="conv-card-meta">
          ${c.id.substring(0, 8)}... | ${new Date(c.updated_at).toLocaleString()}
        </div>
        <div class="conv-card-actions" onclick="event.stopPropagation()">
          <input class="input-sm" id="rename-${c.id}" placeholder="Rename" onclick="event.stopPropagation()">
          <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); renameConv('${c.id}')">Rename</button>
          <button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); deleteConv('${c.id}')">Delete</button>
        </div>
      </div>
    `).join('');
  } catch (err) {
    container.innerHTML = `<div class="empty-list">Could not load conversations: ${escHtml(err.message)}</div>`;
  }
};

window.loadConv = async function(id) {
  try {
    const res = await fetch('/conversations/' + id);
    const data = await res.json();
    const conv = data.conversation;
    const events = data.events || [];

    conversationId = id;
    history.replaceState(null, '', `/c/${id}`);
    currentVersion = conv.event_count;
    // Agent-owned conversations route through /c/{id}/action (callback or MCP pull).
    // Product-mode conversations (no agent) use /send → StandaloneAgent.
    callbackUrl = conv.callback_url || (conv.source === 'inbox' ? '__mcp_pull__' : null);
    // conv info display removed from header
    appVersions = extractAppVersions(events);
    viewedVersion = appVersions[appVersions.length - 1]?.version || 0;

    // Pre-fetch callback runs so we can interleave them with version cards
    let allRuns = [];
    try {
      const runsRes = await fetch(`/c/${id}/callback-runs`);
      if (runsRes.ok) {
        const runsData = await runsRes.json();
        allRuns = runsData.runs || [];
      }
    } catch {}

    // Build version → runs lookup (a version may have multiple runs, use result_version)
    const runsByVersion = new Map();
    for (const run of allRuns) {
      if (run.result_version) {
        if (!runsByVersion.has(run.result_version)) runsByVersion.set(run.result_version, []);
        runsByVersion.get(run.result_version).push(run);
      }
    }
    // Runs without result_version go at the end
    const orphanRuns = allRuns.filter(r => !r.result_version);

    // Rebuild chat from events, interleaving callback cards after version cards
    const chatArea = document.getElementById('chat-area');
    chatArea.innerHTML = '';
    callbackCards.clear();

    let lastSuggestions = [];
    _knownMsgCount = events.filter(e => e.type === 'message').length;
    for (const evt of events) {
      if (evt.type === 'message') {
        addChatMsg(evt.role, evt.content);
      } else if ((evt.type === 'generation' || evt.type === 'growth') && evt.status === 'success') {
        const version = appVersions.find(v => v.code === evt.code);
        if (version) {
          // Pass associated callback runs into the version card (merged view)
          const runs = runsByVersion.get(version.version) || [];
          ensureVersionCard(version, runs);
        }
        if (evt.intent_suggestions?.length) lastSuggestions = evt.intent_suggestions;
      }
    }

    // Render orphan runs — only the most recent one that's still genuinely active.
    // Stale orphans (agent got the message but never replied) just clutter the UI.
    const activeOrphan = orphanRuns.length > 0 ? orphanRuns[orphanRuns.length - 1] : null;
    if (activeOrphan && activeOrphan.status !== 'failed' && activeOrphan.status !== 'no_update') {
      lastUserIntent = activeOrphan.intent || lastUserIntent || 'Processing...';
      renderCallbackRun(activeOrphan);
    }

    // Show suggestions from the last generation/growth event
    renderSuggestions(lastSuggestions);

    // Render latest code if available
    const latestVersion = appVersions[appVersions.length - 1];
    if (latestVersion) {
      applyAppVersion(latestVersion);
    } else if (conv.latest_code) {
      const fallbackVersion = {
        version: currentVersion || 1,
        title: conv.latest_intent || conv.title || 'Latest version',
        code: conv.latest_code,
        kind: 'latest',
      };
      applyAppVersion(fallbackVersion);
    }

    // Subscribe to live updates AFTER rebuild is complete to avoid SSE race
    setupEventSource(id);

    document.getElementById('history-modal').classList.add('hidden');
    chatArea.scrollTop = chatArea.scrollHeight;
  } catch (err) {
    alert('Error loading conversation: ' + err.message);
  }
};

window.renameConv = async function(id) {
  const input = document.getElementById('rename-' + id);
  const title = input.value.trim();
  if (!title) return;
  try {
    await fetch('/conversations/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    input.value = '';
    loadConversations();
  } catch (err) {
    alert('Error: ' + err.message);
  }
};

window.deleteConv = async function(id) {
  if (!confirm('Delete this conversation?')) return;
  try {
    await fetch('/conversations/' + id, { method: 'DELETE' });
    if (conversationId === id) {
      conversationId = null;
      currentVersion = 0;
      viewedVersion = 0;
      appVersions = [];
      hidePreviewNotice();
      document.getElementById('chat-area').innerHTML =
        '<div class="chat-msg system"><div class="chat-bubble">Conversation deleted.</div></div>';
      placeholder.classList.remove('hidden');
      iframe.classList.add('hidden');
      codeDisplay.textContent = '';
      codeMeta.textContent = 'No app version selected';
      setChangeCount(0);
    }
    loadConversations();
  } catch (err) {
    alert('Error: ' + err.message);
  }
};

// ─── Helpers ──────────────────────────────────────────────────

function showStatus(text, kind = 'info') {
  const bar = document.getElementById('status-bar');
  if (statusTimer) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
  bar.classList.remove('hidden');
  bar.classList.remove('status-running', 'status-success', 'status-error');
  if (kind !== 'info') bar.classList.add(`status-${kind}`);
  if (kind === 'error') {
    bar.innerHTML = `<span>${escHtml(text)}</span><span class="status-close" onclick="document.getElementById('status-bar').classList.add('hidden')">×</span>`;
  } else {
    bar.textContent = text;
  }
  scrollChatToBottom({ defer: true });
}

function hideStatus() {
  if (statusTimer) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
  document.getElementById('status-bar').classList.add('hidden');
}

function flashStatus(text, kind = 'success', ms = 2400) {
  showStatus(text, kind);
  statusTimer = window.setTimeout(() => {
    statusTimer = null;
    if (!pendingAction) hideStatus();
  }, ms);
}

function showPreviewNotice(title, detail, { fixable = false } = {}) {
  previewNotice.classList.remove('hidden');
  const fixBtn = fixable
    ? `<button class="preview-fix-btn" onclick="this.disabled=true;this.textContent='Fixing...';window.__sacFixRenderError()">✨ Fix it</button>`
    : '';
  previewNotice.innerHTML = `<strong>${escHtml(title)}</strong><span title="${escHtml(detail)}">${escHtml(detail)}</span>${fixBtn}`;
}

// Called by the fix button in the preview notice.
// Posts directly to /inbox so the fix goes through SaC's own ingest
// pipeline (stream_ingest → evolve). This works for all connection
// modes (standalone, MCP, callback) because /inbox is the universal
// entry point — no StandaloneAgent dependency, no agent round-trip.
window.__sacFixRenderError = async function() {
  const detail = previewNotice.querySelector('span')?.textContent || '';
  const truncated = detail.slice(0, 300);
  hidePreviewNotice();
  addChatMsg('system', 'Fixing render error...');
  setPending(true);
  try {
    const res = await fetch('/inbox', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: conversationId,
        intent: `Fix the rendering error in the current code: ${truncated}`,
        content: `Fix the rendering error: ${truncated}`,
        type: 'ui',
      }),
    });
    if (!res.ok) {
      const err = await res.text().catch(() => 'unknown error');
      addChatMsg('system', 'Fix failed: ' + err);
    }
  } catch (err) {
    addChatMsg('system', 'Fix failed: ' + err.message);
  } finally {
    setPending(false);
  }
};

function hidePreviewNotice() {
  previewNotice.classList.add('hidden');
  previewNotice.innerHTML = '';
}

function hideSuggestions() {
  document.getElementById('suggestions-area').classList.add('hidden');
}

function beginNewAttempt() {
  hidePreviewNotice();
  removeProcessingCard();
  removeAgentWorking();
}

let _pendingTimer = null;
let _pendingPoll = null;   // polling interval while waiting for agent response
let _knownMsgCount = 0;    // message events known to the frontend
function setPending(value) {
  pendingAction = value;
  document.body.classList.toggle('is-pending', value);
  // Frontend-side TTL: if pending stays true for 45s with no response, show error
  if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
  if (value) {
    _pendingTimer = setTimeout(() => {
      if (pendingAction) {
        removeProcessingCard();
        showStatus('Agent did not respond. Tell your agent to use SaC MCP to restart server and wait for action.', 'error');
        setPending(false);
      }
    }, 45000);
  }
  // MCP pull mode: poll for missed events while pending (SSE may be unreliable)
  if (_pendingPoll) { clearInterval(_pendingPoll); _pendingPoll = null; }
  if (value && callbackUrl === '__mcp_pull__' && conversationId) {
    _pendingPoll = setInterval(() => _pollPendingResult(), 4000);
  }
  // Swap send button between send mode and cancel mode
  if (value) {
    sendBtn.disabled = false;
    sendBtn.classList.add('cancel-mode');
    sendBtn.textContent = '×';
    sendBtn.title = 'Cancel';
  } else {
    sendBtn.classList.remove('cancel-mode');
    sendBtn.textContent = '↑';
    sendBtn.title = 'Send';
    sendBtn.disabled = false;
  }
  // Disable/enable suggestion buttons
  document.querySelectorAll('.suggestion-btn').forEach(b => {
    b.disabled = value;
    b.style.opacity = value ? '0.5' : '';
    b.style.pointerEvents = value ? 'none' : '';
  });
}

function resizeIntentInput() {
  const maxHeight = 108;
  intentInput.style.height = 'auto';
  const nextHeight = Math.max(20, Math.min(intentInput.scrollHeight, maxHeight));
  intentInput.style.height = `${nextHeight}px`;
  intentInput.style.overflowY = intentInput.scrollHeight > maxHeight ? 'auto' : 'hidden';
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escClass(s) {
  return String(s || '').replace(/[^a-zA-Z0-9_-]/g, '_');
}

function compactText(s, max = 500) {
  const text = String(s || '').replace(/\s+/g, ' ').trim();
  return text.length > max ? text.slice(0, max - 1) + '...' : text;
}

function humanizeCallbackError(value) {
  const raw = String(value || '').replace(/\s+/g, ' ').trim();
  if (!raw) return 'Callback failed.';

  const messages = [];
  const messageRe = /"message"\s*:\s*"((?:\\.|[^"\\])*)"/g;
  let match;
  while ((match = messageRe.exec(raw))) {
    try {
      messages.push(JSON.parse(`"${match[1]}"`));
    } catch {
      messages.push(match[1]);
    }
  }

  let text = messages.find(Boolean) || raw;
  text = text
    .replace(/^Codex callback failed\s*[:(]\s*/i, '')
    .replace(/[).]*$/g, '')
    .trim();

  if (/usage limit/i.test(text)) {
    const retry = text.match(/try again at ([^.]+?)(?:\.|$)/i)?.[1]?.trim();
    return retry
      ? `Codex usage limit reached. Try again at ${retry}.`
      : 'Codex usage limit reached. Try again later.';
  }

  if (/finished without updating the app/i.test(text) || /no \/inbox response/i.test(text)) {
    return 'Codex finished without updating the app. The app is unchanged.';
  }

  if (/codex cli not found/i.test(text)) {
    return 'Codex CLI not found. Set SAC_CODEX_BIN to the Codex executable path.';
  }

  if (text.startsWith('{') || text.includes('{"type":')) {
    return 'Codex callback failed. Raw details are available in the card.';
  }
  return compactText(text, 180);
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function compactPath(value) {
  const path = String(value || '');
  const parts = path.split('/').filter(Boolean);
  if (parts.length <= 2) return path;
  return `.../${parts.slice(-2).join('/')}`;
}

function formatCodeSize(code) {
  const length = String(code || '').length;
  if (length >= 1000) return `${(length / 1000).toFixed(1)}k chars`;
  return `${length} chars`;
}

function adapterLabel(adapter) {
  if (adapter === 'codex_exec_resume') return 'Codex';
  if (adapter === 'openclaw_gateway') return 'OpenClaw';
  if (adapter === 'openclaw_taskflow') return 'OpenClaw TaskFlow';
  if (adapter === 'default') return 'HTTP';
  return adapter || 'Agent';
}

function runStatusLabel(status) {
  if (status === 'queued') return 'Queued';
  if (status === 'running') return 'Running';
  if (status === 'succeeded') return 'Completed';
  if (status === 'no_update') return 'No update';
  if (status === 'failed') return 'Failed';
  return status || 'Unknown';
}

function scrollChatToBottom() {
  const opts = typeof arguments[0] === 'object' ? arguments[0] : {};
  if (opts.defer) {
    requestAnimationFrame(() => requestAnimationFrame(() => scrollChatToBottom()));
    return;
  }
  const area = document.getElementById('chat-area');
  if (area) area.scrollTop = area.scrollHeight;
}

// ─── Model Selector ──────────────────────────────────────────
(function initModelSelector() {
  const btn = document.getElementById('model-selector');
  const label = document.getElementById('model-selector-label');
  const dropdown = document.getElementById('model-dropdown');
  if (!btn || !dropdown) return;

  const RECOMMENDED_ORDER = [
    'openai/gpt-5.6-luna',
    'google/gemini-3.7-flash',
    'anthropic/claude-haiku-4.5',
  ];
  const RECOMMENDED_IDS = new Set(RECOMMENDED_ORDER);

  function renderDropdown() {
    dropdown.innerHTML = '';
    const modelMap = Object.fromEntries(availableModels.map(m => [m.id, m]));
    const recommended = RECOMMENDED_ORDER.map(id => modelMap[id]).filter(Boolean);

    const groupLabel = document.createElement('div');
    groupLabel.className = 'model-dropdown-group';
    groupLabel.textContent = 'Recommended';
    dropdown.appendChild(groupLabel);

    for (const m of recommended) {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'model-dropdown-item' + (m.id === currentModel ? ' active' : '');
      item.innerHTML = '<span class="model-check">' + (m.id === currentModel ? '✓' : '') + '</span>' + m.name;
      item.addEventListener('click', () => {
        currentModel = m.id;
        label.textContent = m.name;
        dropdown.classList.add('hidden');
        renderDropdown();
      });
      dropdown.appendChild(item);
    }

    const hint = document.createElement('div');
    hint.className = 'model-dropdown-hint';
    hint.innerHTML = 'Add more models via <span>OpenRouter</span> in <code>src/sac/runtime/prompts/app.py</code>';
    dropdown.appendChild(hint);
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.classList.toggle('hidden');
  });
  document.addEventListener('click', () => dropdown.classList.add('hidden'));
  dropdown.addEventListener('click', (e) => e.stopPropagation());

  fetch('/models').then(r => r.json()).then(data => {
    availableModels = data.models || [];
    currentModel = data.default || (availableModels[0] && availableModels[0].id);
    const active = availableModels.find(m => m.id === currentModel);
    if (active) label.textContent = active.name;
    renderDropdown();
  }).catch(() => {});
})();

// Auto-load conversation if id is present in URL: /c/{id} or /?c={id}
(function () {
  const m = window.location.pathname.match(/^\/c\/([^/?#]+)/);
  const fromPath = m ? m[1] : null;
  const fromQuery = new URLSearchParams(window.location.search).get('c');
  const targetId = fromPath || fromQuery;
  if (targetId) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => window.loadConv(targetId));
    } else {
      window.loadConv(targetId);
    }
  }
})();
