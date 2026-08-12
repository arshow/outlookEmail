(() => {
    let csrfToken = null;

    function showToast(message, isError = false) {
        const el = document.getElementById('toast');
        if (!el) return;
        el.textContent = message;
        el.classList.toggle('error', !!isError);
        el.classList.add('show');
        setTimeout(() => el.classList.remove('show'), 2600);
    }

    async function ensureCsrf() {
        if (csrfToken) return csrfToken;
        const response = await fetch('/api/csrf-token', { credentials: 'same-origin', cache: 'no-store' });
        const data = await response.json();
        csrfToken = data.csrf_token || null;
        return csrfToken;
    }

    async function api(url, options = {}) {
        const opts = { credentials: 'same-origin', ...options };
        opts.headers = { ...(opts.headers || {}) };
        if (opts.method && opts.method.toUpperCase() !== 'GET') {
            const token = await ensureCsrf();
            if (token) opts.headers['X-CSRFToken'] = token;
            if (opts.body && !opts.headers['Content-Type']) {
                opts.headers['Content-Type'] = 'application/json';
            }
        }
        const response = await fetch(url, opts);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
            throw new Error(data.error || `请求失败 (${response.status})`);
        }
        return data;
    }

    function switchTab(tab) {
        document.querySelectorAll('.ai-tab').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.tab === tab);
        });
        document.querySelectorAll('.ai-panel').forEach((panel) => {
            panel.classList.toggle('active', panel.id === `panel-${tab}`);
        });
    }

    function splitCsv(value) {
        return String(value || '')
            .split(/[,，]/)
            .map((part) => part.trim())
            .filter(Boolean);
    }

    async function loadSettings() {
        const data = await api('/api/ai/settings');
        const s = data.settings || {};
        document.getElementById('aiEnabled').checked = !!s.enabled;
        document.getElementById('aiProvider').value = s.provider || 'gemini';
        document.getElementById('aiModel').value = s.model || '';
        document.getElementById('geminiBaseUrl').value = s.gemini_base_url || '';
        document.getElementById('deepseekBaseUrl').value = s.deepseek_base_url || '';
        document.getElementById('systemPersona').value = s.system_persona || '';
        document.getElementById('geminiApiKey').value = '';
        document.getElementById('deepseekApiKey').value = '';
        document.getElementById('geminiKeyHint').textContent = s.gemini_api_key_configured ? '已配置（********）' : '未配置';
        document.getElementById('deepseekKeyHint').textContent = s.deepseek_api_key_configured ? '已配置（********）' : '未配置';
        const socks = s.gemini_socks5 || {};
        document.getElementById('geminiSocksEnabled').checked = !!socks.enabled;
        document.getElementById('geminiSocksHost').value = socks.hostname || '';
        document.getElementById('geminiSocksPort').value = socks.port || '';
        document.getElementById('geminiSocksUser').value = socks.username || '';
        document.getElementById('geminiSocksPass').value = '';
        document.getElementById('geminiSocksPassHint').textContent = socks.has_password ? '已保存密码' : '未设置密码';
    }

    async function saveSettings(extra = {}) {
        const payload = {
            enabled: document.getElementById('aiEnabled').checked,
            provider: document.getElementById('aiProvider').value,
            model: document.getElementById('aiModel').value.trim(),
            gemini_base_url: document.getElementById('geminiBaseUrl').value.trim(),
            deepseek_base_url: document.getElementById('deepseekBaseUrl').value.trim(),
            system_persona: document.getElementById('systemPersona').value,
            gemini_socks5: {
                enabled: document.getElementById('geminiSocksEnabled').checked,
                hostname: document.getElementById('geminiSocksHost').value.trim(),
                port: Number(document.getElementById('geminiSocksPort').value || 0),
                username: document.getElementById('geminiSocksUser').value.trim(),
                password: document.getElementById('geminiSocksPass').value,
                keep_password: true,
            },
            ...extra,
        };
        const geminiKey = document.getElementById('geminiApiKey').value.trim();
        const deepseekKey = document.getElementById('deepseekApiKey').value.trim();
        if (geminiKey) payload.gemini_api_key = geminiKey;
        if (deepseekKey) payload.deepseek_api_key = deepseekKey;
        await api('/api/ai/settings', { method: 'PUT', body: JSON.stringify(payload) });
        showToast('设置已保存');
        await loadSettings();
    }

    async function testSettings() {
        const payload = {
            provider: document.getElementById('aiProvider').value,
            model: document.getElementById('aiModel').value.trim(),
            gemini_base_url: document.getElementById('geminiBaseUrl').value.trim(),
            deepseek_base_url: document.getElementById('deepseekBaseUrl').value.trim(),
            gemini_socks5: {
                enabled: document.getElementById('geminiSocksEnabled').checked,
                hostname: document.getElementById('geminiSocksHost').value.trim(),
                port: Number(document.getElementById('geminiSocksPort').value || 0),
                username: document.getElementById('geminiSocksUser').value.trim(),
                password: document.getElementById('geminiSocksPass').value,
                keep_password: true,
            },
        };
        const geminiKey = document.getElementById('geminiApiKey').value.trim();
        const deepseekKey = document.getElementById('deepseekApiKey').value.trim();
        if (geminiKey) payload.gemini_api_key = geminiKey;
        if (deepseekKey) payload.deepseek_api_key = deepseekKey;
        const data = await api('/api/ai/settings/test', { method: 'POST', body: JSON.stringify(payload) });
        showToast(`连接成功：${data.provider} / ${data.model}`);
    }

    async function loadKnowledge() {
        const data = await api('/api/ai/knowledge');
        const root = document.getElementById('knowledgeList');
        root.innerHTML = '';
        (data.entries || []).forEach((entry) => {
            const card = document.createElement('div');
            card.className = 'card';
            card.innerHTML = `
                <h3>${escapeHtml(entry.title)}</h3>
                <div class="meta">${escapeHtml(entry.category)} · priority ${entry.priority} · ${entry.enabled ? '启用' : '停用'}</div>
                <div>${escapeHtml(entry.content)}</div>
                <div class="meta" style="margin-top:8px;">关键词：${escapeHtml((entry.keywords || []).join(', '))}</div>
                <div class="btn-row">
                    <button class="btn" type="button" data-toggle="${entry.id}">${entry.enabled ? '停用' : '启用'}</button>
                    <button class="btn btn-danger" type="button" data-del="${entry.id}">删除</button>
                </div>
            `;
            card.querySelector('[data-toggle]')?.addEventListener('click', async () => {
                await api(`/api/ai/knowledge/${entry.id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ ...entry, enabled: !entry.enabled }),
                });
                await loadKnowledge();
            });
            card.querySelector('[data-del]')?.addEventListener('click', async () => {
                if (!confirm('确认删除该知识条目？')) return;
                await api(`/api/ai/knowledge/${entry.id}`, { method: 'DELETE' });
                await loadKnowledge();
            });
            root.appendChild(card);
        });
    }

    async function addKnowledge() {
        const payload = {
            title: document.getElementById('knowledgeTitle').value.trim(),
            category: document.getElementById('knowledgeCategory').value.trim() || 'general',
            keywords: splitCsv(document.getElementById('knowledgeKeywords').value),
            priority: Number(document.getElementById('knowledgePriority').value || 0),
            content: document.getElementById('knowledgeContent').value.trim(),
            enabled: true,
        };
        await api('/api/ai/knowledge', { method: 'POST', body: JSON.stringify(payload) });
        document.getElementById('knowledgeTitle').value = '';
        document.getElementById('knowledgeContent').value = '';
        document.getElementById('knowledgeKeywords').value = '';
        showToast('知识已新增');
        await loadKnowledge();
    }

    async function loadRules() {
        const data = await api('/api/ai/rules');
        const root = document.getElementById('rulesList');
        root.innerHTML = '';
        (data.rules || []).forEach((rule) => {
            const card = document.createElement('div');
            card.className = 'card';
            card.innerHTML = `
                <h3>${escapeHtml(rule.version_label || ('规则 #' + rule.id))}</h3>
                <div class="meta">
                    <span class="badge ${escapeHtml(rule.risk_level)}">${escapeHtml(rule.risk_level)}</span>
                    ${escapeHtml(rule.status)} · priority ${rule.priority} · ${rule.enabled ? '启用' : '停用'}
                </div>
                <div>${escapeHtml(rule.instruction)}</div>
                <div class="meta" style="margin-top:8px;">关键词：${escapeHtml((rule.keywords || []).join(', '))}</div>
                <div class="meta">禁止短语：${escapeHtml((rule.forbidden_phrases || []).join(', '))}</div>
                <div class="btn-row">
                    ${rule.status !== 'published' ? `<button class="btn btn-primary" type="button" data-publish="${rule.id}">发布</button>` : ''}
                    <button class="btn" type="button" data-archive="${rule.id}">归档</button>
                    <button class="btn btn-danger" type="button" data-del="${rule.id}">删除</button>
                </div>
            `;
            card.querySelector('[data-publish]')?.addEventListener('click', async () => {
                await api(`/api/ai/rules/${rule.id}/publish`, { method: 'POST', body: '{}' });
                await loadRules();
            });
            card.querySelector('[data-archive]')?.addEventListener('click', async () => {
                await api(`/api/ai/rules/${rule.id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ status: 'archived' }),
                });
                await loadRules();
            });
            card.querySelector('[data-del]')?.addEventListener('click', async () => {
                if (!confirm('确认删除该规则？')) return;
                await api(`/api/ai/rules/${rule.id}`, { method: 'DELETE' });
                await loadRules();
            });
            root.appendChild(card);
        });
    }

    function collectRulePayload(status) {
        return {
            version_label: document.getElementById('ruleLabel').value.trim(),
            risk_level: document.getElementById('ruleRisk').value,
            keywords: splitCsv(document.getElementById('ruleKeywords').value),
            intents: splitCsv(document.getElementById('ruleIntents').value),
            forbidden_phrases: splitCsv(document.getElementById('ruleForbidden').value),
            priority: Number(document.getElementById('rulePriority').value || 0),
            instruction: document.getElementById('ruleInstruction').value.trim(),
            status,
            enabled: true,
        };
    }

    async function addRule(status) {
        await api('/api/ai/rules', { method: 'POST', body: JSON.stringify(collectRulePayload(status)) });
        showToast(status === 'published' ? '规则已发布' : '草稿已创建');
        await loadRules();
    }

    async function testRules() {
        const text = document.getElementById('ruleTestText').value.trim();
        const data = await api('/api/ai/rules/test', { method: 'POST', body: JSON.stringify({ text }) });
        document.getElementById('ruleTestResult').textContent = JSON.stringify(data, null, 2);
    }

    async function loadRuns() {
        const data = await api('/api/ai/analysis-runs?limit=50');
        const body = document.getElementById('runsTableBody');
        body.innerHTML = '';
        (data.runs || []).forEach((run) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${escapeHtml(run.created_at || '')}</td>
                <td>${escapeHtml(run.account_email || '')}</td>
                <td>${escapeHtml(run.context_scope || '')}</td>
                <td>${escapeHtml((run.provider || '') + ' / ' + (run.model || ''))}</td>
                <td>${escapeHtml(run.status || '')}</td>
                <td><span class="badge ${escapeHtml(run.risk_level || '')}">${escapeHtml(run.risk_level || '-')}</span></td>
                <td>${escapeHtml(String(run.duration_ms ?? ''))}ms</td>
                <td>${escapeHtml(run.summary_zh || run.error_message || '')}</td>
            `;
            body.appendChild(tr);
        });
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function bindEvents() {
        document.querySelectorAll('.ai-tab').forEach((btn) => {
            btn.addEventListener('click', () => switchTab(btn.dataset.tab));
        });
        document.getElementById('saveSettingsBtn').addEventListener('click', () => {
            saveSettings().catch((err) => showToast(err.message, true));
        });
        document.getElementById('testSettingsBtn').addEventListener('click', () => {
            testSettings().catch((err) => showToast(err.message, true));
        });
        document.getElementById('clearGeminiKeyBtn').addEventListener('click', () => {
            saveSettings({ clear_gemini_api_key: true }).catch((err) => showToast(err.message, true));
        });
        document.getElementById('clearDeepseekKeyBtn').addEventListener('click', () => {
            saveSettings({ clear_deepseek_api_key: true }).catch((err) => showToast(err.message, true));
        });
        document.getElementById('addKnowledgeBtn').addEventListener('click', () => {
            addKnowledge().catch((err) => showToast(err.message, true));
        });
        document.getElementById('reloadKnowledgeBtn').addEventListener('click', () => {
            loadKnowledge().catch((err) => showToast(err.message, true));
        });
        document.getElementById('addRuleDraftBtn').addEventListener('click', () => {
            addRule('draft').catch((err) => showToast(err.message, true));
        });
        document.getElementById('addRulePublishedBtn').addEventListener('click', () => {
            addRule('published').catch((err) => showToast(err.message, true));
        });
        document.getElementById('testRulesBtn').addEventListener('click', () => {
            testRules().catch((err) => showToast(err.message, true));
        });
        document.getElementById('reloadRulesBtn').addEventListener('click', () => {
            loadRules().catch((err) => showToast(err.message, true));
        });
        document.getElementById('reloadRunsBtn').addEventListener('click', () => {
            loadRuns().catch((err) => showToast(err.message, true));
        });
    }

    async function boot() {
        bindEvents();
        try {
            await ensureCsrf();
            await loadSettings();
            await loadKnowledge();
            await loadRules();
            await loadRuns();
        } catch (err) {
            showToast(err.message || '加载失败', true);
        }
    }

    document.addEventListener('DOMContentLoaded', boot);
})();
