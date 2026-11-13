/* global fetchWithTimeout, isCurrentMailboxContext */
const manualMailTasks = new Map();
const manualMailTaskCards = new Map();
let mailFetchTasksOpen = false;
const MAIL_TASK_STORAGE_PREFIX = 'outlook.manualMailTask.v1.';
const MAIL_TASK_PANEL_KEY = 'outlook.manualMailTasksPanelOpen.v1';
const savedManualMailTasks = new Map();
let manualMailTasksInitialized = false;

function persistManualMailTask(task) {
    try {
        const context = task.context;
        const value = JSON.stringify({
            version: 1, id: task.id,
            context: { account: context.account, aggregated: !!context.aggregated, folder: context.folder,
                groupId: context.groupId, groupName: context.groupName },
            target: task.target, pageSize: task.pageSize, page: task.page, started: task.started,
            state: task.state, snapshot: task.snapshot, received: task.received,
            requestId: task.requestId, lastAppliedPage: task.lastAppliedPage, lastResult: task.lastResult,
            receivedKeys: task.state === 'running' ? task.receivedKeys : [],
            finishedAt: task.finishedAt, message: task.message, errorDetails: task.errorDetails
        });
        if (savedManualMailTasks.get(task.id) === value) return;
        window.localStorage.setItem(MAIL_TASK_STORAGE_PREFIX + task.id, value);
        savedManualMailTasks.set(task.id, value);
    } catch (_) {
        const warning = document.getElementById('mailFetchTasksStorageWarning');
        if (warning) warning.hidden = false;
    }
}

function initManualMailTasks() {
    if (manualMailTasksInitialized) return;
    manualMailTasksInitialized = true;
    try {
        mailFetchTasksOpen = window.localStorage.getItem(MAIL_TASK_PANEL_KEY) === 'true';
        const keys = [];
        for (let index = 0; index < window.localStorage.length; index++) {
            const key = window.localStorage.key(index);
            if (key?.startsWith(MAIL_TASK_STORAGE_PREFIX)) keys.push(key);
        }
        const restored = [];
        for (const key of keys) {
            try {
                const task = JSON.parse(window.localStorage.getItem(key));
                if (task?.version !== 1 || !/^[a-f0-9]{32}$/.test(task.id || '')
                    || key !== MAIL_TASK_STORAGE_PREFIX + task.id || !task.context
                    || typeof task.context.account !== 'string' || typeof task.context.folder !== 'string'
                    || !['running', 'completed', 'partial', 'failed'].includes(task.state)
                    || !Number.isFinite(task.started) || task.started <= 0
                    || !Number.isInteger(task.target) || task.target < 1 || task.target > 2000
                    || !Number.isInteger(task.pageSize) || task.pageSize < 1 || task.pageSize > 50
                    || !Number.isInteger(task.page) || task.page < 1) continue;
                task.pages = Math.ceil(task.target / task.pageSize);
                if (task.page > task.pages) continue;
                task.receivedKeys = Array.isArray(task.receivedKeys)
                    ? task.receivedKeys.filter(value => typeof value === 'string').slice(0, 10000) : [];
                task.received = Number.isFinite(task.received) ? Math.max(0, task.received) : 0;
                if (task.snapshot && typeof task.snapshot === 'object') {
                    task.snapshot.active = Array.isArray(task.snapshot.active)
                        ? task.snapshot.active.filter(value => typeof value === 'string').slice(0, 3) : [];
                } else task.snapshot = null;
                if (!Number.isFinite(task.finishedAt)) delete task.finishedAt;
                restored.push(task);
            } catch (_) { /* Ignore one malformed record without losing other tasks. */ }
        }
        restored.sort((left, right) => left.started - right.started);
        for (const task of restored.slice(-28)) manualMailTasks.set(task.id, task);
    } catch (_) {
        const warning = document.getElementById('mailFetchTasksStorageWarning');
        if (warning) warning.hidden = false;
    }
    renderMailFetchTasks();
    for (const task of manualMailTasks.values()) {
        if (task.state === 'running') void resumeManualMailTask(task);
    }
}

async function resumeManualMailTask(task) {
    const run = startManualMailProgress(task.context, task.target, task.pageSize, task);
    try {
        while (true) {
            let result;
            if (task.lastAppliedPage === task.page && task.lastResult) {
                result = task.lastResult;
            } else {
                if (!/^[a-f0-9]{32}$/.test(task.requestId || '')) {
                    run.finish('failed', '任务尚未提交，请重新获取邮件');
                    return;
                }
                result = await waitForManualMailFetch(task.requestId, { onProgress: progress => run.updateSnapshot(progress) });
                run.recordResult(result);
            }
            if (!result.success || result.partial) {
                const expired = result.errorCode === 'MAIL_FETCH_JOB_EXPIRED' || result.error?.code === 'MAIL_FETCH_JOB_EXPIRED';
                run.finish(result.success ? 'partial' : 'failed', expired
                    ? '后台任务已失效，请重新获取邮件（原记录已保留）'
                    : (result.success ? '邮件未完整获取，请查看错误详情' : '获取邮件失败，请查看错误详情'));
                return;
            }
            if (!result.has_more || task.received >= task.target || task.page >= task.pages) {
                run.finish('completed', `已获取 ${task.received} 封邮件`);
                return;
            }
            const requestId = run.beginPage(task.page + 1);
            const skip = (task.page - 1) * task.pageSize;
            const params = { folder: task.context.folder, skip, top: Math.min(task.pageSize, task.target - skip), keyword: '' };
            const url = task.context.aggregated
                ? buildAggregatedEmailsUrl({ ...params, groupId: task.context.groupId })
                : buildEmailListRequestUrl(task.context.account, { ...params, method: 'graph' });
            const response = await fetchWithTimeout(`${url}&async=1&progress_id=${requestId}`, {
                timeoutMs: 10000, cache: 'no-store'
            });
            const submitted = await response.json();
            if (!submitted.pending) run.recordResult(submitted);
        }
    } catch (error) {
        run.recordResult({ success: false, details: { browser: {
            code: 'MAIL_TASK_RESUME_FAILED', message: '恢复任务查询失败，请重新获取邮件',
            type: error?.name || 'Error', details: error?.message || ''
        } } });
        run.finish('failed', '恢复任务查询失败，请重新获取邮件（原记录已保留）');
    }
}

function toggleMailFetchTasks(open) {
    mailFetchTasksOpen = typeof open === 'boolean' ? open : !mailFetchTasksOpen;
    try { window.localStorage.setItem(MAIL_TASK_PANEL_KEY, String(mailFetchTasksOpen)); } catch (_) { /* Keep panel usable. */ }
    renderMailFetchTasks();
}

function removeManualMailTaskRecord(task) {
    try {
        window.localStorage.removeItem(MAIL_TASK_STORAGE_PREFIX + task.id);
    } catch (_) {
        const warning = document.getElementById('mailFetchTasksStorageWarning');
        if (warning) warning.hidden = false;
        return false;
    }
    manualMailTasks.delete(task.id);
    savedManualMailTasks.delete(task.id);
    manualMailTaskCards.get(task.id)?.card.remove();
    manualMailTaskCards.delete(task.id);
    return true;
}

function deleteManualMailTask(id) {
    const task = manualMailTasks.get(id);
    if (!task || task.state === 'running') return;
    removeManualMailTaskRecord(task);
    renderMailFetchTasks();
}

function clearCompletedMailTasks() {
    for (const task of manualMailTasks.values()) {
        if (task.state !== 'running') removeManualMailTaskRecord(task);
    }
    renderMailFetchTasks();
}

function renderMailFetchTasks() {
    const panel = document.getElementById('mailFetchTasksPanel');
    const trigger = document.getElementById('mailFetchTasksButton');
    const badge = document.getElementById('mailFetchTasksBadge');
    const list = document.getElementById('mailFetchTasksList');
    const empty = document.getElementById('mailFetchTasksEmpty');
    const clear = document.getElementById('mailFetchTasksClear');
    if (!panel || !list) return;
    const completed = [...manualMailTasks.values()].filter(task => task.state !== 'running')
        .sort((left, right) => left.finishedAt - right.finishedAt);
    while (completed.length > 20) {
        const task = completed.shift();
        removeManualMailTaskRecord(task);
    }
    const running = [...manualMailTasks.values()].filter(task => task.state === 'running').length;
    panel.hidden = !mailFetchTasksOpen;
    trigger.setAttribute('aria-expanded', String(mailFetchTasksOpen));
    trigger.dataset.running = String(running > 0);
    trigger.title = running ? `收信任务：${running} 项进行中` : '收信任务';
    badge.hidden = manualMailTasks.size === 0;
    badge.textContent = running || manualMailTasks.size;
    if (empty) empty.hidden = manualMailTasks.size !== 0;
    if (clear) clear.disabled = ![...manualMailTasks.values()].some(task => task.state !== 'running');
    const states = { running: '进行中', completed: '已完成', partial: '部分失败', failed: '失败' };
    for (const task of [...manualMailTasks.values()].reverse()) {
        let nodes = manualMailTaskCards.get(task.id);
        if (!nodes) {
            const element = (tag, className, parent) => {
                const el = document.createElement(tag);
                el.className = className;
                parent.appendChild(el);
                return el;
            };
            const card = element('article', 'mail-fetch-task', list);
            const heading = element('div', 'mail-fetch-task-heading', card);
            nodes = {
                card, title: element('strong', 'mail-fetch-task-title', heading),
                state: element('span', 'mail-fetch-task-state', heading),
                meta: element('div', 'mail-fetch-task-meta', card),
                bar: element('progress', '', card),
                summary: element('div', 'mail-fetch-task-summary', card),
                current: element('div', 'mail-fetch-task-current', card),
                error: element('button', 'mail-fetch-task-error', card),
                remove: element('button', 'mail-fetch-task-delete', card)
            };
            nodes.bar.setAttribute('aria-label', '任务真实进度');
            nodes.error.type = 'button';
            nodes.error.textContent = '查看错误详情';
            nodes.error.addEventListener('click', () => showEmailFetchErrorModal(task.errorDetails));
            nodes.remove.type = 'button';
            nodes.remove.textContent = '删除记录';
            nodes.remove.addEventListener('click', () => deleteManualMailTask(task.id));
            list.prepend(card);
            manualMailTaskCards.set(task.id, nodes);
        }
        nodes.card.dataset.state = task.state;
        nodes.title.textContent = task.context.aggregated
            ? `聚合收件箱 · ${task.context.groupName || '当前分组'}`
            : task.context.account;
        nodes.state.textContent = states[task.state];
        const folder = { all: '全部邮件', inbox: '收件箱', junkemail: '垃圾邮件' }[task.context.folder] || '自定义文件夹';
        const seconds = Math.floor(((task.finishedAt || Date.now()) - task.started) / 1000);
        nodes.meta.textContent = `${folder} · 最近 ${task.target} 封 · 第 ${task.page}/${task.pages} 批 · 已耗时 ${seconds} 秒`;
        const snapshot = task.snapshot;
        if (snapshot) {
            nodes.bar.max = Math.max(1, snapshot.total);
            nodes.bar.value = snapshot.completed;
            nodes.summary.textContent = `${task.context.aggregated ? '邮箱' : '文件夹'} ${snapshot.completed}/${snapshot.total} · 成功 ${snapshot.successful} · 失败 ${snapshot.failed} · 部分失败 ${snapshot.partial} · 超时 ${snapshot.timed_out}`;
        } else {
            nodes.bar.removeAttribute('value');
            nodes.summary.textContent = '等待服务端进度…';
        }
        if (task.state === 'completed') { nodes.bar.max = 1; nodes.bar.value = 1; }
        const active = (snapshot?.active || []).map(value => ({ inbox: '收件箱', junkemail: '垃圾邮件' }[value] || value));
        nodes.current.textContent = task.state === 'running'
            ? `${active.length ? `正在获取：${active.join('、')}` : '正在整理邮件…'} · 本批已读取 ${snapshot?.fetched || 0} 封`
            : task.message;
        nodes.error.hidden = !task.errorDetails || task.state === 'running';
        nodes.remove.hidden = task.state === 'running';
    }
}

async function waitForManualMailFetch(requestId, options = {}) {
    const deadline = Date.now() + 15 * 60 * 1000;
    let failures = 0;
    while (Date.now() < deadline) {
        let data;
        try {
            const response = await fetchWithTimeout(`/api/emails/fetch-progress/${requestId}?result=1`, {
                timeoutMs: 10000, cache: 'no-store'
            });
            data = await response.json();
            if (!data.success) throw new Error('无法查询邮件任务');
            failures = 0;
        } catch (error) {
            if (++failures >= 3) throw error;
        }
        if (data?.success) {
            if (data.progress && typeof options.onProgress === 'function') options.onProgress(data.progress);
            if (!data.pending) {
                return data.result || { success: false, error: '邮件任务结果已失效，请重新获取' };
            }
        }
        await new Promise(resolve => window.setTimeout(resolve, 1000));
    }
    throw new DOMException('等待后台收信结果超时，请重新获取邮件', 'TimeoutError');
}

function startManualMailProgress(context, target, pageSize, restoredTask = null) {
    const started = restoredTask?.started || Date.now();
    let page = restoredTask?.page || 1, received = restoredTask?.received || 0, snapshot = restoredTask?.snapshot || null;
    let stopped = false;
    const pages = Math.ceil(target / pageSize);
    const task = restoredTask || {
        id: window.crypto.randomUUID().replaceAll('-', ''), context: { ...context }, target,
        pages, page, pageSize, started, state: 'running', snapshot: null, received: 0, errorDetails: null,
        requestId: '', receivedKeys: [], lastAppliedPage: 0, lastResult: null
    };
    manualMailTasks.set(task.id, task);

    function render() {
        task.page = page;
        task.snapshot = snapshot;
        task.received = received;
        persistManualMailTask(task);
        renderMailFetchTasks();
    }

    function tick() {
        if (stopped) return;
        render();
    }

    const timer = window.setInterval(tick, 1000);
    const run = {
        beginPage(number) {
            page = number;
            snapshot = null;
            task.requestId = task.id.slice(0, 24) + number.toString(16).padStart(8, '0');
            render();
            return task.requestId;
        },
        applyResponse(data, count) {
            if (stopped) return;
            received = count;
            if (data?.fetch_progress) snapshot = data.fetch_progress;
            render();
        },
        updateSnapshot(progress) {
            if (stopped) return;
            snapshot = progress;
            render();
        },
        recordResult(data) {
            if (stopped) return;
            if (data.fetch_progress) snapshot = data.fetch_progress;
            const keys = new Set(task.receivedKeys);
            for (const item of data.emails || []) {
                keys.add(`${item.account_email || context.account}:${item.folder || context.folder}:${item.id_mode || ''}:${item.id}`);
            }
            task.receivedKeys = [...keys];
            received = keys.size;
            task.lastAppliedPage = page;
            task.lastResult = { success: !!data.success, partial: !!data.partial, has_more: !!data.has_more,
                errorCode: data.error?.code || '' };
            if (data?.partial || data?.success === false) {
                task.errorDetails = context.aggregated && typeof buildAggregatedFetchErrorDetails === 'function'
                    ? buildAggregatedFetchErrorDetails(data)
                    : (data.details || { error: data.error || '获取邮件失败' });
            }
            render();
        },
        finish(state, message) {
            if (stopped) return;
            task.state = state;
            task.message = message;
            task.finishedAt = Date.now();
            render();
            run.stop();
        },
        stop() {
            stopped = true;
            window.clearInterval(timer);
        }
    };
    render();
    return run;
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initManualMailTasks, { once: true });
    else initManualMailTasks();
}
