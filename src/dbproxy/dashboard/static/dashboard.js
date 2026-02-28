const conversations = {};
let ws;
let selectedKey = null;

function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/dashboard/ws`);

    ws.onopen = () => {
        document.getElementById('status').className = 'status connected';
        document.getElementById('status').textContent = 'Connected';
        document.getElementById('ws-status').textContent = 'Live';
    };

    ws.onclose = () => {
        document.getElementById('status').className = 'status disconnected';
        document.getElementById('status').textContent = 'Disconnected';
        document.getElementById('ws-status').textContent = 'Offline';
        setTimeout(connect, 2000);
    };

    ws.onmessage = (evt) => {
        const data = JSON.parse(evt.data);
        if (data.type === 'initial_state') {
            for (const conv of data.conversations) {
                conversations[conv.key] = conv;
            }
            render();
        } else if (data.type === 'state_update') {
            const conv = data.conversation;
            conversations[conv.key] = conv;
            render();
            addEvent(conv);
            if (selectedKey && conv.key === selectedKey) {
                loadDetail(selectedKey);
            }
        } else if (data.type === 'api_error') {
            addError(data);
        }
    };
}

function render() {
    const list = document.getElementById('conv-list');
    const convs = Object.values(conversations);
    document.getElementById('conv-count').textContent = convs.length;

    list.innerHTML = convs.map(c => {
        const util = (c.utilization * 100).toFixed(1);
        const utilClass = c.utilization < 0.7 ? 'util-low' : c.utilization < 0.95 ? 'util-mid' : 'util-high';
        const selected = c.key === selectedKey ? ' conv-selected' : '';
        const msgCount = c.message_count != null ? ` | ${c.message_count} msgs` : '';
        const shortModel = c.model.replace('claude-', '').replace(/-\d{8}$/, '');
        return `
            <div class="conv-card${selected}" onclick="selectConv('${c.key}')">
                <div>
                    <div class="conv-id">${c.conv_id}</div>
                    <div class="conv-model">${shortModel} | ${c.total_input_tokens.toLocaleString()} tokens${msgCount}</div>
                </div>
                <span class="phase-badge phase-${c.phase}">${c.phase}</span>
                <div>
                    <div class="utilization-bar">
                        <div class="utilization-fill ${utilClass}" style="width: ${util}%"></div>
                    </div>
                    <div style="font-size: 0.65rem; color: var(--text-secondary); text-align: center; margin-top: 2px">${util}%</div>
                </div>
                <button class="btn-reset" onclick="event.stopPropagation(); resetConv('${c.conv_id}')">Reset</button>
            </div>
        `;
    }).join('');
}

function selectConv(key) {
    selectedKey = selectedKey === key ? null : key;
    render();
    if (selectedKey) {
        loadDetail(selectedKey);
    } else {
        document.getElementById('detail-panel').style.display = 'none';
    }
}

async function loadDetail(key) {
    const panel = document.getElementById('detail-panel');
    try {
        const resp = await fetch(`/dashboard/api/conversation/${encodeURIComponent(key)}`);
        if (!resp.ok) {
            panel.innerHTML = '<div class="detail-error">Conversation not found</div>';
            panel.style.display = 'block';
            return;
        }
        const data = await resp.json();
        renderDetail(data);
    } catch (e) {
        panel.innerHTML = `<div class="detail-error">Error: ${e.message}</div>`;
        panel.style.display = 'block';
    }
}

/** Build an expandable content block — shows first ~120px with show/hide toggle. */
function buildExpandable(text, id) {
    const escaped = escapeHtml(text);
    const needsExpand = text.length > 300;
    if (!needsExpand) {
        return `<div class="expandable-content expanded">${escaped}</div>`;
    }
    return `<div class="expandable">
        <div class="expandable-content collapsed" id="${id}">${escaped}</div>
        <div class="expand-fade" id="${id}-fade"></div>
        <button class="btn-expand" id="${id}-btn" onclick="toggleExpand('${id}')">Show full content</button>
    </div>`;
}

function toggleExpand(id) {
    const el = document.getElementById(id);
    const fade = document.getElementById(id + '-fade');
    const btn = document.getElementById(id + '-btn');
    if (el.classList.contains('collapsed')) {
        el.classList.remove('collapsed');
        el.classList.add('expanded');
        if (fade) fade.style.display = 'none';
        btn.textContent = 'Collapse';
    } else {
        el.classList.remove('expanded');
        el.classList.add('collapsed');
        if (fade) fade.style.display = '';
        btn.textContent = 'Show full content';
    }
}

/** Build a message preview with optional expand for long content. */
function buildMsgPreview(text, msgId) {
    const maxLen = 200;
    if (text.length <= maxLen) {
        return `<span class="msg-preview">${escapeHtml(text)}</span>`;
    }
    const short = text.substring(0, maxLen);
    return `<span class="msg-preview" id="${msgId}">${escapeHtml(short)}…</span>
        <button class="btn-msg-expand" onclick="toggleMsg('${msgId}', ${JSON.stringify(JSON.stringify(text))})">more</button>`;
}

function toggleMsg(id, fullJson) {
    const el = document.getElementById(id);
    const btn = el.nextElementSibling;
    const full = JSON.parse(fullJson);
    if (btn.textContent === 'more') {
        el.textContent = full;
        btn.textContent = 'less';
    } else {
        el.textContent = full.substring(0, 200) + '…';
        btn.textContent = 'more';
    }
}

function renderDetail(data) {
    const panel = document.getElementById('detail-panel');
    const anchor = data.wal_start_index;
    const hasCheckpoint = data.checkpoint_content && data.checkpoint_content.length > 0;
    const shortModel = data.model.replace('claude-', '').replace(/-\d{8}$/, '');

    // Messages
    let msgsHtml = '';
    if (data.messages && data.messages.length > 0) {
        msgsHtml = data.messages.map((m, i) => {
            let zone = '';
            if (anchor != null) {
                zone = i < anchor ? 'msg-checkpointed' : 'msg-wal';
            }
            const roleClass = m.role === 'user' ? 'role-user' : m.role === 'assistant' ? 'role-assistant' : 'role-other';
            const preview = buildMsgPreview(m.preview, `msg-${data.conv_id}-${i}`);
            return `<div class="msg-row ${zone}">
                <span class="msg-index">${i}</span>
                <span class="msg-role ${roleClass}">${m.role}</span>
                ${preview}
            </div>`;
        }).join('');
    } else {
        msgsHtml = '<div class="detail-empty">No messages captured yet</div>';
    }

    // Checkpoint
    let checkpointHtml = '';
    if (hasCheckpoint) {
        checkpointHtml = `
            <div class="detail-section">
                <h3>Checkpoint Summary</h3>
                ${buildExpandable(data.checkpoint_content, 'ckpt-' + data.conv_id)}
            </div>`;
    }

    // Anchor
    let anchorHtml = '';
    if (anchor != null) {
        anchorHtml = `<div class="detail-meta">
            Checkpoint anchor: <strong>index ${anchor}</strong> |
            Checkpointed: <strong>${anchor}</strong> msgs |
            WAL: <strong>${data.messages.length - anchor}</strong> msgs
        </div>`;
    }

    panel.innerHTML = `
        <div class="detail-header">
            <h3>${data.conv_id} (${shortModel})</h3>
            <button class="btn-close" onclick="selectConv('${data.key}')">&times;</button>
        </div>
        <div class="detail-meta">
            ${data.model} | ${data.phase} | ${(data.utilization * 100).toFixed(1)}% |
            ${data.total_input_tokens.toLocaleString()} / ${data.context_window.toLocaleString()} tokens
        </div>
        ${anchorHtml}
        ${checkpointHtml}
        <div class="detail-section">
            <h3>Messages (${data.messages.length})</h3>
            <div class="msg-legend">
                <span class="legend-chip legend-checkpointed">Checkpointed</span>
                <span class="legend-chip legend-wal">WAL (post-checkpoint)</span>
            </div>
            <div class="msg-list">${msgsHtml}</div>
        </div>
    `;
    panel.style.display = 'block';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function resetConv(convId) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'reset_conversation', conv_id: convId }));
    }
}

function addEvent(conv) {
    const events = document.getElementById('events');
    const time = new Date().toLocaleTimeString();
    const line = document.createElement('div');
    line.className = 'event-line';
    const shortModel = conv.model.replace('claude-', '').replace(/-\d{8}$/, '');
    line.innerHTML = `<span style="color:var(--text-muted)">${time}</span> <span class="event-type">${conv.phase}</span> ${conv.conv_id} <span style="color:var(--text-secondary)">${shortModel}</span> (${(conv.utilization * 100).toFixed(1)}%)`;
    events.prepend(line);
    while (events.children.length > 200) events.lastChild.remove();
}

function addError(data) {
    const events = document.getElementById('events');
    const time = new Date().toLocaleTimeString();
    const line = document.createElement('div');
    line.className = 'event-line';
    line.innerHTML = `<span style="color:var(--text-muted)">${time}</span> <span style="color:var(--accent-red);font-weight:bold">ERROR ${data.status}</span> ${data.conv_id} <span style="color:var(--accent-red)">${escapeHtml(data.body).substring(0, 200)}</span>`;
    events.prepend(line);
    while (events.children.length > 200) events.lastChild.remove();
}

connect();
