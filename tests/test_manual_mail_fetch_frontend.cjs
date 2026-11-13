const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/js/index/05-emails.js'), 'utf8');
const helpers = source.slice(source.indexOf('function buildBrowserMailFetchError('), source.indexOf('function showBackgroundMailFetchErrorModal('));
const start = source.indexOf('async function fetchRecentEmails()');
const fetchRecent = source.slice(start, source.indexOf('// 刷新邮件', start));
const harness = String.raw`
const scenario = SCENARIO;
const window = {};
const document = { getElementById: () => null };
const toasts = [], statuses = [], modals = [], loading = [];
let isFetchingRecentEmails = false, mailboxViewSeq = 1;
let isTempEmailGroup = false, currentAccount = 'test@example.com';
let currentEmails = [], hasMoreEmails = false, requests = 0;
let aggregatedInboxGroupId = 18, currentGroupId = 18;
const requestOptions = [];
const EMAIL_FETCH_PAGE_SIZE = 10, AGGREGATED_INBOX_ACCOUNT_KEY = 'aggregated';
const isAggregatedInboxMode = () => scenario === 'aggregated_partial' || scenario === 'background_pages';
const getCurrentMailboxContext = () => ({
    account: currentAccount, folder: 'all', aggregated: isAggregatedInboxMode()
});
const isCurrentMailboxContext = () => true;
const persistEmailFetchTop = value => value;
const getEmailFetchTop = () => 20;
const getAggregatedInboxCacheAccountKey = () => 'aggregated';
const setEmailListLoadingState = value => loading.push(value);
const setMailSyncStatus = value => statuses.push(value);
const showToast = (message, type) => toasts.push({ message, type });
const showEmailFetchErrorModal = details => modals.push(details);
const isTimeoutAbortError = error => error.name === 'AbortError';
const buildAggregatedFetchErrorDetails = data => ({ account: data.account_errors[0].error });
async function fetchRemoteEmails(account, cacheKey, options) {
    requests++;
    requestOptions.push({folder:options.folder,groupId:options.groupId});
    if (scenario === 'background_pages' && requests === 1) {
        mailboxViewSeq++;
        currentAccount = 'other@example.com';
        aggregatedInboxGroupId = 99;
    }
    if (scenario === 'timeout' || scenario === 'stale_timeout') {
        if (scenario === 'stale_timeout') mailboxViewSeq++;
        throw Object.assign(new Error('request timed out'), { name: 'AbortError' });
    }
    if (scenario === 'network') throw new TypeError('Failed to fetch');
    if (scenario === 'failure' || (scenario === 'later_failure' && requests === 2)) {
        // The API failure branch in fetchRemoteEmails already opens the modal.
        showEmailFetchErrorModal({ graph: { code: 'EMAIL_FETCH_FAILED' } });
        return false;
    }
    const error = { code: 'EMAIL_FETCH_TIMEOUT', message: '收件箱获取超时' };
    currentEmails = Array.from({ length: 10 }, (_, id) => ({ id: scenario === 'background_pages' ? requests * 10 + id : id }));
    return {
        success: true,
        emails: currentEmails,
        has_more: scenario === 'later_failure' || scenario === 'partial' || (scenario === 'background_pages' && requests === 1),
        partial: scenario === 'partial' || scenario === 'aggregated_partial',
        details: { inbox: error },
        account_errors: [{ error }]
    };
}
HELPERS
FETCH_RECENT
fetchRecentEmails().then(() => console.log(JSON.stringify({
    toasts, statuses, modals, loading, requests, isFetchingRecentEmails,
    lastError: window._lastFetchErrorDetails, requestOptions
}))).catch(error => { console.error(error); process.exitCode = 1; });
`;
async function runFetch(scenario) {
    let result;
    const context = vm.createContext({
        console: { log: value => { result = JSON.parse(value); }, error: error => { throw error; } },
        process: { exitCode: 0 }
    });
    await vm.runInContext(harness.replace('SCENARIO', JSON.stringify(scenario)).replace('HELPERS', helpers).replace('FETCH_RECENT', fetchRecent), context);
    assert.equal(context.process.exitCode, 0);
    return result;
}
function assertFailed(result) {
    assert.equal(result.isFetchingRecentEmails, false);
    assert.deepEqual(result.loading, [true, false]);
    assert.equal(result.toasts.some(item => item.message.startsWith('已获取')), false);
}

test('manual browser timeout keeps error details without an inline status', async () => {
    const result = await runFetch('timeout');
    assertFailed(result);
    assert.equal(result.statuses.at(-1), '');
    assert.equal(result.toasts.at(-1).type, 'error');
    assert.equal(result.modals[0].browser.code, 'MAIL_NETWORK_TIMEOUT');
    assert.deepEqual(result.lastError, result.modals[0]);
});
test('manual network failure shows error details', async () => {
    const result = await runFetch('network');
    assertFailed(result);
    assert.equal(result.modals[0].browser.code, 'MAIL_NETWORK_FAILED');
});
for (const scenario of ['failure', 'later_failure']) {
    test(scenario + ' does not overwrite error with a success notice', async () => {
        const result = await runFetch(scenario);
        assertFailed(result);
        assert.equal(result.statuses.at(-1), '');
        assert.equal(result.modals.length, 1);
    });
}
for (const [scenario, detailKey] of [['partial', 'inbox'], ['aggregated_partial', 'account']]) {
    test(scenario + ' shows partial timeout details and stops paging', async () => {
        const result = await runFetch(scenario);
        assertFailed(result);
        assert.equal(result.requests, 1);
        assert.equal(result.toasts.at(-1).type, 'warning');
        assert.equal(result.modals[0][detailKey].code, 'EMAIL_FETCH_TIMEOUT');
    });
}
test('successful fetch still reports the received count', async () => {
    const result = await runFetch('success');
    assert.equal(result.statuses.at(-1), '');
    assert.equal(result.toasts.at(-1).message, '已获取 10 封邮件');
    assert.deepEqual(result.modals, []);
    assert.equal(result.isFetchingRecentEmails, false);
    assert.deepEqual(result.loading, [true, false]);
});
test('timeout after switching views does not show stale errors', async () => {
    const result = await runFetch('stale_timeout');
    assert.deepEqual(result.modals, []);
    assert.deepEqual(result.toasts, []);
});

test('background pagination continues for the original group after switching views', async () => {
    const result = await runFetch('background_pages');
    assert.equal(result.requests, 2);
    assert.deepEqual(result.requestOptions, [{folder:'all',groupId:18},{folder:'all',groupId:18}]);
    assert.deepEqual(result.modals, []);
    assert.deepEqual(result.toasts, []);
});

for (const aggregated of [false, true]) {
    test((aggregated ? 'aggregated' : 'single mailbox') + ' cached view clears spinner and old completion leaves a new refresh alone', async () => {
        const classes = new Set(), attributes = new Set();
        const button = { disabled: false, title: '',
            classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name) },
            toggleAttribute: (name, on) => on ? attributes.add(name) : attributes.delete(name)
        };
        const input = {}, tab = {};
        const replies = [];
        const context = vm.createContext({
            window: {}, document: {
                querySelector: () => button, querySelectorAll: () => [tab],
                getElementById: id => id === 'emailFetchTopInput' ? input : null
            },
            getEmailFetchTop: () => 10, persistEmailFetchTop: value => value,
            isAggregatedInboxMode: () => aggregated,
            getAggregatedInboxCacheAccountKey: () => 'aggregated',
            isCurrentMailboxContext: () => true,
            getCurrentMailboxContext: () => ({ account: 'test@example.com', folder: 'all', aggregated }),
            setMailSyncStatus() {}, showToast() {}, showEmailFetchErrorModal() {},
            fetchRemoteEmails: () => new Promise(resolve => replies.push(resolve))
        });
        const loading = source.slice(source.indexOf('function setEmailListLoadingState('), source.indexOf('function isCurrentMailboxContext('));
        const changeView = source.slice(source.indexOf('function beginMailboxViewChange('), source.indexOf('function coerceMailReadState('));
        vm.runInContext(`let isFetchingRecentEmails=false,mailboxViewSeq=1,currentEmailTotalCount=0;
            let isTempEmailGroup=false,currentAccount='test@example.com',currentGroupId=18,aggregatedInboxGroupId=18;
            const EMAIL_FETCH_PAGE_SIZE=50,AGGREGATED_INBOX_ACCOUNT_KEY='aggregated';
            ${helpers}\n${loading}\n${changeView}\n${fetchRecent}`, context);
        const oldRun = vm.runInContext('fetchRecentEmails()', context);
        assert.equal(classes.has('spinning'), true);
        assert.equal(attributes.has('aria-busy'), true);
        vm.runInContext('beginMailboxViewChange()', context);
        assert.equal(classes.has('spinning'), false);
        assert.equal(attributes.has('aria-busy'), false);
        assert.equal(button.disabled, false);
        assert.equal(input.disabled, false);
        assert.equal(tab.disabled, false);
        assert.equal(button.title, '获取最近 10 封');
        // The new mailbox is cached, so it performs no load/cleanup of its own.
        const newRun = vm.runInContext('fetchRecentEmails()', context);
        replies[0]({success:true,emails:[],has_more:false});
        await oldRun;
        assert.equal(classes.has('spinning'), true);
        assert.equal(button.disabled, true);
        replies[1]({success:true,emails:[],has_more:false});
        await newRun;
        assert.equal(classes.has('spinning'), false);
        assert.equal(attributes.has('aria-busy'), false);
        assert.equal(button.disabled, false);
    });
}

for (const [status, expectedCode] of [[504, 'MAIL_NETWORK_TIMEOUT'], [524, 'MAIL_NETWORK_TIMEOUT'], [502, 'MAIL_RESPONSE_INVALID']]) {
    test('HTML HTTP ' + status + ' response uses the real fetch path and opens error details', async () => {
        const modals = [];
        const list = { innerHTML: '' };
        const context = vm.createContext({
            currentFolder: 'all', EMAIL_LIST_REQUEST_TIMEOUT_MS: 130000,
            window: {}, document: { getElementById: () => list },
            isAggregatedInboxMode: () => false,
            resolveEmailListKeyword: () => '',
            getRemoteMailboxMethodFallback: () => 'graph',
            buildEmailListRequestUrl: () => '/api/emails/test@example.com',
            fetchWithTimeout: async () => ({
                status,
                json: async () => { throw new SyntaxError('Unexpected token <, HTML is not valid JSON'); }
            }),
            showEmailFetchErrorModal: details => modals.push(details),
            renderEmptyStateMarkup: () => '获取邮件失败，点击查看详情',
            handleApiError: () => assert.fail('Expected the structured error modal')
        });
        const start = source.indexOf('async function fetchRemoteEmails(');
        const remoteFetch = source.slice(start, source.indexOf('function startBackgroundRemoteMailboxSync(', start));
        const result = await vm.runInContext(remoteFetch + '\nfetchRemoteEmails("test@example.com", "test_all")', context);
        assert.equal(result, false);
        assert.equal(modals.length, 1);
        assert.equal(modals[0].error.status, status);
        assert.equal(modals[0].error.code, expectedCode);
        assert.match(modals[0].error.message, new RegExp('HTTP ' + status));
        assert.equal(context.window._lastFetchErrorDetails.error.status, status);
        assert.match(list.innerHTML, /获取邮件失败/);
    });
}

test('manual fetch submits a background job and waits for its final result', async () => {
    let requestedUrl, waitedId, applied = false;
    const context = vm.createContext({
        currentFolder: 'all', EMAIL_LIST_REQUEST_TIMEOUT_MS: 130000,
        isAggregatedInboxMode: () => false, resolveEmailListKeyword: () => '',
        getRemoteMailboxMethodFallback: () => 'graph',
        buildEmailListRequestUrl: () => '/api/emails/test@example.com?folder=all',
        fetchWithTimeout: async url => {
            requestedUrl = url;
            return { json: async () => ({ success: true, pending: true }) };
        },
        waitForManualMailFetch: async id => {
            waitedId = id;
            return { success: true, emails: [], has_more: false };
        },
        setMailSyncStatus: () => {},
        applyEmailListResponse: () => { applied = true; }
    });
    const start = source.indexOf('async function fetchRemoteEmails(');
    const remoteFetch = source.slice(start, source.indexOf('function startBackgroundRemoteMailboxSync(', start));
    const result = await vm.runInContext(remoteFetch + '\nfetchRemoteEmails("test@example.com", "test_all", {progressId:"abc"})', context);
    assert.match(requestedUrl, /progress_id=abc&async=1/);
    assert.equal(waitedId, 'abc');
    assert.equal(result.success, true);
    assert.equal(applied, true);
});
