const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/index/16-mail-fetch-progress.js'), 'utf8');

function setup(aggregated = false) {
    function element() {
        return { hidden: true, dataset: {}, textContent: '', children: [],
            removeAttribute(name) { delete this[name]; },
            setAttribute(name, value) { this[name] = value; },
            appendChild(child) { child.parent = this; this.children.push(child); },
            prepend(child) { child.remove(); child.parent = this; this.children.unshift(child); },
            remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); },
            addEventListener() {}
        };
    }
    const elements = Object.fromEntries(['', 'Bar', 'Title', 'Summary', 'Current', 'Elapsed'].map(suffix => [
        'mailFetchProgress' + suffix,
        element()
    ]));
    for (const id of ['Panel', 'Button', 'Badge', 'List', 'Empty']) elements['mailFetchTasks' + id] = element();
    let tick, valid = true, cleared = false, counter = 0;
    const context = vm.createContext({
        document: { getElementById: id => elements[id], createElement: element }, AbortController,
        Date,
        window: {
            crypto: { randomUUID: () => String(++counter).padStart(32, '0') },
            setInterval: callback => { tick = callback; return 1; },
            clearInterval: () => { cleared = true; }
        },
        isCurrentMailboxContext: () => valid,
        fetchWithTimeout: async () => ({ json: async () => ({ success: true, progress: {
            state: 'running', total: 2, completed: 1, successful: 1, failed: 0,
            partial: 0, timed_out: 0, fetched: 3, active: aggregated ? ['beta@example.com'] : ['junkemail']
        } }) })
    });
    vm.runInContext(source, context);
    const run = vm.runInContext(`startManualMailProgress({account:'test@example.com',folder:'all',aggregated:${aggregated}}, 100, 50)`, context);
    const update = () => run.updateSnapshot({ state: 'running', total: 2, completed: 1, successful: 1,
        failed: 0, partial: 0, timed_out: 0, fetched: 3, active: aggregated ? ['beta@example.com'] : ['junkemail'] });
    return { run, elements, context, tick: () => { update(); return tick(); }, setValid: value => { valid = value; }, isCleared: () => cleared };
}

for (const aggregated of [false, true]) {
    test((aggregated ? 'aggregated accounts' : 'single mailbox folders') + ' shows real progress and active unit', async () => {
        const { run, elements, tick, isCleared } = setup(aggregated);
        assert.match(run.beginPage(1), /^[a-f0-9]{32}$/);
        assert.equal(elements.mailFetchProgressBar.value, undefined);
        await tick();
        assert.equal(elements.mailFetchProgressBar.value, 1);
        assert.equal(elements.mailFetchProgressBar.max, 2);
        assert.match(elements.mailFetchProgressSummary.textContent, aggregated ? /邮箱 1\/2/ : /文件夹 1\/2/);
        assert.match(elements.mailFetchProgressCurrent.textContent, aggregated ? /beta@example.com/ : /垃圾邮件/);
        run.finish('partial', '部分文件夹超时');
        assert.equal(elements.mailFetchProgress.dataset.state, 'partial');
        assert.equal(elements.mailFetchProgressCurrent.textContent, '部分文件夹超时');
        assert.equal(isCleared(), true);
        assert.equal(elements.mailFetchProgress.hidden, false);
    });
}

test('next batch resets its bar; success preserves received count and stops polling', async () => {
    const { run, elements, tick, isCleared } = setup();
    run.beginPage(1);
    await tick();
    run.beginPage(2);
    assert.equal(elements.mailFetchProgressBar.value, undefined);
    assert.match(elements.mailFetchProgressTitle.textContent, /第 2\/2 批/);
    run.applyResponse({ fetch_progress: { total: 2, completed: 2, successful: 2, failed: 0, partial: 0, timed_out: 0, fetched: 30, active: [], state: 'completed' } }, 80);
    run.finish('completed', '已获取 80 封邮件');
    assert.match(elements.mailFetchProgressSummary.textContent, /已展示 80 封/);
    assert.equal(elements.mailFetchProgressBar.value, 1);
    assert.equal(isCleared(), true);
});

test('switching mailboxes hides inline progress but keeps the task running until completion', async () => {
    const { run, elements, tick, setValid, isCleared } = setup();
    run.beginPage(1);
    setValid(false);
    await tick();
    assert.equal(elements.mailFetchProgress.hidden, true);
    assert.equal(isCleared(), false);
    run.finish('completed', '已完成');
    assert.equal(isCleared(), true);
});

test('background result polling tolerates a transient gateway failure and returns final partial data', async () => {
    const snapshots = [];
    let calls = 0;
    const context = vm.createContext({
        Date, DOMException,
        window: { setTimeout: callback => { callback(); } },
        isCurrentMailboxContext: () => true,
        fetchWithTimeout: async () => {
            calls++;
            if (calls === 2) throw new TypeError('temporary gateway failure');
            return { json: async () => ({ success: true, pending: calls < 3,
                progress: { completed: calls < 3 ? 1 : 2, total: 2 },
                result: calls < 3 ? null : { success: true, partial: true, emails: [{ id: 'one' }] }
            }) };
        }
    });
    vm.runInContext(source, context);
    context.onProgress = snapshot => snapshots.push(snapshot.completed);
    const result = await vm.runInContext('waitForManualMailFetch("test", {onProgress})', context);
    assert.equal(calls, 3);
    assert.equal(result.partial, true);
    assert.equal(result.emails[0].id, 'one');
    assert.deepEqual(snapshots, [1, 2]);
});

test('background task result is still collected after switching mailbox', async () => {
    const context = vm.createContext({ Date, DOMException, window: {}, isCurrentMailboxContext: () => false,
        fetchWithTimeout: async () => ({ json: async () => ({ success: true, pending: false, result: {success:true,emails:[]} }) })
    });
    vm.runInContext(source, context);
    const result = await vm.runInContext('waitForManualMailFetch("test", {context:{}})', context);
    assert.equal(result.success, true);
});

test('floating panel stays open after completion and mailbox changes until explicitly closed', async () => {
    const { run, elements, context, tick, setValid } = setup();
    vm.runInContext('toggleMailFetchTasks(true)', context);
    run.beginPage(1);
    await tick();
    setValid(false);
    await tick();
    assert.equal(elements.mailFetchTasksPanel.hidden, false);
    run.finish('completed', '已获取 3 封邮件');
    assert.equal(elements.mailFetchTasksPanel.hidden, false);
    assert.equal(elements.mailFetchTasksList.children.length, 1);
    assert.equal(elements.mailFetchTasksList.children[0].dataset.state, 'completed');
    vm.runInContext('toggleMailFetchTasks(false)', context);
    assert.equal(elements.mailFetchTasksPanel.hidden, true);
    vm.runInContext('renderMailFetchTasks()', context);
    assert.equal(elements.mailFetchTasksPanel.hidden, true);
});

test('each manual refresh adds a separate task without losing the previous running task', async () => {
    const { run, elements, context } = setup();
    run.beginPage(1);
    const other = vm.runInContext('startManualMailProgress({account:"other@example.com",folder:"all",aggregated:false},10,50)', context);
    other.beginPage(1);
    assert.equal(elements.mailFetchTasksList.children.length, 2);
    assert.equal(elements.mailFetchTasksBadge.textContent, 2);
    other.finish('completed', '完成');
    assert.equal(elements.mailFetchTasksList.children.length, 2);
    assert.equal(elements.mailFetchTasksBadge.textContent, 1);
    assert.equal(elements.mailFetchProgress.dataset.state, 'completed');
    run.beginPage(2);
    assert.equal(elements.mailFetchProgress.dataset.state, 'completed');
    run.finish('completed', '先前任务已完成');
});
