/* global fetchWithTimeout, isCurrentMailboxContext */
let activeManualMailProgress = null;
const manualMailTasks = new Map();
const manualMailTaskCards = new Map();
let mailFetchTasksOpen = false;

function toggleMailFetchTasks(open) {
    mailFetchTasksOpen = typeof open === 'boolean' ? open : !mailFetchTasksOpen;
    renderMailFetchTasks();
}

function renderMailFetchTasks() {
    const panel = document.getElementById('mailFetchTasksPanel');
    const trigger = document.getElementById('mailFetchTasksButton');
    const badge = document.getElementById('mailFetchTasksBadge');
    const list = document.getElementById('mailFetchTasksList');
    const empty = document.getElementById('mailFetchTasksEmpty');
    if (!panel || !list) return;
    const completed = [...manualMailTasks.values()].filter(task => task.state !== 'running')
        .sort((left, right) => left.finishedAt - right.finishedAt);
    while (completed.length > 20) {
        const task = completed.shift();
        manualMailTasks.delete(task.id);
        manualMailTaskCards.get(task.id)?.card.remove();
        manualMailTaskCards.delete(task.id);
    }
    const running = [...manualMailTasks.values()].filter(task => task.state === 'running').length;
    panel.hidden = !mailFetchTasksOpen;
    trigger.setAttribute('aria-expanded', String(mailFetchTasksOpen));
    trigger.dataset.running = String(running > 0);
    trigger.title = running ? `收信任务：${running} 项进行中` : '收信任务';
    badge.hidden = manualMailTasks.size === 0;
    badge.textContent = running || manualMailTasks.size;
    if (empty) empty.hidden = manualMailTasks.size !== 0;
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
                error: element('button', 'mail-fetch-task-error', card)
            };
            nodes.bar.setAttribute('aria-label', '任务真实进度');
            nodes.error.type = 'button';
            nodes.error.textContent = '查看错误详情';
            nodes.error.addEventListener('click', () => showEmailFetchErrorModal(task.errorDetails));
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

function startManualMailProgress(context, target, pageSize) {
    if (activeManualMailProgress) activeManualMailProgress.stop(true);
    const panel = document.getElementById('mailFetchProgress');
    const bar = document.getElementById('mailFetchProgressBar');
    const title = document.getElementById('mailFetchProgressTitle');
    const summary = document.getElementById('mailFetchProgressSummary');
    const current = document.getElementById('mailFetchProgressCurrent');
    const elapsed = document.getElementById('mailFetchProgressElapsed');
    const started = Date.now();
    let requestId = '', page = 1, received = 0, snapshot = null;
    let stopped = false;
    let detached = false;
    const pages = Math.ceil(target / pageSize);
    const unit = context.aggregated ? '邮箱' : '文件夹';
    const task = {
        id: window.crypto.randomUUID().replaceAll('-', ''), context: { ...context }, target,
        pages, page, started, state: 'running', snapshot: null, received: 0, errorDetails: null
    };
    manualMailTasks.set(task.id, task);

    function render() {
        task.page = page;
        task.snapshot = snapshot;
        task.received = received;
        renderMailFetchTasks();
        if (!panel || detached || !isCurrentMailboxContext(context)) return;
        panel.hidden = false;
        title.textContent = `${context.aggregated ? '聚合收件箱' : '单邮箱'} · 第 ${page}/${pages} 批 · 最近 ${target} 封`;
        elapsed.textContent = `已耗时 ${Math.floor((Date.now() - started) / 1000)} 秒`;
        if (snapshot) {
            bar.max = Math.max(1, snapshot.total);
            bar.value = snapshot.completed;
            summary.textContent = `当前批${unit} ${snapshot.completed}/${snapshot.total} · 成功 ${snapshot.successful} · 失败 ${snapshot.failed} · 部分失败 ${snapshot.partial} · 超时 ${snapshot.timed_out} · 本批已读取 ${snapshot.fetched} 封 · 本次已展示 ${received} 封`;
            const labels = { inbox: '收件箱', junkemail: '垃圾邮件', deleteditems: '已删除邮件' };
            current.textContent = snapshot.active?.length
                ? `正在获取：${snapshot.active.map(value => labels[value] || value).join('、')}`
                : (snapshot.state === 'running' ? '正在整理邮件…' : '本批请求已结束');
        } else {
            bar.removeAttribute('value');
            summary.textContent = `等待${unit}进度 · 本次已获取 ${received} 封`;
            current.textContent = '正在连接邮件服务…';
        }
    }

    function tick() {
        if (stopped) return;
        if (!isCurrentMailboxContext(context)) run.stop(true);
        render();
    }

    const timer = window.setInterval(tick, 1000);
    const run = {
        beginPage(number) {
            page = number;
            snapshot = null;
            requestId = window.crypto.randomUUID().replaceAll('-', '');
            if (panel && !detached && isCurrentMailboxContext(context)) panel.dataset.state = 'running';
            render();
            return requestId;
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
            if (data?.partial || data?.success === false) {
                task.errorDetails = context.aggregated && typeof buildAggregatedFetchErrorDetails === 'function'
                    ? buildAggregatedFetchErrorDetails(data)
                    : (data.details || { error: data.error || '获取邮件失败' });
            }
        },
        finish(state, message) {
            if (stopped) return;
            task.state = state;
            task.message = message;
            task.finishedAt = Date.now();
            render();
            if (panel && !detached && isCurrentMailboxContext(context)) {
                panel.dataset.state = state;
                current.textContent = message;
                if (state === 'completed') { bar.max = 1; bar.value = 1; }
            }
            run.stop(false);
        },
        stop(hide) {
            if (hide) {
                detached = true;
                if (panel && activeManualMailProgress === run) panel.hidden = true;
                if (activeManualMailProgress === run) activeManualMailProgress = null;
                return;
            }
            stopped = true;
            window.clearInterval(timer);
            if (activeManualMailProgress === run) activeManualMailProgress = null;
        }
    };
    activeManualMailProgress = run;
    renderMailFetchTasks();
    return run;
}
