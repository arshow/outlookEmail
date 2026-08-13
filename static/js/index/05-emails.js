        /* global AGGREGATED_INBOX_ACCOUNT_KEY, EMAIL_DETAIL_REQUEST_TIMEOUT_MS, EMAIL_LIST_REQUEST_TIMEOUT_MS, accountsCache, adjustAccountUnreadCount, adjustIframeHeight, aggregatedInboxGroupId, applyAccountUnreadCountsMap, applyEmailListCache, closeMobilePanels, closeNavbarActionsMenu, copyCurrentEmail, currentAccount, currentEmailDetail, currentEmailId, currentEmails, currentFolder, currentGroupId, currentMethod, currentSkip, emailListCache, escapeHtml, fetchWithTimeout, formatDate, getAggregatedInboxCacheAccountKey, getEmailListCacheEntry, getFolderDisplayName, getNextEmailSkipFromCache, handleApiError, hasMoreEmails, invalidateEmailListCache, isAggregatedInboxMode, isNormalMailLocalRetentionEnabled, isTempEmailGroup, isTimeoutAbortError, loadCloudflareGlobalMessages, mergeFolderSummaries, normalizeFolderSummaries, renderCloudflareGlobalFilterBar, renderColoredRemarkMarkup, renderEmptyStateMarkup, scheduleEmailListLoadCheck, showEmailFetchErrorModal, showMobileEmailDetail, showToast, updateMobileContext, updateModalBodyState */

        // ==================== 邮件相关 ====================

        function syncCachesAfterAggregatedFetch(data, options = {}) {
            if (!data || data.success !== true) {
                return;
            }

            const folder = String(options.folder || currentFolder || 'all').trim().toLowerCase() || 'all';
            const source = String(options.source || data.source || '').trim().toLowerCase();
            const isRemote = !['local', 'retained', 'cache'].includes(source);

            // 远程聚合刷新后失效各账号列表缓存，避免点进单账号仍读旧数据
            if (isRemote && Array.isArray(data.account_summaries)) {
                data.account_summaries.forEach(summary => {
                    if (!summary || summary.success === false) {
                        return;
                    }
                    const accountEmail = String(summary.account_email || '').trim();
                    if (accountEmail && typeof invalidateEmailListCache === 'function') {
                        invalidateEmailListCache(accountEmail, folder);
                    }
                });
            }

            if (typeof applyAccountUnreadCountsMap === 'function') {
                applyAccountUnreadCountsMap(data.unread_by_account, {
                    aggregatedTotal: data.unread_total
                });
            }
        }

        function isNormalMailboxListRequest() {
            return !isTempEmailGroup && currentMethod !== 'cloudflare-admin' && !isAggregatedInboxMode();
        }

        function getEmailAccountAddress(emailItem = {}) {
            return String(emailItem?.account_email || emailItem?.accountEmail || '').trim();
        }

        function getEmailSelectionKey(emailItem, fallbackFolder = currentFolder) {
            const stableKey = getEmailMessageStableKey(emailItem, fallbackFolder);
            if (!stableKey) {
                return '';
            }
            const accountId = emailItem?.account_id != null && emailItem?.account_id !== ''
                ? String(emailItem.account_id)
                : '';
            const accountEmail = getEmailAccountAddress(emailItem).toLowerCase();
            const accountPart = accountId || accountEmail || (isAggregatedInboxMode() ? '' : String(currentAccount || ''));
            return accountPart ? `${accountPart}::${stableKey}` : stableKey;
        }

        const backgroundMailboxSyncs = new Map();
        const pendingNewMailSyncs = new Map();
        const BACKGROUND_MAIL_ERROR_MODAL_COOLDOWN_MS = 5 * 60 * 1000;
        let lastBackgroundMailErrorModal = { key: '', shownAt: 0 };
        const normalDetailIframeResizeResources = { timers: [], observer: null };
        const fullscreenIframeResizeResources = { timers: [], observer: null };
        const NEW_EMAIL_HIGHLIGHT_CLEAR_DELAY_MS = 3500;

        function cleanupIframeResizeResources(resources) {
            (resources.timers || []).forEach(timerId => window.clearTimeout(timerId));
            resources.timers = [];
            if (resources.observer) {
                resources.observer.disconnect();
                resources.observer = null;
            }
        }

        function cleanupNormalDetailIframeResizeResources() {
            cleanupIframeResizeResources(normalDetailIframeResizeResources);
        }

        function cleanupFullscreenIframeResizeResources() {
            cleanupIframeResizeResources(fullscreenIframeResizeResources);
        }

        function getNormalMailboxRemoteMethod() {
            const cacheMethod = getEmailListCacheEntry(currentAccount, currentFolder)?.remote_method;
            return cacheMethod || currentMethod;
        }

        function getRemoteMailboxMethodFallback() {
            const method = String(getNormalMailboxRemoteMethod() || '').trim().toLowerCase();
            return ['graph', 'imap'].includes(method) ? method : 'graph';
        }

        function getCurrentEmailRemoteActionMethod(emailItem = {}) {
            const idMode = String(emailItem?.id_mode || emailItem?.idMode || '').trim().toLowerCase();
            if (idMode === 'graph') {
                return 'graph';
            }
            if (idMode === 'uid' || idMode === 'sequence') {
                return 'imap';
            }
            return getRemoteMailboxMethodFallback();
        }

        function buildEmailListRequestUrl(email, params = {}) {
            const query = typeof buildMailFolderListParams === 'function'
                ? buildMailFolderListParams(params)
                : new URLSearchParams(params);
            return `/api/emails/${encodeURIComponent(email)}?${query.toString()}`;
        }

        function setEmailListLoadingState(isLoading, options = {}) {
            const refreshBtn = document.querySelector('.refresh-btn');
            const folderTabs = document.querySelectorAll('.folder-tab');
            const isBackgroundSync = options.background === true;

            if (refreshBtn) {
                refreshBtn.disabled = isLoading && !isBackgroundSync;
                refreshBtn.classList.toggle('spinning', isLoading);
                refreshBtn.title = isLoading
                    ? (isBackgroundSync ? '本地保留邮件已显示，正在后台同步远程邮件' : '正在获取邮件...')
                    : `获取最近 ${getEmailFetchTop()} 封`;
                refreshBtn.toggleAttribute('aria-busy', isLoading);
            }
            const fetchTopInput = document.getElementById('emailFetchTopInput');
            if (fetchTopInput) {
                fetchTopInput.disabled = isLoading && !isBackgroundSync;
            }
            folderTabs.forEach(tab => {
                tab.disabled = isLoading && !isBackgroundSync;
                tab.title = isLoading && isBackgroundSync
                    ? '本地保留邮件已显示，后台同步进行中'
                    : '';
            });
        }

        function isCurrentMailboxContext(context) {
            if (!context) {
                return false;
            }
            const contextFolder = String(context.folder || '').trim() || 'all';
            const currentViewFolder = String(currentFolder || '').trim() || 'all';
            if (contextFolder !== currentViewFolder) {
                return false;
            }
            if (context.aggregated === true || context.account === AGGREGATED_INBOX_ACCOUNT_KEY) {
                return isAggregatedInboxMode();
            }
            return !isAggregatedInboxMode() && currentAccount === context.account;
        }

        function getCurrentMailboxContext() {
            return {
                account: isAggregatedInboxMode() ? AGGREGATED_INBOX_ACCOUNT_KEY : currentAccount,
                folder: currentFolder,
                aggregated: isAggregatedInboxMode()
            };
        }

        function beginMailboxViewChange() {
            mailboxViewSeq += 1;
            isFetchingRecentEmails = false;
            if (typeof setMailSyncStatus === 'function') {
                setMailSyncStatus('');
            }
        }

        function coerceMailReadState(value) {
            if (value === false || value === 0 || value === '0') {
                return false;
            }
            if (typeof value === 'string') {
                const normalized = value.trim().toLowerCase();
                if (['false', 'no', 'off', 'unread', ''].includes(normalized)) {
                    return false;
                }
                if (['true', 'yes', 'on', '1', 'read'].includes(normalized)) {
                    return true;
                }
                return false;
            }
            if (value == null) {
                return null;
            }
            return Boolean(value);
        }

        function isEmailUnread(email) {
            if (!email || typeof email !== 'object') {
                return false;
            }
            const raw = Object.prototype.hasOwnProperty.call(email, 'is_read')
                ? email.is_read
                : email.isRead;
            return coerceMailReadState(raw) === false;
        }

        function coerceMailFlagState(value) {
            if (value && typeof value === 'object') {
                const status = String(value.flagStatus || value.flag_status || '').trim().toLowerCase();
                return status === 'flagged';
            }
            if (value === true || value === 1 || value === '1') {
                return true;
            }
            if (typeof value === 'string') {
                const normalized = value.trim().toLowerCase();
                return ['true', 'yes', 'on', 'flagged', 'flag'].includes(normalized);
            }
            return false;
        }

        function isEmailFlagged(email) {
            if (!email || typeof email !== 'object') {
                return false;
            }
            if (Object.prototype.hasOwnProperty.call(email, 'is_flagged')) {
                return coerceMailFlagState(email.is_flagged);
            }
            if (Object.prototype.hasOwnProperty.call(email, 'isFlagged')) {
                return coerceMailFlagState(email.isFlagged);
            }
            if (email.flag != null) {
                return coerceMailFlagState(email.flag);
            }
            return false;
        }

        function normalizeEmailListItems(emails) {
            return (Array.isArray(emails) ? emails : []).map(email => {
                if (!email || typeof email !== 'object') {
                    return email;
                }
                const rawRead = Object.prototype.hasOwnProperty.call(email, 'is_read')
                    ? email.is_read
                    : email.isRead;
                if (rawRead !== undefined) {
                    email.is_read = coerceMailReadState(rawRead) === true;
                }
                const rawFlag = Object.prototype.hasOwnProperty.call(email, 'is_flagged')
                    ? email.is_flagged
                    : (Object.prototype.hasOwnProperty.call(email, 'isFlagged')
                        ? email.isFlagged
                        : email.flag);
                if (rawFlag !== undefined) {
                    email.is_flagged = coerceMailFlagState(rawFlag);
                }
                return email;
            });
        }

        let statusFilterOverrideEmails = null;
        let statusFilterHydrateSeq = 0;

        function clearEmailStatusFilterOverride() {
            statusFilterOverrideEmails = null;
        }

        let currentEmailKeyword = '';
        const EMAIL_FETCH_PAGE_SIZE = 50;
        const EMAIL_FETCH_TOP_DEFAULT = 500;
        const EMAIL_FETCH_TOP_MIN = 1;
        const EMAIL_FETCH_TOP_MAX = 2000;
        const EMAIL_FETCH_TOP_STORAGE_KEY = 'emailFetchTop';
        let isFetchingRecentEmails = false;
        let mailboxViewSeq = 0;

        function getEmailSearchKeyword() {
            return String(currentEmailKeyword || '').trim().toLowerCase();
        }

        function emailMatchesKeyword(email, keyword = getEmailSearchKeyword()) {
            const needle = String(keyword || '').trim().toLowerCase();
            if (!needle) {
                return true;
            }
            const haystack = [
                email?.subject,
                email?.from,
                email?.to,
                email?.body_preview,
                email?.account_email,
                email?.accountEmail
            ].map(value => String(value || '').toLowerCase()).join('\n');
            return haystack.includes(needle);
        }

        function resolveEmailListKeyword(options = {}) {
            if (Object.prototype.hasOwnProperty.call(options, 'keyword')) {
                return String(options.keyword || '').trim();
            }
            return getEmailSearchKeyword();
        }

        function getVisibleEmailsForCurrentFilter(emails = currentEmails) {
            let list = normalizeEmailListItems(Array.isArray(emails) ? emails : []);
            const filter = String(currentEmailStatusFilter || 'all').trim().toLowerCase();
            if (filter === 'unread') {
                list = list.filter(email => isEmailUnread(email));
            } else if (filter === 'read') {
                list = list.filter(email => !isEmailUnread(email));
            } else if (filter === 'flagged') {
                list = list.filter(email => isEmailFlagged(email));
            }
            const keyword = getEmailSearchKeyword();
            if (keyword) {
                list = list.filter(email => emailMatchesKeyword(email, keyword));
            }
            return list;
        }

        async function hydrateEmailStatusFilterIfNeeded() {
            const filter = String(currentEmailStatusFilter || 'all').trim().toLowerCase();
            if (filter === 'all' || isTempEmailGroup || currentMethod === 'cloudflare-admin') {
                statusFilterOverrideEmails = null;
                return false;
            }
            if (getVisibleEmailsForCurrentFilter(currentEmails).length > 0) {
                statusFilterOverrideEmails = null;
                return false;
            }
            if (typeof isNormalMailLocalRetentionEnabled !== 'function' || !isNormalMailLocalRetentionEnabled()) {
                return false;
            }
            if (!isAggregatedInboxMode() && !currentAccount) {
                return false;
            }

            const seq = ++statusFilterHydrateSeq;
            const account = currentAccount;
            const folder = currentFolder;
            try {
                const url = isAggregatedInboxMode()
                    ? buildAggregatedEmailsUrl({
                        source: 'local',
                        folder,
                        skip: 0,
                        top: 100,
                        status: filter,
                        keyword: getEmailSearchKeyword()
                    })
                    : buildEmailListRequestUrl(account, {
                        source: 'local',
                        folder,
                        skip: 0,
                        top: 100,
                        status: filter,
                        keyword: getEmailSearchKeyword()
                    });
                const response = await fetchWithTimeout(url, {
                    timeoutMs: EMAIL_LIST_REQUEST_TIMEOUT_MS,
                    timeoutMessage: '读取本地筛选邮件超时'
                });
                const data = await response.json();
                if (seq !== statusFilterHydrateSeq) {
                    return false;
                }
                if (
                    currentEmailStatusFilter !== filter
                    || currentAccount !== account
                    || currentFolder !== folder
                ) {
                    return false;
                }
                const emails = normalizeEmailListItems(data.emails);
                if (!data.success || !emails.length) {
                    return false;
                }
                statusFilterOverrideEmails = emails;
                renderEmailList(currentEmails);
                const emailCount = document.getElementById('emailCount');
                if (emailCount) {
                    emailCount.textContent = `(${getVisibleEmailsForCurrentFilter(emails).length})`;
                }
                return true;
            } catch (error) {
                return false;
            }
        }

        function updateEmailListHeader(methodLabel, emailCount) {
            const methodTag = document.getElementById('methodTag');
            if (methodTag) {
                methodTag.textContent = methodLabel;
                methodTag.style.display = 'inline';
            }

            const emailCountEl = document.getElementById('emailCount');
            if (emailCountEl) {
                const visibleCount = Array.isArray(currentEmails)
                    ? getVisibleEmailsForCurrentFilter(currentEmails).length
                    : Number(emailCount) || 0;
                emailCountEl.textContent = `(${visibleCount})`;
            }
        }

        function setMailSyncStatus(message = '') {
            const status = document.getElementById('mailSyncStatus');
            if (!status) {
                return;
            }

            status.textContent = message;
            status.hidden = !message;
        }

        function getEmailListMethodMetadata(data, options = {}) {
            const requestMethod = String(data.request_method || '').trim().toLowerCase();
            const optionMethod = String(options.method || '').trim().toLowerCase();
            let method = requestMethod || optionMethod;
            if (!method) {
                if (options.aggregated === true || String(data.method || '').toLowerCase().includes('aggregated')) {
                    method = 'aggregated';
                } else if (data.method === 'Graph API') {
                    method = 'graph';
                } else {
                    method = 'imap';
                }
            }
            return {
                method,
                remoteMethod: method === 'local' || method === 'aggregated'
                    ? getRemoteMailboxMethodFallback()
                    : method,
                methodLabel: options.methodLabel || data.method || method,
                disableLoadMore: options.disableLoadMore === true
            };
        }

        function getEmailMessageStableKey(emailItem, fallbackFolder = currentFolder) {
            const id = String(emailItem?.id || '').trim();
            if (!id) {
                return '';
            }

            const folder = String(emailItem?.folder || fallbackFolder || '').trim().toLowerCase();
            const idMode = String(emailItem?.id_mode || emailItem?.idMode || '').trim().toLowerCase();
            return `${folder}::${idMode}::${id}`;
        }

        function buildAggregatedEmailsUrl({ skip = 0, top = 20, folder = currentFolder, source = '', status = '', keyword = '' } = {}) {
            const safeFolder = ['all', 'inbox', 'junkemail'].includes(String(folder || '').toLowerCase())
                ? String(folder || 'all')
                : 'all';
            const query = new URLSearchParams({
                group_id: String(aggregatedInboxGroupId || currentGroupId || ''),
                folder: safeFolder,
                skip: String(Math.max(0, Number(skip) || 0)),
                top: String(Math.max(0, Number(top) || 20))
            });
            if (source) {
                query.set('source', String(source));
            }
            const statusName = String(status || '').trim().toLowerCase();
            if (statusName && statusName !== 'all') {
                query.set('status', statusName);
            }
            const keywordValue = String(keyword || '').trim();
            if (keywordValue) {
                query.set('keyword', keywordValue);
            }
            return `/api/emails/aggregated?${query.toString()}`;
        }

        function buildAggregatedFetchErrorDetails(data) {
            const details = {};
            const accountErrors = Array.isArray(data?.account_errors) ? data.account_errors : [];
            accountErrors.forEach((item, index) => {
                const key = String(item?.account_email || item?.account_id || `account_${index + 1}`);
                details[key] = item?.error || item || '获取邮件失败';
            });
            if (data?.error) {
                details.error = data.error;
            }
            if (!Object.keys(details).length && data?.details) {
                return data.details;
            }
            return details;
        }

        function getEmailListTimestamp(emailItem) {
            const timestamp = Date.parse(emailItem?.date || emailItem?.received_at || '');
            return Number.isNaN(timestamp) ? 0 : timestamp;
        }

        function mergeEmailListByStableKey(existingEmails, incomingEmails, fallbackFolder = currentFolder) {
            const mergedEmails = [];
            const indexByKey = new Map();
            const newEmails = [];

            (existingEmails || []).forEach(emailItem => {
                const key = getEmailMessageStableKey(emailItem, fallbackFolder);
                if (key) {
                    indexByKey.set(key, mergedEmails.length);
                }
                mergedEmails.push(emailItem);
            });

            (incomingEmails || []).forEach(emailItem => {
                const key = getEmailMessageStableKey(emailItem, fallbackFolder);
                if (key && indexByKey.has(key)) {
                    const index = indexByKey.get(key);
                    mergedEmails[index] = { ...mergedEmails[index], ...emailItem };
                    return;
                }

                if (key) {
                    indexByKey.set(key, mergedEmails.length);
                }
                mergedEmails.push(emailItem);
                newEmails.push(emailItem);
            });

            mergedEmails.sort((left, right) => getEmailListTimestamp(right) - getEmailListTimestamp(left));
            return { emails: mergedEmails, newEmails };
        }

        function cacheEmailListResponse(cacheKey, data, method, methodLabel, options = {}) {
            const emails = Array.isArray(data.emails) ? data.emails : [];
            const disableLoadMore = options.disableLoadMore === true;
            const folderSummaries = options.folderSummaries || data.folder_summaries;
            const requestSkip = Number(options.requestSkip);
            const requestTop = Number(options.requestTop);
            const nextSkip = options.aggregated === true
                ? (Number.isFinite(requestSkip) ? requestSkip : 0) + (Number.isFinite(requestTop) ? requestTop : 20)
                : emails.length;
            emailListCache[cacheKey] = {
                emails,
                has_more: disableLoadMore ? false : data.has_more === true,
                skip: nextSkip,
                method,
                method_label: methodLabel,
                derived_from: null,
                local_retention: data.local_retention === true,
                local_retention_count: Number(data.count) || emails.length,
                folder_summaries: currentFolder === 'all'
                    ? normalizeFolderSummaries(folderSummaries)
                    : undefined
            };

            if (options.remoteMethod) {
                emailListCache[cacheKey].remote_method = options.remoteMethod;
            }
        }

        function applyEmailListResponse(cacheKey, data, options = {}) {
            const emails = normalizeEmailListItems(data.emails);
            const { method, remoteMethod, methodLabel, disableLoadMore } = getEmailListMethodMetadata(data, options);

            currentEmails = emails;
            currentMethod = method;
            hasMoreEmails = disableLoadMore ? false : data.has_more === true;
            if (options.aggregated === true) {
                const requestSkip = Number(options.requestSkip);
                const requestTop = Number(options.requestTop);
                currentSkip = (Number.isFinite(requestSkip) ? requestSkip : 0)
                    + (Number.isFinite(requestTop) ? requestTop : 20);
            } else {
                currentSkip = currentEmails.length;
            }

            cacheEmailListResponse(cacheKey, data, method, methodLabel, {
                disableLoadMore,
                remoteMethod,
                aggregated: options.aggregated === true,
                requestSkip: options.requestSkip,
                requestTop: options.requestTop
            });
            updateEmailListHeader(methodLabel, currentEmails.length);
            renderEmailList(currentEmails);
            scheduleEmailListLoadCheck(80);
        }

        function getPendingNewMailSyncKey(account = currentAccount, folder = currentFolder) {
            return `${account || ''}_${folder || 'all'}`;
        }

        function hasPendingNewMailSync(account = currentAccount, folder = currentFolder) {
            return pendingNewMailSyncs.has(getPendingNewMailSyncKey(account, folder));
        }

        function updateEmailListCacheRemoteMetadata(cacheKey, data, options = {}) {
            const existingCache = emailListCache[cacheKey];
            if (!existingCache) {
                return;
            }

            const { method, remoteMethod } = getEmailListMethodMetadata(data, options);
            existingCache.remote_method = remoteMethod || method;
            if (currentFolder === 'all' && data.folder_summaries) {
                existingCache.folder_summaries = mergeFolderSummaries(
                    existingCache.folder_summaries,
                    data.folder_summaries
                );
            }
        }

        function queuePendingNewMailSync(syncKey, cacheKey, data, options = {}) {
            const existingEmails = Array.isArray(currentEmails) ? currentEmails : [];
            const incomingEmails = Array.isArray(data.emails) ? data.emails : [];
            const mergedResult = mergeEmailListByStableKey(existingEmails, incomingEmails, options.folder);
            const newlySyncedRows = collectNewlySyncedEmailRows(data, mergedResult, options.folder);

            updateEmailListCacheRemoteMetadata(cacheKey, data, options);
            pendingNewMailSyncs.set(syncKey, {
                cacheKey,
                data,
                options: { ...options },
                newlySyncedRows
            });
            announceNewlySyncedEmailRows(data, newlySyncedRows, options.folder, syncKey);
            return mergedResult;
        }

        let inboxDiscoveryEventSource = null;

        function shouldAcceptInboxDiscoveryEvent(event = {}) {
            if (isTempEmailGroup || !currentAccount) {
                return false;
            }
            if (String(currentFolder || '').toLowerCase() === 'junkemail') {
                return false;
            }
            const accountEmail = String(event.account_email || '').trim().toLowerCase();
            if (!accountEmail) {
                return false;
            }
            if (isAggregatedInboxMode()) {
                return true;
            }
            return accountEmail === String(currentAccount || '').trim().toLowerCase();
        }

        function handleInboxDiscoveryNewMailEvent(event = {}) {
            if (!shouldAcceptInboxDiscoveryEvent(event)) {
                return;
            }
            const newCount = Number(event.new_count || 0);
            const emails = Array.isArray(event.emails) ? event.emails : [];
            if (newCount <= 0 || !emails.length) {
                return;
            }

            const accountEmail = String(event.account_email || '').trim();
            const folder = String(event.folder || currentFolder || 'inbox').toLowerCase() || 'inbox';
            const viewAccount = isAggregatedInboxMode()
                ? getAggregatedInboxCacheAccountKey()
                : currentAccount;
            const syncKey = getPendingNewMailSyncKey(viewAccount, currentFolder || 'all');
            const cacheKey = `${viewAccount}_${currentFolder || 'all'}`;
            const payload = {
                success: true,
                emails,
                new_count: newCount,
                new_message_ids: event.new_message_ids || [],
                has_more: hasMoreEmails === true,
                method: isAggregatedInboxMode() ? 'aggregated' : (currentMethod || 'local'),
                request_method: isAggregatedInboxMode() ? 'aggregated' : 'local',
                local_retention: true,
            };

            queuePendingNewMailSync(syncKey, cacheKey, payload, {
                folder: currentFolder || folder,
                context: {
                    account: viewAccount,
                    folder: currentFolder || 'all',
                    account_email: accountEmail,
                },
                announceNewRows: true,
                aggregated: isAggregatedInboxMode(),
            });
        }

        function stopInboxDiscoveryEventSource() {
            if (inboxDiscoveryEventSource) {
                try {
                    inboxDiscoveryEventSource.close();
                } catch (error) {
                    // ignore close errors
                }
                inboxDiscoveryEventSource = null;
            }
        }

        function startInboxDiscoveryEventSource() {
            if (typeof EventSource === 'undefined') {
                return;
            }
            if (!isNormalMailLocalRetentionEnabled()) {
                stopInboxDiscoveryEventSource();
                return;
            }
            if (inboxDiscoveryEventSource) {
                return;
            }

            const source = new EventSource('/api/emails/inbox-discovery/events');
            inboxDiscoveryEventSource = source;
            source.onmessage = (messageEvent) => {
                let payload = null;
                try {
                    payload = JSON.parse(messageEvent.data || '{}');
                } catch (error) {
                    return;
                }
                if (!payload || typeof payload !== 'object') {
                    return;
                }
                if (payload.type === 'new_mail') {
                    handleInboxDiscoveryNewMailEvent(payload);
                }
            };
            source.onerror = () => {
                // 浏览器会自动重连；保留引用即可
            };
        }

        function syncInboxDiscoveryEventSource() {
            if (isNormalMailLocalRetentionEnabled()) {
                startInboxDiscoveryEventSource();
            } else {
                stopInboxDiscoveryEventSource();
            }
        }


        function applyPendingNewMailSync(syncKey = getPendingNewMailSyncKey()) {
            const pending = pendingNewMailSyncs.get(syncKey);
            if (!pending) {
                hideNewMailNotice();
                return false;
            }

            const listElement = document.getElementById('emailList');
            const previousScrollTop = listElement ? listElement.scrollTop : null;
            const mergeResult = mergeEmailListByStableKey(
                Array.isArray(currentEmails) ? currentEmails : [],
                pending.data.emails,
                pending.options.folder
            );
            const newlySyncedRows = collectNewlySyncedEmailRows(
                pending.data,
                mergeResult,
                pending.options.folder
            );
            const { method, remoteMethod, methodLabel } = getEmailListMethodMetadata(
                pending.data,
                pending.options
            );
            markNewlySyncedEmailRows(newlySyncedRows, pending.options.folder);
            currentEmails = mergeResult.emails;
            currentMethod = method;
            hasMoreEmails = pending.data.has_more === true;
            currentSkip = currentEmails.length;

            cacheEmailListResponse(pending.cacheKey, { ...pending.data, emails: currentEmails }, method, methodLabel, {
                remoteMethod
            });
            updateEmailListHeader(methodLabel, currentEmails.length);
            renderEmailList(currentEmails);
            if (previousScrollTop !== null) {
                const currentListElement = document.getElementById('emailList');
                if (currentListElement) {
                    currentListElement.scrollTop = previousScrollTop;
                }
            }
            scheduleEmailListLoadCheck(80);
            requestBodyRetentionForNewRows(newlySyncedRows, pending.options.folder);
            if (newlySyncedRows.length > 0) {
                scheduleNewEmailHighlightClear();
            }
            pendingNewMailSyncs.delete(syncKey);
            hideNewMailNotice();
            return true;
        }


        function applyMergedRemoteEmailSync(cacheKey, data, options = {}) {
            const syncKey = getPendingNewMailSyncKey(options.context?.account, options.context?.folder);
            if (options.announceNewRows === true && Number(data.new_count || 0) > 0) {
                return queuePendingNewMailSync(syncKey, cacheKey, data, options);
            }

            pendingNewMailSyncs.delete(syncKey);
            hideNewMailNotice();
            const mergedResult = mergeEmailListByStableKey(currentEmails, data.emails, options.folder);
            const { method, remoteMethod, methodLabel } = getEmailListMethodMetadata(data, options);
            currentEmails = mergedResult.emails;
            currentMethod = method;
            hasMoreEmails = data.has_more === true;
            currentSkip = currentEmails.length;
            cacheEmailListResponse(cacheKey, { ...data, emails: currentEmails }, method, methodLabel, { remoteMethod });
            updateEmailListHeader(methodLabel, currentEmails.length);
            renderEmailList(currentEmails);
            scheduleEmailListLoadCheck(80);
            return mergedResult;
        }

        async function tryRenderLocalRetainedEmails(email, cacheKey) {
            if (!isNormalMailLocalRetentionEnabled()) {
                setMailSyncStatus('本地存储未启用');
                return false;
            }
            try {
                const response = await fetchWithTimeout(
                    buildEmailListRequestUrl(email, {
                        source: 'local',
                        folder: currentFolder,
                        skip: 0,
                        top: 20,
                        keyword: resolveEmailListKeyword()
                    }),
                    {
                        timeoutMs: EMAIL_LIST_REQUEST_TIMEOUT_MS,
                        timeoutMessage: '读取本地保留邮件超时'
                    }
                );
                const data = await response.json();
                const retainedEmails = Array.isArray(data.emails) ? data.emails : [];
                if (!data.success || retainedEmails.length === 0) {
                    return false;
                }

                applyEmailListResponse(cacheKey, data, {
                    method: 'local',
                    methodLabel: data.method || 'Local Retention'
                });
                return true;
            } catch (error) {
                return false;
            }
        }

        function buildBrowserMailFetchError(error) {
            const isTimeout = isTimeoutAbortError(error);
            const message = isTimeout
                ? '网络连接超时：邮件服务未在规定时间内响应，请检查网络、代理和服务地址'
                : '网络连接失败：浏览器无法连接邮件接口，请检查当前网络、代理和服务是否正常';
            return {
                code: isTimeout ? 'MAIL_NETWORK_TIMEOUT' : 'MAIL_NETWORK_FAILED',
                message,
                type: error?.name || 'NetworkError',
                status: isTimeout ? 504 : 0,
                category: 'network',
                details: error?.message || String(error || ''),
                trace_id: '-'
            };
        }

        function getFetchErrorMessage(error) {
            return buildBrowserMailFetchError(error).message;
        }

        function showBackgroundMailFetchErrorModal(context, details) {
            const errorFingerprint = Object.entries(details || {}).map(([method, error]) => {
                const value = error && typeof error === 'object'
                    ? (error.reason_code || error.code || error.type || error.message || 'unknown')
                    : String(error || 'unknown');
                return `${method}:${value}`;
            }).sort().join('|');
            const key = `${context?.account || ''}_${context?.folder || ''}:${errorFingerprint}`;
            const now = Date.now();
            if (
                key === lastBackgroundMailErrorModal.key
                && now - lastBackgroundMailErrorModal.shownAt < BACKGROUND_MAIL_ERROR_MODAL_COOLDOWN_MS
            ) {
                return;
            }

            lastBackgroundMailErrorModal = { key, shownAt: now };
            showEmailFetchErrorModal(details);
        }

        async function fetchRemoteEmails(email, cacheKey, options = {}) {
            const requestFolder = options.folder || currentFolder;
            const requestSkip = Number.isFinite(Number(options.skip)) ? Number(options.skip) : 0;
            const requestTop = Number.isFinite(Number(options.top)) ? Number(options.top) : 20;
            const requestKeyword = resolveEmailListKeyword(options);
            const aggregated = options.aggregated === true || isAggregatedInboxMode();
            const response = await fetchWithTimeout(
                aggregated
                    ? buildAggregatedEmailsUrl({
                        skip: requestSkip,
                        top: requestTop,
                        folder: requestFolder,
                        source: options.source || '',
                        keyword: requestKeyword
                    })
                    : buildEmailListRequestUrl(email, {
                        method: options.method || getRemoteMailboxMethodFallback(),
                        folder: requestFolder,
                        skip: requestSkip,
                        top: requestTop,
                        keyword: requestKeyword
                    }),
                {
                    timeoutMs: EMAIL_LIST_REQUEST_TIMEOUT_MS,
                    timeoutMessage: '获取邮件超时，请检查网络、代理或账号配置后重试'
                }
            );
            const data = await response.json();

            if (data.success) {
                if (!options.context || isCurrentMailboxContext(options.context)) {
                    if (options.keepSyncStatus !== true) {
                        setMailSyncStatus('');
                    }
                    if (options.mergeWithCurrentList === true) {
                        applyMergedRemoteEmailSync(cacheKey, data, options);
                    } else {
                        applyEmailListResponse(cacheKey, data, {
                            ...options,
                            aggregated,
                            requestSkip,
                            requestTop,
                            method: aggregated ? 'aggregated' : options.method,
                            methodLabel: aggregated
                                ? (data.method || '聚合')
                                : options.methodLabel
                        });
                        if (aggregated && data.partial && Array.isArray(data.account_errors) && data.account_errors.length) {
                            showToast(`部分账号拉取失败（${data.account_errors.length}）`, 'warning');
                        }
                    }
                    if (aggregated) {
                        syncCachesAfterAggregatedFetch(data, {
                            folder: requestFolder,
                            source: options.source || ''
                        });
                    }
                }
                return data;
            }

            const fetchErrorDetails = aggregated
                ? buildAggregatedFetchErrorDetails(data)
                : (data.details || (data.error ? { error: data.error } : {}));
            if (options.preserveCurrentListOnError === true) {
                window._lastFetchErrorDetails = fetchErrorDetails;
                if (!options.context || isCurrentMailboxContext(options.context)) {
                    const errorMessage = data.error?.message
                        || (typeof data.error === 'string' ? data.error : '')
                        || '后台同步失败，已保留本地邮件列表';
                    setMailSyncStatus(`后台同步失败：${errorMessage}`);
                    showToast(errorMessage, 'error');
                    showBackgroundMailFetchErrorModal(options.context, fetchErrorDetails);
                }
                return false;
            }

            if (Object.keys(fetchErrorDetails).length > 0) {
                showEmailFetchErrorModal(fetchErrorDetails);
            } else {
                handleApiError(data, '获取邮件失败');
            }
            document.getElementById('emailList').innerHTML = renderEmptyStateMarkup(
                '⚠️',
                '获取邮件失败，<a href="javascript:void(0)" onclick="showEmailFetchErrorModal(window._lastFetchErrorDetails)" style="color:#409eff;text-decoration:underline;">点击查看详情</a>',
                {
                    allowHtml: true,
                    onAction: 'refreshEmails()',
                    actionTitle: '刷新邮件列表'
                }
            );
            window._lastFetchErrorDetails = fetchErrorDetails;
            return false;
        }

        function startBackgroundRemoteMailboxSync(email, cacheKey) {
            const context = {
                account: email,
                folder: currentFolder
            };
            const syncKey = `${context.account}_${context.folder}`;
            if (backgroundMailboxSyncs.has(syncKey)) {
                return;
            }

            setEmailListLoadingState(true, { background: true });
            const syncPromise = fetchRemoteEmails(email, cacheKey, {
                folder: context.folder,
                method: getRemoteMailboxMethodFallback(),
                context,
                mergeWithCurrentList: true,
                announceNewRows: true,
                preserveCurrentListOnError: true
            }).catch(error => {
                if (isCurrentMailboxContext(context)) {
                    const browserError = buildBrowserMailFetchError(error);
                    const errorMessage = getFetchErrorMessage(error);
                    setMailSyncStatus(`后台同步失败：${errorMessage}`);
                    showToast(errorMessage, 'error');
                    showBackgroundMailFetchErrorModal(context, { browser: browserError });
                }
            }).finally(() => {
                backgroundMailboxSyncs.delete(syncKey);
                if (isCurrentMailboxContext(context)) {
                    setEmailListLoadingState(false);
                }
            });
            backgroundMailboxSyncs.set(syncKey, syncPromise);
        }

        // 加载邮件列表
        async function loadEmails(email, forceRefresh = false) {
            const container = document.getElementById('emailList');
            const aggregated = isAggregatedInboxMode() || email === AGGREGATED_INBOX_ACCOUNT_KEY;
            const context = {
                account: aggregated ? AGGREGATED_INBOX_ACCOUNT_KEY : email,
                folder: currentFolder,
                aggregated
            };
            const cacheAccountKey = aggregated
                ? getAggregatedInboxCacheAccountKey()
                : email;

            // 切换账号/刷新时清除选中状态
            selectedEmailIds.clear();
            updateEmailBatchActionBar();
            hideNewMailNotice();
            pendingNewMailSyncs.delete(getPendingNewMailSyncKey(cacheAccountKey, currentFolder));
            setMailSyncStatus('');

            const cacheKey = `${cacheAccountKey}_${currentFolder}`;
            const cache = !forceRefresh ? getEmailListCacheEntry(cacheAccountKey, currentFolder) : null;
            if (cache) {
                applyEmailListCache(cache, { scheduleLoadCheck: false });
                return;
            }

            setEmailListLoadingState(true);
            currentSkip = 0;
            hasMoreEmails = true;
            container.innerHTML = '<div class="loading"><div class="loading-spinner"></div></div>';

            try {
                // 非强制刷新：单账号/聚合都只读本地（或空着等手动刷新），不自动打远程
                if (!forceRefresh) {
                    if (aggregated) {
                        if (isNormalMailLocalRetentionEnabled()) {
                            const localData = await fetchRemoteEmails(email, cacheKey, {
                                aggregated: true,
                                skip: 0,
                                top: 20,
                                source: 'local',
                                method: 'aggregated',
                                methodLabel: 'Local Retention',
                                context
                            });
                            if (!isCurrentMailboxContext(context)) {
                                return;
                            }
                            if (!localData || !localData.success || !(localData.emails || []).length) {
                                currentEmails = [];
                                currentMethod = 'aggregated';
                                hasMoreEmails = false;
                                currentSkip = 0;
                                setMailSyncStatus('本地暂无聚合缓存，可点击刷新从远程获取');
                                container.innerHTML = renderEmptyStateMarkup(
                                    '📭',
                                    '本地暂无聚合邮件缓存，请点击右上角刷新从远程获取',
                                    {
                                        onAction: 'refreshEmails()',
                                        actionTitle: '从远程刷新'
                                    }
                                );
                                updateEmailListHeader('Local', 0);
                            }
                        } else {
                            currentEmails = [];
                            currentMethod = 'aggregated';
                            hasMoreEmails = false;
                            currentSkip = 0;
                            setMailSyncStatus('未开启本地保留，请手动刷新远程聚合');
                            container.innerHTML = renderEmptyStateMarkup(
                                '📭',
                                '聚合收件箱不会自动拉远程，请点击右上角刷新',
                                {
                                    onAction: 'refreshEmails()',
                                    actionTitle: '从远程刷新'
                                }
                            );
                            updateEmailListHeader('聚合', 0);
                        }
                        return;
                    }

                    if (
                        isNormalMailboxListRequest()
                        && isNormalMailLocalRetentionEnabled()
                    ) {
                        const renderedLocal = await tryRenderLocalRetainedEmails(email, cacheKey);
                        if (!renderedLocal) {
                            currentEmails = [];
                            currentMethod = 'local';
                            hasMoreEmails = false;
                            currentSkip = 0;
                            setMailSyncStatus('本地暂无缓存，可点击刷新从远程获取');
                            container.innerHTML = renderEmptyStateMarkup(
                                '📭',
                                '本地暂无邮件缓存，请点击右上角刷新从远程获取',
                                {
                                    onAction: 'refreshEmails()',
                                    actionTitle: '从远程刷新'
                                }
                            );
                            updateEmailListHeader('Local', 0);
                        }
                        return;
                    }
                }

                await fetchRemoteEmails(email, cacheKey, {
                    aggregated,
                    skip: 0,
                    top: 20,
                    method: aggregated ? 'aggregated' : undefined,
                    context
                });
            } catch (error) {
                if (!isCurrentMailboxContext(context)) {
                    return;
                }
                const browserError = buildBrowserMailFetchError(error);
                const errorMessage = getFetchErrorMessage(error);
                setMailSyncStatus('');
                showEmailFetchErrorModal({ browser: browserError });
                container.innerHTML = renderEmptyStateMarkup('⚠️', errorMessage, {
                    onAction: 'refreshEmails()',
                    actionTitle: '刷新邮件列表'
                });
            } finally {
                if (isCurrentMailboxContext(context)) {
                    setEmailListLoadingState(false);
                }
            }
        }

        // 渲染邮件列表
        // Selected email IDs
        let selectedEmailIds = new Set();
        let pendingReadEmailIds = new Set();
        let isBatchSelectMode = false;
        let highlightedNewEmailKeys = new Set();
        const requestedBodyRetentionKeys = new Set();
        const BODY_RETENTION_REQUEST_LIMIT = 5;

        function hideNewMailNotice() {
            const notice = document.getElementById('newMailNotice');
            if (!notice) {
                return;
            }

            notice.hidden = true;
            notice.innerHTML = '';
            notice.dataset.syncKey = '';
            notice.removeAttribute('role');
            notice.removeAttribute('tabindex');
            notice.onclick = null;
            notice.onkeydown = null;
        }

        function getNewMessageIdKeys(newMessageIds, fallbackFolder = currentFolder) {
            return new Set(
                (newMessageIds || [])
                    .map(item => getEmailMessageStableKey(item, fallbackFolder))
                    .filter(Boolean)
            );
        }

        function collectNewlySyncedEmailRows(data, mergeResult, fallbackFolder = currentFolder) {
            const newMessageKeys = getNewMessageIdKeys(data.new_message_ids, fallbackFolder);
            const candidateRows = Array.isArray(data.emails) ? data.emails : [];
            const rows = candidateRows.filter(emailItem => {
                const key = getEmailMessageStableKey(emailItem, fallbackFolder);
                return key && newMessageKeys.has(key);
            });

            if (rows.length > 0) {
                return rows;
            }
            return Number(data.new_count || 0) > 0 ? mergeResult.newEmails : [];
        }

        function showNewMailNotice(newCount, syncKey = getPendingNewMailSyncKey()) {
            const notice = document.getElementById('newMailNotice');
            if (!notice || newCount <= 0) {
                hideNewMailNotice();
                return;
            }

            const acceptPendingSync = () => applyPendingNewMailSync(syncKey);
            notice.hidden = false;
            notice.setAttribute('role', 'button');
            notice.setAttribute('tabindex', '0');
            notice.dataset.syncKey = syncKey;
            notice.onclick = acceptPendingSync;
            notice.onkeydown = event => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    acceptPendingSync();
                }
            };
            notice.replaceChildren();
            const message = document.createElement('span');
            message.textContent = `有 ${Number(newCount)} 封新邮件已同步`;
            const hint = document.createElement('span');
            hint.className = 'new-mail-notice__hint';
            hint.textContent = '点击显示';
            notice.append(message, hint);
        }


        function markNewlySyncedEmailRows(rows, fallbackFolder = currentFolder) {
            highlightedNewEmailKeys = new Set();
            rows.forEach(emailItem => {
                const key = getEmailMessageStableKey(emailItem, fallbackFolder);
                if (key) {
                    highlightedNewEmailKeys.add(key);
                }
            });
        }

        function scheduleNewEmailHighlightClear() {
            window.setTimeout(() => {
                highlightedNewEmailKeys = new Set();
                document.querySelectorAll('.email-item.newly-synced').forEach(item => {
                    item.classList.remove('newly-synced');
                });
            }, NEW_EMAIL_HIGHLIGHT_CLEAR_DELAY_MS);
        }

        function announceNewlySyncedEmailRows(data, rows, fallbackFolder = currentFolder, syncKey = getPendingNewMailSyncKey()) {
            const reportedCount = Number(data.new_count || 0);
            const visibleCount = reportedCount > 0 ? reportedCount : rows.length;
            if (visibleCount <= 0) {
                hideNewMailNotice();
                return;
            }

            showNewMailNotice(visibleCount, syncKey);
        }

        function buildBodyRetentionItems(rows, fallbackFolder = currentFolder) {
            const method = getRemoteMailboxMethodFallback();
            return (rows || [])
                .map(emailItem => ({
                    id: String(emailItem?.id || '').trim(),
                    folder: String(emailItem?.folder || fallbackFolder || 'inbox'),
                    id_mode: String(emailItem?.id_mode || '').trim(),
                    method
                }))
                .filter(item => item.id);
        }

        function getUnrequestedBodyRetentionItems(rows, fallbackFolder = currentFolder) {
            const items = buildBodyRetentionItems(rows, fallbackFolder);
            const unrequestedItems = items.filter(item => {
                const key = getEmailMessageStableKey(item, fallbackFolder);
                return key && !requestedBodyRetentionKeys.has(key);
            });
            return unrequestedItems.slice(0, BODY_RETENTION_REQUEST_LIMIT);
        }

        function requestBodyRetentionForNewRows(rows, fallbackFolder = currentFolder) {
            const items = getUnrequestedBodyRetentionItems(rows, fallbackFolder);
            if (!items.length || !currentAccount || isTempEmailGroup || !isNormalMailLocalRetentionEnabled()) {
                return;
            }

            const requestedKeys = items
                .map(item => getEmailMessageStableKey(item, fallbackFolder))
                .filter(Boolean);
            requestedKeys.forEach(key => requestedBodyRetentionKeys.add(key));

            fetch('/api/emails/retain-bodies', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    email: currentAccount,
                    folder: fallbackFolder,
                    method: getRemoteMailboxMethodFallback(),
                    items
                })
            }).then(response => {
                if (!response.ok) {
                    throw new Error(`Retained body request failed with status ${response.status}`);
                }
                return response.json().catch(() => ({ success: true }));
            }).then(data => {
                if (data && data.success === false) {
                    throw new Error(data.error || 'Retained body request failed');
                }
            }).catch(error => {
                requestedKeys.forEach(key => requestedBodyRetentionKeys.delete(key));
                console.warn('Retained mail body background fetch failed:', error);
            });
        }

        function buildEmailDetailRequestUrl(messageId, folder, selectedEmail = {}) {
            const accountEmail = getEmailAccountAddress(selectedEmail)
                || (!isAggregatedInboxMode() ? currentAccount : '');
            const query = new URLSearchParams({
                method: getCurrentEmailRemoteActionMethod(selectedEmail),
                folder
            });
            if (isNormalMailboxListRequest() && isNormalMailLocalRetentionEnabled()) {
                query.set('prefer_local', '1');
            }
            appendEmailIdModeParam(query, selectedEmail);
            return `/api/email/${encodeURIComponent(accountEmail)}/${encodeURIComponent(messageId)}?${query.toString()}`;
        }

        function getRecipientDisplayLabel(emailItem) {
            if (isTempEmailGroup && currentMethod !== 'cloudflare-admin') {
                return '';
            }

            const accountAddress = getEmailAccountAddress(emailItem);
            const normalizedCurrentAccount = String(
                isAggregatedInboxMode() ? accountAddress : (currentAccount || '')
            ).trim().toLowerCase();
            const toValue = String(emailItem?.to || '').trim();
            if (!normalizedCurrentAccount || !toValue) {
                return '';
            }

            const recipientCandidates = toValue.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
            const recipients = recipientCandidates.length > 0
                ? recipientCandidates.map(recipient => recipient.trim().toLowerCase())
                : [toValue.toLowerCase()];

            if (recipients.includes(normalizedCurrentAccount)) {
                return '';
            }

            return `to: ${toValue}`;
        }

        function getEmailSourceLabel(emailItem) {
            if (currentMethod === 'cloudflare-admin') {
                return 'Cloudflare';
            }
            if (isTempEmailGroup || currentFolder !== 'all' || !emailItem?.folder) {
                return '';
            }
            return getFolderDisplayName(emailItem?.folder);
        }

        function formatAttachmentSize(size) {
            const numericSize = Number(size) || 0;
            if (numericSize < 1024) {
                return `${numericSize} B`;
            }
            if (numericSize < 1024 * 1024) {
                return `${(numericSize / 1024).toFixed(1).replace(/\.0$/, '')} KB`;
            }
            return `${(numericSize / (1024 * 1024)).toFixed(1).replace(/\.0$/, '')} MB`;
        }

        function appendEmailIdModeParam(query, email) {
            const idMode = String(email?.id_mode || email?.idMode || '').trim().toLowerCase();
            if (idMode) {
                query.set('id_mode', idMode);
            }
        }

        function buildAttachmentDownloadUrl(email, attachment) {
            const query = new URLSearchParams();
            query.set('method', getCurrentEmailRemoteActionMethod(email));
            query.set('folder', email?.folder || currentFolder || 'inbox');
            appendEmailIdModeParam(query, email);
            return `/api/email/${encodeURIComponent(currentAccount)}/${encodeURIComponent(email.id)}/attachments/${encodeURIComponent(attachment.id)}?${query.toString()}`;
        }

        function buildAllAttachmentsDownloadUrl(email) {
            const query = new URLSearchParams();
            query.set('method', getCurrentEmailRemoteActionMethod(email));
            query.set('folder', email?.folder || currentFolder || 'inbox');
            appendEmailIdModeParam(query, email);
            return `/api/email/${encodeURIComponent(currentAccount)}/${encodeURIComponent(email.id)}/attachments/download-all?${query.toString()}`;
        }

        function parseDownloadFilename(response, fallbackFilename) {
            const disposition = response.headers.get('content-disposition') || '';
            const encodedMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
            if (encodedMatch) {
                try {
                    return decodeURIComponent(encodedMatch[1].trim().replace(/^"|"$/g, '')) || fallbackFilename;
                } catch (error) {
                    return fallbackFilename;
                }
            }

            const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
            return filenameMatch ? filenameMatch[1] : fallbackFilename;
        }

        function triggerAttachmentDownload(blob, filename) {
            const url = window.URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = filename || 'attachment';
            link.style.display = 'none';
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.setTimeout(() => window.URL.revokeObjectURL(url), 1000);
        }

        function setAttachmentDownloadState(link, isDownloading) {
            link.dataset.downloading = isDownloading ? 'true' : 'false';
            link.classList.toggle('is-downloading', isDownloading);
            link.setAttribute('aria-busy', isDownloading ? 'true' : 'false');

            if (link.classList.contains('email-attachments__download-all')) {
                if (!link.dataset.defaultLabel) {
                    link.dataset.defaultLabel = link.textContent.trim() || '全部下载';
                }
                link.textContent = isDownloading ? '打包中...' : link.dataset.defaultLabel;
                return;
            }

            const action = link.querySelector('.email-attachment-item__action');
            if (action) {
                action.textContent = isDownloading ? '下载中...' : '下载';
            }
        }

        async function downloadEmailAttachmentFile(event, link) {
            event.preventDefault();
            if (!link || link.dataset.downloading === 'true') {
                return;
            }

            const isDownloadAll = link.classList.contains('email-attachments__download-all');
            const fallbackFilename = link.getAttribute('download') || (isDownloadAll ? 'attachments.zip' : 'attachment');
            const pendingMessage = isDownloadAll ? '正在打包附件...' : '正在下载附件...';
            const failureMessage = isDownloadAll ? '全部附件下载失败' : '附件下载失败';
            const successMessage = isDownloadAll ? '附件已打包，下载已开始' : '附件下载已开始';

            setAttachmentDownloadState(link, true);
            showToast(pendingMessage, 'info');

            try {
                const response = await fetch(link.href, {
                    method: 'GET',
                    cache: 'no-store',
                    credentials: 'same-origin'
                });
                const contentType = response.headers.get('content-type') || '';
                const disposition = response.headers.get('content-disposition') || '';
                const isFileResponse = /attachment/i.test(disposition);

                if (!response.ok || (!isFileResponse && contentType.includes('application/json'))) {
                    const data = contentType.includes('application/json')
                        ? await response.json().catch(() => null)
                        : null;
                    if (data) {
                        handleApiError(data, failureMessage);
                    } else {
                        showToast(`${failureMessage}（HTTP ${response.status}）`, 'error');
                    }
                    return;
                }

                const blob = await response.blob();
                triggerAttachmentDownload(blob, parseDownloadFilename(response, fallbackFilename));
                showToast(successMessage, 'success');
            } catch (error) {
                showToast(`${failureMessage}，请检查网络后重试`, 'error');
            } finally {
                setAttachmentDownloadState(link, false);
            }
        }

        function renderAttachmentSection(email) {
            const attachments = Array.isArray(email?.attachments) ? email.attachments : [];
            if (attachments.length === 0) {
                return '';
            }

            return `
                <section class="email-attachments" aria-label="邮件附件">
                    <div class="email-attachments__header">
                        <div class="email-attachments__summary">
                            <div class="email-attachments__title">附件</div>
                            <div class="email-attachments__count">${attachments.length} 个</div>
                        </div>
                        ${attachments.length > 1 ? `
                            <a class="email-attachments__download-all"
                               href="${buildAllAttachmentsDownloadUrl(email)}"
                               download="attachments.zip"
                               onclick="downloadEmailAttachmentFile(event, this)">全部下载</a>
                        ` : ''}
                    </div>
                    <div class="email-attachments__list">
                        ${attachments.map(attachment => `
                            <a class="email-attachment-item"
                               href="${buildAttachmentDownloadUrl(email, attachment)}"
                               download="${escapeHtml(attachment.name || 'attachment')}"
                               onclick="downloadEmailAttachmentFile(event, this)">
                                <span class="email-attachment-item__icon" aria-hidden="true">📎</span>
                                <span class="email-attachment-item__content">
                                    <span class="email-attachment-item__name">${escapeHtml(attachment.name || 'attachment')}</span>
                                    <span class="email-attachment-item__meta">
                                        ${attachment.is_inline ? '<span class="email-attachment-item__badge">内联</span>' : ''}
                                        <span>${formatAttachmentSize(attachment.size)}</span>
                                        <span>${escapeHtml(attachment.content_type || 'application/octet-stream')}</span>
                                    </span>
                                </span>
                                <span class="email-attachment-item__action">下载</span>
                            </a>
                        `).join('')}
                    </div>
                </section>
            `;
        }

        function renderEmailList(emails) {
            const container = document.getElementById('emailList');
            const sourceEmails = Array.isArray(statusFilterOverrideEmails)
                ? normalizeEmailListItems(statusFilterOverrideEmails)
                : normalizeEmailListItems(emails);
            const visibleEmails = isTempEmailGroup || currentMethod === 'cloudflare-admin'
                ? sourceEmails
                : getVisibleEmailsForCurrentFilter(sourceEmails);

            if (visibleEmails.length === 0) {
                const emptyStateText = isTempEmailGroup
                    ? '暂无邮件'
                    : (currentEmailStatusFilter !== 'all' && sourceEmails.length > 0
                        ? '当前筛选下没有邮件'
                        : `${getFolderDisplayName(currentFolder)}为空`);
                const emptyPrefix = currentMethod === 'cloudflare-admin' && typeof renderCloudflareGlobalFilterBar === 'function'
                    ? renderCloudflareGlobalFilterBar()
                    : '';
                container.innerHTML = emptyPrefix + renderEmptyStateMarkup('📭', emptyStateText, {
                    onAction: 'refreshEmails()',
                    actionTitle: '刷新邮件列表'
                });
                selectedEmailIds.clear();
                currentEmailId = null;
                updateEmailBatchActionBar();
                return;
            }

            bindEmailListDelegatedEvents();
            const listPrefix = currentMethod === 'cloudflare-admin' && typeof renderCloudflareGlobalFilterBar === 'function'
                ? renderCloudflareGlobalFilterBar()
                : '';

            container.innerHTML = listPrefix + visibleEmails.map((email) => {
                const sourceIndex = sourceEmails.indexOf(email);
                const selectionKey = getEmailSelectionKey(email);
                const isChecked = selectedEmailIds.has(selectionKey);
                const isActive = currentEmailId === selectionKey || currentEmailId === email.id;
                const recipientDisplayLabel = getRecipientDisplayLabel(email);
                const sourceLabel = getEmailSourceLabel(email);
                const accountLabel = isAggregatedInboxMode() ? getEmailAccountAddress(email) : '';
                const accountRemark = isAggregatedInboxMode()
                    ? String(email.account_remark || email.accountRemark || '').trim()
                    : '';
                const hasAttachments = Boolean(email.has_attachments);
                const isFlagged = isEmailFlagged(email);
                const isNewlySynced = highlightedNewEmailKeys.has(getEmailMessageStableKey(email));
                return `
                <div class="email-item ${isEmailUnread(email) ? 'unread' : ''} ${isActive ? 'active' : ''} ${isNewlySynced ? 'newly-synced' : ''}"
                     data-email-id="${escapeHtml(String(email.id || ''))}"
                     data-email-selection-key="${escapeHtml(selectionKey)}"
                     data-email-index="${sourceIndex}">
                    <div class="email-checkbox-wrapper" data-email-selection-key="${escapeHtml(selectionKey)}">
                        <input type="checkbox" class="email-checkbox" ${isChecked ? 'checked' : ''} style="pointer-events: none;">
                    </div>
                    <div class="email-body">
                        <div class="email-top-row">
                            <div class="email-top-main">
                                ${isEmailUnread(email) ? '<span class="email-unread-dot" title="未读" aria-label="未读"></span>' : ''}
                                <div class="email-sender-block">
                                    <div class="email-from" title="${escapeHtml(email.from || '未知发件人')}">${escapeHtml(email.from || '未知发件人')}</div>
                                    ${recipientDisplayLabel ? `<div class="email-recipient" title="${escapeHtml(recipientDisplayLabel)}">${escapeHtml(recipientDisplayLabel)}</div>` : ''}
                                </div>
                                ${hasAttachments ? '<span class="email-attachment-indicator" title="含附件" aria-label="含附件">📎</span>' : ''}
                                ${sourceLabel ? `<span class="email-folder-badge email-folder-badge--${escapeHtml(String(email.folder || '').toLowerCase())}">${escapeHtml(sourceLabel)}</span>` : ''}
                            </div>
                            <div style="display:flex;align-items:center;gap:8px;">
                                ${!isTempEmailGroup ? `<button type="button" class="email-flag-btn ${isFlagged ? 'is-flagged' : ''}" data-email-flag-toggle="true" data-email-index="${sourceIndex}" title="${isFlagged ? '取消 Flag' : '标记 Flag'}" aria-label="${isFlagged ? '取消 Flag' : '标记 Flag'}">${isFlagged ? '★' : '☆'}</button>` : ''}
                                <div class="email-date">${formatDate(email.date)}</div>
                            </div>
                        </div>
                        ${accountLabel ? `<div class="email-account-meta">
                            <div class="email-account-label" title="${escapeHtml(accountLabel)}">${escapeHtml(accountLabel)}</div>
                            ${accountRemark ? renderColoredRemarkMarkup(accountRemark, 'email-account-remark') : ''}
                        </div>` : ''}
                        <div class="email-subject">${escapeHtml(email.subject || '无主题')}</div>
                        <div class="email-preview">${escapeHtml((email.body_preview || '').trim() || '暂无预览内容')}</div>
                    </div>
                </div>
            `}).join('');

            updateEmailBatchActionBar();
        }

        function findEmailItemBySelectionKey(selectionKey) {
            const key = String(selectionKey || '');
            if (!key) {
                return null;
            }
            return Array.from(document.querySelectorAll('.email-item[data-email-selection-key]'))
                .find(item => String(item.dataset.emailSelectionKey || '') === key)
                || null;
        }

        function setEmailSelectionState(selectionKey, selected, emailItem = null) {
            const key = String(selectionKey || '');
            if (!key) {
                return;
            }

            if (selected) {
                selectedEmailIds.add(key);
            } else {
                selectedEmailIds.delete(key);
            }

            const targetItem = emailItem || findEmailItemBySelectionKey(key);
            const checkbox = targetItem?.querySelector?.('.email-checkbox');
            if (checkbox) {
                checkbox.checked = selected;
            }
            updateEmailBatchActionBar();
        }

        function handleEmailListClick(event) {
            const flagBtn = event.target.closest('[data-email-flag-toggle="true"]');
            if (flagBtn) {
                event.stopPropagation();
                const index = Number(flagBtn.dataset.emailIndex);
                if (Number.isInteger(index) && currentEmails[index]) {
                    void toggleEmailFlag(currentEmails[index]);
                }
                return;
            }

            const checkboxWrapper = event.target.closest('.email-checkbox-wrapper[data-email-selection-key], .email-checkbox-wrapper[data-email-id]');
            if (checkboxWrapper) {
                event.stopPropagation();
                const selectionKey = checkboxWrapper.dataset.emailSelectionKey || checkboxWrapper.dataset.emailId;
                toggleEmailSelection(selectionKey, checkboxWrapper.closest('.email-item'));
                return;
            }

            const emailItem = event.target.closest('.email-item[data-email-id]');
            if (!emailItem || !emailItem.parentElement?.contains(event.target)) {
                return;
            }

            const emailId = emailItem.dataset.emailId || '';
            const emailIndex = Number(emailItem.dataset.emailIndex || 0);
            // 行点击只打开详情；勾选仅由复选框区域负责
            if (currentMethod === 'cloudflare-admin') {
                getCloudflareGlobalMessageDetail(emailId, emailIndex);
            } else if (isTempEmailGroup) {
                getTempEmailDetail(emailId, emailIndex);
            } else {
                selectEmail(emailId, emailIndex);
            }
        }

        function bindEmailListDelegatedEvents() {
            const container = document.getElementById('emailList');
            if (!container || container.dataset.emailListClickBound === 'true') {
                return;
            }
            container.dataset.emailListClickBound = 'true';
            container.addEventListener('click', handleEmailListClick);
        }

        function getSelectedEmailItems() {
            const selectedKeys = new Set(Array.from(selectedEmailIds).map(id => String(id)));
            if (!selectedKeys.size) {
                return [];
            }

            return currentEmails.filter(email => {
                const selectionKey = getEmailSelectionKey(email);
                return selectedKeys.has(selectionKey) || selectedKeys.has(String(email.id));
            });
        }

        function applyEmailReadState(updatedItems, isRead = true) {
            const updatedKeys = new Set();
            const updatedIds = new Set();
            (updatedItems || []).forEach(item => {
                if (item && typeof item === 'object') {
                    const key = getEmailSelectionKey(item);
                    if (key) {
                        updatedKeys.add(key);
                    }
                    if (item.id != null) {
                        updatedIds.add(String(item.id));
                    }
                    return;
                }
                if (item != null && item !== '') {
                    updatedIds.add(String(item));
                }
            });
            if (!updatedKeys.size && !updatedIds.size) {
                return;
            }

            const matchesUpdated = (email) => {
                const key = getEmailSelectionKey(email);
                return (key && updatedKeys.has(key)) || updatedIds.has(String(email.id));
            };

            const unreadDeltasByAccountId = new Map();
            const resolveUnreadAccountId = (email) => {
                const directId = Number(email?.account_id);
                if (Number.isFinite(directId) && directId > 0) {
                    return directId;
                }
                const accountEmail = (
                    getEmailAccountAddress(email)
                    || (!isAggregatedInboxMode() ? currentAccount : '')
                ).toLowerCase();
                if (!accountEmail || !accountsCache || typeof accountsCache !== 'object') {
                    return 0;
                }
                for (const list of Object.values(accountsCache)) {
                    if (!Array.isArray(list)) continue;
                    const matched = list.find(item => String(item?.email || '').toLowerCase() === accountEmail);
                    if (matched?.id != null) {
                        return Number(matched.id) || 0;
                    }
                }
                return 0;
            };
            const trackUnreadDelta = (email) => {
                const folder = String(email?.folder || currentFolder || 'inbox').trim().toLowerCase();
                if (!['inbox', 'junkemail', 'all'].includes(folder)) {
                    return;
                }
                const accountId = resolveUnreadAccountId(email);
                if (!Number.isFinite(accountId) || accountId <= 0) {
                    return;
                }
                const wasUnread = isEmailUnread(email);
                const willBeUnread = isRead === false;
                if (wasUnread === willBeUnread) {
                    return;
                }
                const delta = willBeUnread ? 1 : -1;
                unreadDeltasByAccountId.set(
                    accountId,
                    (unreadDeltasByAccountId.get(accountId) || 0) + delta
                );
            };

            const applyToEmailList = (emails, trackDelta = false) => {
                if (!Array.isArray(emails)) {
                    return;
                }

                emails.forEach(email => {
                    if (matchesUpdated(email)) {
                        if (trackDelta) {
                            trackUnreadDelta(email);
                        }
                        email.is_read = isRead;
                    }
                });
            };

            applyToEmailList(currentEmails, true);
            if (typeof adjustAccountUnreadCount === 'function') {
                unreadDeltasByAccountId.forEach((delta, accountId) => {
                    adjustAccountUnreadCount(accountId, delta);
                });
            }

            const cachePrefixes = new Set([`${currentAccount || ''}_`]);
            if (isAggregatedInboxMode()) {
                cachePrefixes.add(`${getAggregatedInboxCacheAccountKey()}_`);
            }
            Object.entries(emailListCache).forEach(([cacheKey, cacheValue]) => {
                if (![...cachePrefixes].some(prefix => cacheKey.startsWith(prefix))) {
                    return;
                }
                applyToEmailList(cacheValue?.emails);
            });

            if (currentEmailDetail && (
                updatedKeys.has(getEmailSelectionKey(currentEmailDetail))
                || updatedIds.has(String(currentEmailDetail.id))
            )) {
                currentEmailDetail.is_read = isRead;
            }
        }

        function groupEmailActionItemsByAccount(items) {
            const groups = new Map();
            (items || []).forEach(item => {
                if (!item?.id) {
                    return;
                }
                const accountEmail = getEmailAccountAddress(item) || (!isAggregatedInboxMode() ? currentAccount : '');
                if (!accountEmail) {
                    return;
                }
                if (!groups.has(accountEmail)) {
                    groups.set(accountEmail, []);
                }
                groups.get(accountEmail).push({
                    id: String(item.id),
                    folder: String(item.folder || currentFolder || 'inbox'),
                    id_mode: String(item.id_mode || ''),
                    account_id: item.account_id,
                    account_email: accountEmail
                });
            });
            return groups;
        }

        async function requestMarkEmailsAsRead(items, { silent = false, isRead = true } = {}) {
            const targetIsRead = isRead !== false;
            const actionLabel = targetIsRead ? '已读' : '未读';
            const groupedItems = groupEmailActionItemsByAccount(items);
            const normalizedItems = [];
            groupedItems.forEach(accountItems => {
                accountItems.forEach(item => {
                    const pendingKey = getEmailSelectionKey(item);
                    if (pendingKey && pendingReadEmailIds.has(pendingKey)) {
                        return;
                    }
                    if (!pendingKey && pendingReadEmailIds.has(item.id)) {
                        return;
                    }
                    normalizedItems.push(item);
                });
            });

            if (!normalizedItems.length) {
                return {
                    success: true,
                    success_count: 0,
                    failed_count: 0,
                    updated_ids: [],
                    errors: []
                };
            }

            normalizedItems.forEach(item => {
                const pendingKey = getEmailSelectionKey(item) || String(item.id);
                pendingReadEmailIds.add(pendingKey);
            });

            try {
                const requestGroups = groupEmailActionItemsByAccount(normalizedItems);
                const results = await Promise.all(
                    Array.from(requestGroups.entries()).map(async ([accountEmail, accountItems]) => {
                        const response = await fetch('/api/emails/mark-read', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                email: accountEmail,
                                method: getRemoteMailboxMethodFallback(),
                                folder: currentFolder,
                                is_read: targetIsRead,
                                items: accountItems.map(item => ({
                                    id: item.id,
                                    folder: item.folder,
                                    id_mode: item.id_mode
                                }))
                            })
                        });
                        const result = await response.json();
                        const updatedIds = Array.isArray(result.updated_ids) ? result.updated_ids : [];
                        const updatedItems = accountItems
                            .filter(item => updatedIds.includes(item.id) || updatedIds.includes(String(item.id)))
                            .map(item => ({
                                ...item,
                                account_email: accountEmail
                            }));
                        if (!updatedItems.length && updatedIds.length) {
                            updatedIds.forEach(id => {
                                updatedItems.push({
                                    id: String(id),
                                    account_email: accountEmail,
                                    folder: currentFolder || 'inbox',
                                    id_mode: ''
                                });
                            });
                        }
                        return {
                            ...result,
                            updated_items: updatedItems
                        };
                    })
                );

                const combined = {
                    success: results.every(result => result.success !== false || (result.success_count || 0) > 0),
                    success_count: results.reduce((sum, result) => sum + (Number(result.success_count) || 0), 0),
                    failed_count: results.reduce((sum, result) => sum + (Number(result.failed_count) || 0), 0),
                    updated_ids: results.flatMap(result => result.updated_ids || []),
                    updated_items: results.flatMap(result => result.updated_items || []),
                    errors: results.flatMap(result => result.errors || [])
                };

                if (combined.updated_items.length > 0) {
                    applyEmailReadState(combined.updated_items, targetIsRead);
                    renderEmailList(currentEmails);
                }

                if (!silent) {
                    if (combined.success_count > 0 && combined.failed_count === 0) {
                        showToast(`已将 ${combined.success_count} 封邮件设为${actionLabel}`);
                    } else if (combined.success_count > 0) {
                        showToast(`已设为${actionLabel} ${combined.success_count} 封，失败 ${combined.failed_count} 封`, 'warning');
                    } else {
                        handleApiError(results[0] || combined, `设为${actionLabel}失败`);
                    }
                }

                if (combined.failed_count > 0 && combined.errors.length > 0) {
                    console.warn('Mark read errors:', combined.errors);
                }

                return combined;
            } catch (error) {
                if (!silent) {
                    showToast(`设为${actionLabel}失败，请检查网络后重试`, 'error');
                }
                return {
                    success: false,
                    success_count: 0,
                    failed_count: normalizedItems.length,
                    updated_ids: [],
                    errors: [error]
                };
            } finally {
                normalizedItems.forEach(item => {
                    const pendingKey = getEmailSelectionKey(item) || String(item.id);
                    pendingReadEmailIds.delete(pendingKey);
                });
            }
        }

        async function requestMarkEmailsFlag(items, flagged = true, { silent = false } = {}) {
            const targetFlagged = flagged !== false;
            const actionLabel = targetFlagged ? 'Flag' : '取消 Flag';
            const normalizedItems = [];
            groupEmailActionItemsByAccount(items).forEach(accountItems => {
                accountItems.forEach(item => {
                    if (item?.id) {
                        normalizedItems.push(item);
                    }
                });
            });

            if (!normalizedItems.length) {
                return {
                    success: true,
                    success_count: 0,
                    failed_count: 0,
                    updated_ids: [],
                    errors: []
                };
            }

            try {
                const requestGroups = groupEmailActionItemsByAccount(normalizedItems);
                const results = await Promise.all(
                    Array.from(requestGroups.entries()).map(async ([accountEmail, accountItems]) => {
                        const response = await fetch('/api/emails/mark-flag', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                email: accountEmail,
                                method: getRemoteMailboxMethodFallback(),
                                folder: currentFolder,
                                flagged: targetFlagged,
                                items: accountItems.map(item => ({
                                    id: item.id,
                                    folder: item.folder || currentFolder || 'inbox',
                                    id_mode: item.id_mode || ''
                                }))
                            })
                        });
                        const result = await response.json();
                        const updatedIds = Array.isArray(result.updated_ids) ? result.updated_ids.map(String) : [];
                        const updatedItems = accountItems
                            .filter(item => updatedIds.includes(String(item.id)))
                            .map(item => ({
                                ...item,
                                account_email: accountEmail
                            }));
                        if (!updatedItems.length && updatedIds.length) {
                            updatedIds.forEach(id => {
                                updatedItems.push({
                                    id: String(id),
                                    account_email: accountEmail,
                                    folder: currentFolder || 'inbox',
                                    id_mode: ''
                                });
                            });
                        }
                        return {
                            ...result,
                            updated_items: updatedItems
                        };
                    })
                );

                const combined = {
                    success: results.every(result => result.success !== false || (result.success_count || 0) > 0),
                    success_count: results.reduce((sum, result) => sum + (Number(result.success_count) || 0), 0),
                    failed_count: results.reduce((sum, result) => sum + (Number(result.failed_count) || 0), 0),
                    updated_ids: results.flatMap(result => result.updated_ids || []),
                    updated_items: results.flatMap(result => result.updated_items || []),
                    errors: results.flatMap(result => result.errors || [])
                };

                if (combined.updated_items.length > 0) {
                    applyEmailFlagState(combined.updated_items, targetFlagged);
                    renderEmailList(currentEmails);
                }

                if (!silent) {
                    if (combined.success_count > 0 && combined.failed_count === 0) {
                        showToast(targetFlagged
                            ? `已标记 ${combined.success_count} 封 Flag`
                            : `已取消 ${combined.success_count} 封 Flag`);
                    } else if (combined.success_count > 0) {
                        showToast(`${actionLabel}成功 ${combined.success_count} 封，失败 ${combined.failed_count} 封`, 'warning');
                    } else {
                        handleApiError(results[0] || combined, `${actionLabel}失败`);
                    }
                }

                return combined;
            } catch (error) {
                if (!silent) {
                    showToast(`${actionLabel}失败，请检查网络后重试`, 'error');
                }
                return {
                    success: false,
                    success_count: 0,
                    failed_count: normalizedItems.length,
                    updated_ids: [],
                    errors: [error]
                };
            }
        }

        function applyEmailFlagState(updatedItems, isFlagged = true) {
            const updatedKeys = new Set();
            const updatedIds = new Set();
            (updatedItems || []).forEach(item => {
                if (item && typeof item === 'object') {
                    const key = getEmailSelectionKey(item);
                    if (key) {
                        updatedKeys.add(key);
                    }
                    if (item.id != null) {
                        updatedIds.add(String(item.id));
                    }
                    return;
                }
                if (item != null && item !== '') {
                    updatedIds.add(String(item));
                }
            });
            if (!updatedKeys.size && !updatedIds.size) {
                return;
            }

            const matchesUpdated = (email) => {
                const key = getEmailSelectionKey(email);
                return (key && updatedKeys.has(key)) || updatedIds.has(String(email.id));
            };

            const applyToEmailList = (emails) => {
                if (!Array.isArray(emails)) {
                    return;
                }
                emails.forEach(email => {
                    if (matchesUpdated(email)) {
                        email.is_flagged = isFlagged;
                    }
                });
            };

            applyToEmailList(currentEmails);

            const cachePrefixes = new Set([`${currentAccount || ''}_`]);
            if (isAggregatedInboxMode()) {
                cachePrefixes.add(`${getAggregatedInboxCacheAccountKey()}_`);
            }
            Object.entries(emailListCache).forEach(([cacheKey, cacheValue]) => {
                if (![...cachePrefixes].some(prefix => cacheKey.startsWith(prefix))) {
                    return;
                }
                applyToEmailList(cacheValue?.emails);
            });

            if (currentEmailDetail && (
                updatedKeys.has(getEmailSelectionKey(currentEmailDetail))
                || updatedIds.has(String(currentEmailDetail.id))
            )) {
                currentEmailDetail.is_flagged = isFlagged;
            }
        }

        async function toggleEmailFlag(emailItem) {
            if (!emailItem?.id || isTempEmailGroup) {
                return;
            }
            const accountEmail = getEmailAccountAddress(emailItem) || (!isAggregatedInboxMode() ? currentAccount : '');
            if (!accountEmail) {
                showToast('无法确定邮件所属账号', 'error');
                return;
            }

            const nextFlagged = !isEmailFlagged(emailItem);
            await requestMarkEmailsFlag([{
                id: String(emailItem.id),
                folder: emailItem.folder || currentFolder || 'inbox',
                id_mode: emailItem.id_mode || '',
                account_email: accountEmail
            }], nextFlagged);
        }

        function toggleEmailSelection(emailId, emailItem = null) {
            const selectionKey = String(emailId || '');
            if (!selectionKey) {
                return;
            }
            setEmailSelectionState(selectionKey, !selectedEmailIds.has(selectionKey), emailItem);
        }

        function resetEmailBatchActionButtons() {
            const configs = [
                { id: 'batchMarkReadBtn', label: '已读', title: '设为已读' },
                { id: 'batchMarkUnreadBtn', label: '未读', title: '设为未读' },
                { id: 'batchMarkFlagBtn', label: 'Flag', title: '标记 Flag' },
                { id: 'batchUnmarkFlagBtn', label: '取消', title: '取消 Flag' },
            ];
            configs.forEach(({ id, label, title }) => {
                const btn = document.getElementById(id);
                if (!btn) {
                    return;
                }
                btn.disabled = false;
                btn.dataset.loading = 'false';
                btn.textContent = label;
                btn.title = title;
            });
        }

        function updateEmailBatchActionBar() {
            const bar = document.getElementById('emailBatchActionBar');
            const selectAllBtn = document.getElementById('emailSelectAllBtn');
            const markReadBtn = document.getElementById('batchMarkReadBtn');
            const markUnreadBtn = document.getElementById('batchMarkUnreadBtn');
            const markFlagBtn = document.getElementById('batchMarkFlagBtn');
            const unmarkFlagBtn = document.getElementById('batchUnmarkFlagBtn');
            const panel = document.getElementById('emailListPanel');
            const selectedEmails = getSelectedEmailItems();
            const unreadSelectedCount = selectedEmails.filter(email => isEmailUnread(email)).length;
            const readSelectedCount = selectedEmails.filter(email => !isEmailUnread(email)).length;
            const unflaggedSelectedCount = selectedEmails.filter(email => !isEmailFlagged(email)).length;
            const flaggedSelectedCount = selectedEmails.filter(email => isEmailFlagged(email)).length;
            if (isTempEmailGroup) {
                bar.style.display = 'none';
                panel?.classList.remove('batch-toolbar-active');
                resetEmailBatchActionButtons();
                return;
            }
            if (selectedEmailIds.size > 0) {
                bar.style.display = 'flex';
                panel?.classList.add('batch-toolbar-active');
                document.getElementById('emailSelectedCount').textContent = `已选 ${selectedEmailIds.size} 项`;
                if (selectAllBtn) {
                    const visibleEmails = getVisibleEmailsForCurrentFilter(currentEmails);
                    const visibleKeys = visibleEmails
                        .map(email => getEmailSelectionKey(email))
                        .filter(Boolean);
                    const allVisibleSelected = visibleKeys.length > 0
                        && visibleKeys.every(key => selectedEmailIds.has(key));
                    selectAllBtn.textContent = allVisibleSelected ? '取消全选' : '全选';
                }
                if (markReadBtn) {
                    const isMarking = markReadBtn.dataset.loading === 'true';
                    markReadBtn.disabled = unreadSelectedCount === 0 || isMarking;
                    markReadBtn.title = unreadSelectedCount === 0 ? '所选邮件已全部为已读' : '设为已读';
                    if (!isMarking) {
                        markReadBtn.textContent = unreadSelectedCount > 0
                            ? `已读${unreadSelectedCount !== selectedEmails.length ? ` (${unreadSelectedCount})` : ''}`
                            : '已读';
                    }
                }
                if (markUnreadBtn) {
                    const isMarking = markUnreadBtn.dataset.loading === 'true';
                    markUnreadBtn.disabled = readSelectedCount === 0 || isMarking;
                    markUnreadBtn.title = readSelectedCount === 0 ? '所选邮件已全部为未读' : '设为未读';
                    if (!isMarking) {
                        markUnreadBtn.textContent = readSelectedCount > 0
                            ? `未读${readSelectedCount !== selectedEmails.length ? ` (${readSelectedCount})` : ''}`
                            : '未读';
                    }
                }
                if (markFlagBtn) {
                    const isMarking = markFlagBtn.dataset.loading === 'true';
                    markFlagBtn.disabled = unflaggedSelectedCount === 0 || isMarking;
                    markFlagBtn.title = unflaggedSelectedCount === 0 ? '所选邮件已全部标记 Flag' : '标记 Flag';
                    if (!isMarking) {
                        markFlagBtn.textContent = unflaggedSelectedCount > 0
                            ? `Flag${unflaggedSelectedCount !== selectedEmails.length ? ` (${unflaggedSelectedCount})` : ''}`
                            : 'Flag';
                    }
                }
                if (unmarkFlagBtn) {
                    const isMarking = unmarkFlagBtn.dataset.loading === 'true';
                    unmarkFlagBtn.disabled = flaggedSelectedCount === 0 || isMarking;
                    unmarkFlagBtn.title = flaggedSelectedCount === 0 ? '所选邮件均未标记 Flag' : '取消 Flag';
                    if (!isMarking) {
                        unmarkFlagBtn.textContent = flaggedSelectedCount > 0
                            ? `取消${flaggedSelectedCount !== selectedEmails.length ? ` (${flaggedSelectedCount})` : ''}`
                            : '取消';
                    }
                }
            } else {
                bar.style.display = 'none';
                panel?.classList.remove('batch-toolbar-active');
                resetEmailBatchActionButtons();
            }
        }

        function toggleSelectAllEmails() {
            const visibleEmails = getVisibleEmailsForCurrentFilter(currentEmails);
            if (!visibleEmails.length) return;

            const visibleKeys = visibleEmails
                .map(email => getEmailSelectionKey(email))
                .filter(Boolean);
            if (!visibleKeys.length) return;

            const allVisibleSelected = visibleKeys.every(key => selectedEmailIds.has(key));
            if (allVisibleSelected) {
                visibleKeys.forEach(key => selectedEmailIds.delete(key));
            } else {
                visibleKeys.forEach(key => selectedEmailIds.add(key));
            }
            renderEmailList(currentEmails);
        }

        function clearEmailSelection() {
            if (selectedEmailIds.size === 0) return;
            selectedEmailIds.clear();
            renderEmailList(currentEmails);
        }

        function buildSelectedEmailActionItems(predicate = () => true) {
            return getSelectedEmailItems()
                .filter(predicate)
                .map(email => ({
                    id: email.id,
                    folder: email.folder || currentFolder || 'inbox',
                    id_mode: email.id_mode || '',
                    account_id: email.account_id,
                    account_email: getEmailAccountAddress(email)
                }));
        }

        async function markSelectedEmailsAsRead() {
            const btn = document.getElementById('batchMarkReadBtn');
            if (!btn || btn.disabled) return;

            const unreadItems = buildSelectedEmailActionItems(email => isEmailUnread(email));
            if (!unreadItems.length) {
                showToast('所选邮件已全部为已读');
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = '设置中...';

            try {
                await requestMarkEmailsAsRead(unreadItems, { isRead: true });
            } finally {
                btn.dataset.loading = 'false';
                updateEmailBatchActionBar();
            }
        }

        async function markSelectedEmailsAsUnread() {
            const btn = document.getElementById('batchMarkUnreadBtn');
            if (!btn || btn.disabled) return;

            const readItems = buildSelectedEmailActionItems(email => !isEmailUnread(email));
            if (!readItems.length) {
                showToast('所选邮件已全部为未读');
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = '设置中...';

            try {
                await requestMarkEmailsAsRead(readItems, { isRead: false });
            } finally {
                btn.dataset.loading = 'false';
                updateEmailBatchActionBar();
            }
        }

        async function markSelectedEmailsFlag(flagged = true) {
            const targetFlagged = flagged !== false;
            const btn = document.getElementById(targetFlagged ? 'batchMarkFlagBtn' : 'batchUnmarkFlagBtn');
            if (!btn || btn.disabled) return;

            const items = buildSelectedEmailActionItems(email => (
                targetFlagged ? !isEmailFlagged(email) : isEmailFlagged(email)
            ));
            if (!items.length) {
                showToast(targetFlagged ? '所选邮件已全部标记 Flag' : '所选邮件均未标记 Flag');
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = '设置中...';

            try {
                await requestMarkEmailsFlag(items, targetFlagged);
            } finally {
                btn.dataset.loading = 'false';
                updateEmailBatchActionBar();
            }
        }

        function buildEmailDeleteItems(sourceItems) {
            return (sourceItems || [])
                .map(item => {
                    if (!item) {
                        return null;
                    }
                    if (typeof item !== 'object') {
                        const messageId = String(item || '').trim();
                        if (!messageId) {
                            return null;
                        }
                        return {
                            id: messageId,
                            folder: currentFolder || 'inbox',
                            id_mode: '',
                            account_email: isAggregatedInboxMode() ? '' : currentAccount
                        };
                    }
                    const messageId = String(item.id || item.message_id || '').trim();
                    if (!messageId) {
                        return null;
                    }
                    return {
                        id: messageId,
                        folder: String(item.folder || currentFolder || 'inbox'),
                        id_mode: String(item.id_mode || item.idMode || '').trim(),
                        account_id: item.account_id,
                        account_email: getEmailAccountAddress(item) || (!isAggregatedInboxMode() ? currentAccount : '')
                    };
                })
                .filter(Boolean);
        }

        async function confirmBatchDeleteEmails() {
            if (selectedEmailIds.size === 0) return;

            if (!(await showConfirmModal(`确定要永久删除选中的 ${selectedEmailIds.size} 封邮件吗？此操作不可恢复！`, { title: '批量删除邮件', confirmText: '确认删除' }))) {
                return;
            }

            await deleteEmails(getSelectedEmailItems());
        }

        async function confirmDeleteCurrentEmail() {
            if (isTempEmailGroup) return;
            if (!currentEmailDetail || !currentEmailDetail.id) return;

            if (!(await showConfirmModal('确定要永久删除这封邮件吗？此操作不可恢复！', { title: '删除邮件', confirmText: '确认删除' }))) {
                return;
            }

            await deleteEmails([currentEmailDetail]);
        }

        function removeDeletedEmailsFromCachedLists(deletedItems, account = currentAccount) {
            const deletedKeys = new Set();
            const deletedIds = new Set();
            (deletedItems || []).forEach(item => {
                if (item && typeof item === 'object') {
                    const key = getEmailSelectionKey(item);
                    if (key) {
                        deletedKeys.add(key);
                    }
                    if (item.id != null) {
                        deletedIds.add(String(item.id));
                    }
                    return;
                }
                if (item != null && item !== '') {
                    deletedIds.add(String(item));
                }
            });
            if (!deletedKeys.size && !deletedIds.size) {
                return;
            }

            const matchesDeleted = (email) => {
                const key = getEmailSelectionKey(email);
                return (key && deletedKeys.has(key)) || deletedIds.has(String(email.id));
            };

            const cachePrefixes = new Set([`${account || ''}_`]);
            if (isAggregatedInboxMode()) {
                cachePrefixes.add(`${getAggregatedInboxCacheAccountKey()}_`);
            }
            Object.entries(emailListCache).forEach(([cacheKey, cacheValue]) => {
                if (![...cachePrefixes].some(prefix => cacheKey.startsWith(prefix)) || !Array.isArray(cacheValue?.emails)) {
                    return;
                }
                const beforeCount = cacheValue.emails.length;
                cacheValue.emails = cacheValue.emails.filter(email => !matchesDeleted(email));
                const removedCount = beforeCount - cacheValue.emails.length;
                cacheValue.skip = isAggregatedInboxMode()
                    ? cacheValue.skip
                    : cacheValue.emails.length;
                if (typeof cacheValue.local_retention_count === 'number') {
                    cacheValue.local_retention_count = Math.max(0, cacheValue.local_retention_count - removedCount);
                }
            });
        }

        async function deleteEmails(sourceItems) {
            const items = buildEmailDeleteItems(sourceItems);
            if (!items.length) {
                return;
            }

            showToast('正在删除...', 'info');

            try {
                const requestGroups = groupEmailActionItemsByAccount(items);
                if (!requestGroups.size) {
                    showToast('删除失败: 缺少账号信息', 'error');
                    return;
                }

                const results = await Promise.all(
                    Array.from(requestGroups.entries()).map(async ([accountEmail, accountItems]) => {
                        const response = await fetch('/api/emails/delete', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                email: accountEmail,
                                method: getRemoteMailboxMethodFallback(),
                                folder: currentFolder,
                                items: accountItems.map(item => ({
                                    id: item.id,
                                    folder: item.folder,
                                    id_mode: item.id_mode
                                }))
                            })
                        });
                        const result = await response.json();
                        const deletedIdList = Array.isArray(result.deleted_ids) && result.deleted_ids.length
                            ? result.deleted_ids
                            : (Array.isArray(result.updated_ids) ? result.updated_ids : []);
                        const deletedItems = accountItems.filter(item =>
                            deletedIdList.map(String).includes(String(item.id))
                            || (result.success && !deletedIdList.length)
                        ).map(item => ({
                            ...item,
                            account_email: accountEmail
                        }));
                        return {
                            ...result,
                            deleted_items: deletedItems,
                            deleted_ids: deletedItems.map(item => item.id)
                        };
                    })
                );

                const deletedItems = results.flatMap(result => result.deleted_items || []);
                const deletedIds = new Set(deletedItems.map(item => String(item.id)));
                const successCount = results.reduce(
                    (sum, result) => sum + (Number(result.success_count) || (result.deleted_items || []).length),
                    0
                );
                const failedCount = results.reduce((sum, result) => sum + (Number(result.failed_count) || 0), 0);
                const anySuccess = deletedItems.length > 0 || results.some(result => result.success);

                if (anySuccess) {
                    if (failedCount === 0) {
                        showToast(`成功删除 ${successCount || deletedItems.length} 封邮件`);
                    } else if (deletedItems.length > 0) {
                        showToast(`已删除 ${deletedItems.length} 封，失败 ${failedCount} 封`, 'warning');
                    }

                    currentEmails = currentEmails.filter(email => {
                        const key = getEmailSelectionKey(email);
                        return !deletedItems.some(item => getEmailSelectionKey(item) === key)
                            && !deletedIds.has(String(email.id));
                    });
                    removeDeletedEmailsFromCachedLists(deletedItems);
                    selectedEmailIds.clear();
                    const currentSelectionKey = currentEmailDetail
                        ? getEmailSelectionKey(currentEmailDetail)
                        : '';
                    if (currentEmailId && (
                        deletedItems.some(item => getEmailSelectionKey(item) === currentSelectionKey)
                        || deletedIds.has(String(currentEmailId))
                    )) {
                        currentEmailId = null;
                    }

                    renderEmailList(currentEmails);

                    // If current viewed email was deleted, clear view
                    if (currentEmailDetail && (
                        deletedItems.some(item => getEmailSelectionKey(item) === getEmailSelectionKey(currentEmailDetail))
                        || deletedIds.has(String(currentEmailDetail.id))
                    )) {
                        currentEmailId = null;
                        currentEmailDetail = null;
                        document.getElementById('emailDetail').innerHTML = `
                            <div class="empty-state">
                                <div class="empty-state-icon">🗑️</div>
                                <div class="empty-state-text">邮件已删除</div>
                            </div>
                        `;
                        document.getElementById('emailDetailToolbar').style.display = 'none';
                        resetEmailTranslateUi();
                    }

                    if (failedCount > 0) {
                        console.warn('Deletion errors:', results.flatMap(result => result.errors || []));
                    }
                } else {
                    const firstError = results[0] || {};
                    const errorMessage = firstError.error && firstError.error.message
                        ? firstError.error.message
                        : (firstError.error || '未知错误');
                    showToast('删除失败: ' + errorMessage, 'error');
                }
            } catch (e) {
                showToast('网络错误', 'error');
                console.error(e);
            }
        }

        // 选择邮件
        async function selectEmail(messageId, index) {
            const selectedEmail = Number.isInteger(index) && currentEmails[index]
                ? currentEmails[index]
                : currentEmails.find(email => String(email.id) === String(messageId));
            const selectionKey = selectedEmail ? getEmailSelectionKey(selectedEmail) : String(messageId || '');
            currentEmailId = selectionKey || messageId;
            const requestFolder = currentFolder === 'all'
                ? (selectedEmail?.folder || 'inbox')
                : currentFolder;
            const accountEmail = getEmailAccountAddress(selectedEmail)
                || (!isAggregatedInboxMode() ? currentAccount : '');
            if (!accountEmail) {
                showToast('无法确定邮件所属账号', 'error');
                return;
            }
            // 更新 UI（按 selection key 高亮，避免状态筛选后 DOM 下标错位）
            document.querySelectorAll('.email-item').forEach((item) => {
                const itemKey = String(item.dataset.emailSelectionKey || '');
                const itemId = String(item.dataset.emailId || '');
                item.classList.toggle(
                    'active',
                    itemKey === String(currentEmailId || '')
                    || itemId === String(currentEmailId || '')
                    || itemId === String(messageId || '')
                );
            });

            // 这里不重置 currentEmailDetail，等到 fetch 成功后再设置

            // 重置信任模式
            const trustCheckbox = document.getElementById('trustEmailCheckbox');
            trustCheckbox.checked = false;
            isTrustedMode = false;
            updateTrustToggleState(trustCheckbox);

            // 显示工具栏
            document.getElementById('emailDetailToolbar').style.display = 'flex';
            const deleteBtn = document.querySelector('#emailDetailToolbar .batch-btn.danger');
            if (deleteBtn) deleteBtn.style.display = '';
            showMobileEmailDetail();

            // 加载邮件详情
            const container = document.getElementById('emailDetail');
            container.innerHTML = '<div class="loading"><div class="loading-spinner"></div></div>';

            try {
                const response = await fetchWithTimeout(
                    buildEmailDetailRequestUrl(messageId, requestFolder, selectedEmail || {}),
                    {
                        timeoutMs: EMAIL_DETAIL_REQUEST_TIMEOUT_MS,
                        timeoutMessage: '加载邮件详情超时，请稍后重试'
                    }
                );
                const data = await response.json();

                if (data.success) {
                    currentEmailDetail = {
                        ...data.email,
                        folder: requestFolder,
                        id_mode: data.email?.id_mode || selectedEmail?.id_mode || '',
                        account_id: selectedEmail?.account_id,
                        account_email: accountEmail
                    };
                    renderEmailDetail(currentEmailDetail);
                    if (isEmailUnread(selectedEmail)) {
                        void requestMarkEmailsAsRead([{
                            id: messageId,
                            folder: requestFolder,
                            id_mode: selectedEmail.id_mode || '',
                            account_id: selectedEmail.account_id,
                            account_email: accountEmail
                        }], { silent: true });
                    }
                } else {
                    handleApiError(data, '加载邮件详情失败');
                    const detailErrorMessage = data.error?.message
                        || (typeof data.error === 'string' ? data.error : '')
                        || '加载失败';
                    const hasProtocolDetails = data.details
                        && typeof data.details === 'object'
                        && Object.keys(data.details).length > 0;
                    if (hasProtocolDetails) {
                        window._lastFetchErrorDetails = data.details;
                    }
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">⚠️</div>
                            <div class="empty-state-text"></div>
                            ${hasProtocolDetails ? '<div class="empty-state-actions" style="margin-top:12px;"><a href="javascript:void(0)" class="email-detail-error-link" style="color:#409eff;text-decoration:underline;">点击查看详情</a></div>' : ''}
                        </div>
                    `;
                    const errorText = container.querySelector('.empty-state-text');
                    if (errorText) {
                        errorText.textContent = detailErrorMessage;
                    }
                    const detailLink = container.querySelector('.email-detail-error-link');
                    if (detailLink) {
                        detailLink.addEventListener('click', () => {
                            showEmailFetchErrorModal(window._lastFetchErrorDetails);
                        });
                    }
                }
            } catch (error) {
                const errorMessage = isTimeoutAbortError(error)
                    ? '加载邮件详情超时，请重试'
                    : '网络错误，请重试';
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">⚠️</div>
                        <div class="empty-state-text">${errorMessage}</div>
                    </div>
                `;
            }
        }

        // 渲染邮件详情
        function renderEmailDetail(email) {
            cleanupNormalDetailIframeResizeResources();
            const container = document.getElementById('emailDetail');
            const compactMobileMeta = typeof isMobileLayout === 'function' && isMobileLayout();

            const isHtml = email.body_type === 'html' ||
                (email.body && (email.body.includes('<html') || email.body.includes('<div') || email.body.includes('<p>')));

            const bodyContent = isHtml
                ? `<iframe id="emailBodyFrame" sandbox="allow-same-origin" onload="adjustIframeHeight(this)"></iframe>`
                : `<div class="email-body-text">${escapeHtml(email.body)}</div>`;

            const detailMetaRows = `
                <div class="email-detail-meta-row">
                    <span class="email-detail-meta-label">发件人</span>
                    <span class="email-detail-meta-value">${escapeHtml(email.from)}</span>
                </div>
                <div class="email-detail-meta-row">
                    <span class="email-detail-meta-label">收件人</span>
                    <span class="email-detail-meta-value">${escapeHtml(email.to || '-')}</span>
                </div>
                ${email.cc ? `
                <div class="email-detail-meta-row">
                    <span class="email-detail-meta-label">抄送</span>
                    <span class="email-detail-meta-value">${escapeHtml(email.cc)}</span>
                </div>
                ` : ''}
                <div class="email-detail-meta-row">
                    <span class="email-detail-meta-label">时间</span>
                    <span class="email-detail-meta-value">${formatDate(email.date)}</span>
                </div>
            `;

            const detailHeader = compactMobileMeta
                ? `
                <div class="email-detail-header email-detail-header--compact">
                    <div class="email-detail-subject">${escapeHtml(email.subject || '无主题')}</div>
                    <div class="email-detail-meta-inline">
                        <span class="email-detail-meta-inline__from">${escapeHtml(email.from || '未知发件人')}</span>
                        <span class="email-detail-meta-inline__dot"></span>
                        <span class="email-detail-meta-inline__time">${formatDate(email.date)}</span>
                    </div>
                    <details class="email-detail-meta-collapsible">
                        <summary class="email-detail-meta-collapsible__summary">查看邮件信息</summary>
                        <div class="email-detail-meta email-detail-meta--compact">
                            ${detailMetaRows}
                        </div>
                    </details>
                </div>
                `
                : `
                <div class="email-detail-header">
                    <div class="email-detail-subject">${escapeHtml(email.subject || '无主题')}</div>
                    <div class="email-detail-meta">
                        ${detailMetaRows}
                    </div>
                </div>
                `;

            container.innerHTML = `
                ${detailHeader}
                <div class="email-detail-body">
                    ${renderAttachmentSection(email)}
                    ${bodyContent}
                </div>
            `;

            emailTranslateViewMode = 'original';
            emailTranslateActiveProvider = '';
            emailTranslateInFlightProvider = '';
            updateTranslateEmailButtonLabel();

            // 如果是 HTML 内容，设置 iframe 内容
            if (isHtml) {
                const iframe = document.getElementById('emailBodyFrame');
                if (iframe) {
                    let sanitizedBody;
                    if (isTrustedMode) {
                        sanitizedBody = email.body; // 信任模式：不过滤
                    } else {
                        // 使用 DOMPurify 净化 HTML 内容，防止 XSS 攻击
                        sanitizedBody = DOMPurify.sanitize(email.body, {
                            ALLOWED_TAGS: ['a', 'b', 'i', 'u', 'strong', 'em', 'p', 'br', 'div', 'span', 'img', 'table', 'tr', 'td', 'th', 'thead', 'tbody', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code'],
                            ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'style', 'class', 'width', 'height', 'align', 'border', 'cellpadding', 'cellspacing'],
                            ALLOW_DATA_ATTR: false,
                            FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button'],
                            FORBID_ATTR: ['onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur']
                        });
                    }

                    const htmlContent = `
                        <!DOCTYPE html>
                        <html>
                        <head>
                            <meta charset="UTF-8">
                            <style>
                                body {
                                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                                    font-size: 15px;
                                    line-height: 1.6;
                                    color: #333;
                                    margin: 0;
                                    padding: 0;
                                    background-color: #ffffff;
                                }
                                img {
                                    max-width: 100%;
                                    height: auto;
                                }
                                a {
                                    color: #0078d4;
                                }
                            </style>
                        </head>
                        <body>${sanitizedBody}</body>
                        </html>
                    `;
                    iframe.srcdoc = htmlContent;
                }
            }
        }

        // 动态调整 iframe 高度
        function adjustIframeHeight(iframe) {
            cleanupNormalDetailIframeResizeResources();
            try {
                const adjustHeight = () => {
                    if (!iframe.isConnected) {
                        return;
                    }
                    if (iframe.contentDocument && iframe.contentDocument.body) {
                        const body = iframe.contentDocument.body;
                        const html = iframe.contentDocument.documentElement;
                        const height = Math.max(
                            body.scrollHeight,
                            body.offsetHeight,
                            html.clientHeight,
                            html.scrollHeight,
                            html.offsetHeight
                        );
                        iframe.style.height = Math.max(height + 100, 600) + 'px';
                    }
                };

                adjustHeight();
                [100, 300, 500, 1000, 2000].forEach(delay => {
                    normalDetailIframeResizeResources.timers.push(window.setTimeout(adjustHeight, delay));
                });

                if (iframe.contentDocument) {
                    normalDetailIframeResizeResources.observer = new MutationObserver(adjustHeight);
                    normalDetailIframeResizeResources.observer.observe(iframe.contentDocument.body, {
                        childList: true,
                        subtree: true,
                        attributes: true
                    });

                    const images = iframe.contentDocument.querySelectorAll('img');
                    images.forEach(img => {
                        img.addEventListener('load', adjustHeight);
                        img.addEventListener('error', adjustHeight);
                    });
                }
            } catch (e) {
                console.log('Cannot adjust iframe height:', e);
            }
        }

        // 全屏查看邮件
        let currentFullscreenEmail = null;
        let currentRawEmailSource = '';
        let currentRawEmailFilename = 'message.eml';

        function openFullscreenEmail() {
            const emailDetail = document.getElementById('emailDetail');
            const modal = document.getElementById('fullscreenEmailModal');
            const content = document.getElementById('fullscreenEmailContent');
            const title = document.getElementById('fullscreenEmailTitle');

            // 获取当前邮件的标题
            const subjectElement = emailDetail.querySelector('.email-detail-subject');
            if (subjectElement) {
                title.textContent = subjectElement.textContent;
            }

            // 克隆邮件内容
            const emailHeader = emailDetail.querySelector('.email-detail-header');
            const emailBody = emailDetail.querySelector('.email-detail-body');

            if (emailHeader && emailBody) {
                cleanupFullscreenIframeResizeResources();
                // 清空内容
                content.innerHTML = '';

                // 克隆头部信息
                const headerClone = emailHeader.cloneNode(true);
                content.appendChild(headerClone);

                // 处理邮件正文
                const iframe = emailBody.querySelector('iframe');
                const textContent = emailBody.querySelector('.email-body-text');

                if (iframe) {
                    // 如果是 HTML 邮件，创建新的 iframe
                    const newIframe = document.createElement('iframe');
                    newIframe.id = 'fullscreenEmailBodyFrame';
                    newIframe.style.width = '100%';
                    newIframe.style.border = 'none';
                    newIframe.style.backgroundColor = '#ffffff';

                    // 复制原 iframe 的内容
                    if (iframe.contentDocument) {
                        const htmlContent = iframe.contentDocument.documentElement.outerHTML;
                        newIframe.srcdoc = htmlContent;
                    }

                    content.appendChild(newIframe);

                    // 调整 iframe 高度
                    newIframe.onload = function () {
                        adjustFullscreenIframeHeight(newIframe);
                    };
                } else if (textContent) {
                    // 如果是纯文本邮件，直接克隆
                    const textClone = textContent.cloneNode(true);
                    content.appendChild(textClone);
                }

                // 显示模态框
                modal.classList.add('show');
                updateModalBodyState();
            }
        }

        // 切换信任模式
        function updateTrustToggleState(checkbox) {
            checkbox?.closest('.email-trust-toggle')?.classList.toggle('is-active', !!checkbox?.checked);
        }

        async function toggleTrustMode(checkbox) {
            updateTrustToggleState(checkbox);
            if (checkbox.checked) {
                if (await showConfirmModal('⚠️ 警告：启用信任模式将直接显示邮件原始内容，不进行任何安全过滤。\n\n这可能包含恶意脚本或不安全的内容。您确定要继续吗？', { title: '启用信任模式', confirmText: '确认启用' })) {
                    isTrustedMode = true;
                    if (currentEmailDetail) {
                        renderEmailDetail(currentEmailDetail);
                    }
                } else {
                    checkbox.checked = false;
                    updateTrustToggleState(checkbox);
                }
            } else {
                isTrustedMode = false;
                if (currentEmailDetail) {
                    renderEmailDetail(currentEmailDetail);
                }
            }
        }

        function closeFullscreenEmail() {
            cleanupFullscreenIframeResizeResources();
            const modal = document.getElementById('fullscreenEmailModal');
            if (!modal) return;
            modal.classList.remove('show');
            updateModalBodyState();
        }

        const emailTranslateCache = {};
        let emailTranslateViewMode = 'original';
        let emailTranslateActiveProvider = '';
        let emailTranslateInFlightProvider = '';

        function getEmailTranslateCacheKey(email = currentEmailDetail) {
            if (!email) return '';
            const account = String(email.account_email || currentAccount || '').trim();
            const messageId = String(email.id || '').trim();
            if (!account || !messageId) return '';
            return `${account}|${messageId}`;
        }

        function getEmailTranslateBucket(cacheKey) {
            if (!cacheKey) return null;
            if (!emailTranslateCache[cacheKey]) {
                emailTranslateCache[cacheKey] = {};
            }
            return emailTranslateCache[cacheKey];
        }

        function providerDisplayName(provider, model) {
            if (provider === 'mymemory') return 'MyMemory 免费翻译';
            if (provider === 'ai' || provider === 'gemini' || provider === 'deepseek') {
                const name = provider === 'ai' ? 'AI' : String(provider).toUpperCase();
                return model ? `AI 翻译（${name} / ${model}）` : 'AI 翻译';
            }
            return provider ? `翻译（${provider}）` : '翻译';
        }

        function updateTranslateEmailButtonLabel() {
            const configs = [
                {
                    provider: 'mymemory',
                    btnId: 'translateEmailBtn',
                    textId: 'translateEmailBtnText',
                    idle: '免费翻译',
                    busy: '翻译中…',
                },
                {
                    provider: 'ai',
                    btnId: 'aiTranslateEmailBtn',
                    textId: 'aiTranslateEmailBtnText',
                    idle: 'AI翻译',
                    busy: 'AI翻译中…',
                },
            ];
            const cacheKey = getEmailTranslateCacheKey();
            const bucket = cacheKey ? emailTranslateCache[cacheKey] : null;

            configs.forEach((cfg) => {
                const textEl = document.getElementById(cfg.textId);
                const btn = document.getElementById(cfg.btnId);
                if (!textEl || !btn) return;
                if (emailTranslateInFlightProvider) {
                    btn.disabled = true;
                    textEl.textContent = emailTranslateInFlightProvider === cfg.provider ? cfg.busy : cfg.idle;
                    return;
                }
                btn.disabled = false;
                const hasCache = !!(bucket && bucket[cfg.provider]);
                if (!hasCache) {
                    textEl.textContent = cfg.idle;
                    return;
                }
                if (
                    emailTranslateViewMode === 'translation'
                    && emailTranslateActiveProvider === cfg.provider
                ) {
                    textEl.textContent = '显示原文';
                    return;
                }
                textEl.textContent = '显示译文';
            });
        }

        function setEmailOriginalBodyVisible(visible) {
            const detailBody = document.querySelector('#emailDetail .email-detail-body');
            if (!detailBody) return;
            detailBody.querySelectorAll('#emailBodyFrame, .email-body-text').forEach((node) => {
                node.style.display = visible ? '' : 'none';
            });
        }

        function ensureEmailTranslatePanel() {
            const detailBody = document.querySelector('#emailDetail .email-detail-body');
            if (!detailBody) return null;
            let panel = detailBody.querySelector('.email-translate-panel');
            if (panel) return panel;
            panel = document.createElement('div');
            panel.className = 'email-translate-panel';
            panel.innerHTML = `
                <div class="email-translate-panel__head">
                    <div class="email-translate-panel__title">中文译文</div>
                    <button type="button" class="email-translate-panel__toggle" id="emailTranslateToggleBtn">显示原文</button>
                </div>
                <div class="email-translate-panel__subject" id="emailTranslateSubject"></div>
                <div class="email-translate-panel__body" id="emailTranslateBody"></div>
                <div class="email-translate-panel__foot"></div>
            `;
            const firstChild = detailBody.firstElementChild;
            if (firstChild) {
                detailBody.insertBefore(panel, firstChild);
            } else {
                detailBody.appendChild(panel);
            }
            const toggleBtn = panel.querySelector('#emailTranslateToggleBtn');
            if (toggleBtn) {
                toggleBtn.addEventListener('click', () => {
                    emailTranslateViewMode = emailTranslateViewMode === 'translation' ? 'original' : 'translation';
                    applyEmailTranslateViewMode();
                });
            }
            return panel;
        }

        function applyEmailTranslateViewMode() {
            const panel = document.querySelector('#emailDetail .email-translate-panel');
            const showingTranslation = emailTranslateViewMode === 'translation';
            if (panel) {
                panel.style.display = showingTranslation ? '' : 'none';
                const toggleBtn = panel.querySelector('#emailTranslateToggleBtn');
                if (toggleBtn) {
                    toggleBtn.textContent = showingTranslation ? '显示原文' : '显示译文';
                }
            }
            setEmailOriginalBodyVisible(!showingTranslation);
            updateTranslateEmailButtonLabel();
        }

        function renderEmailTranslatePanel(payload, providerKey) {
            const panel = ensureEmailTranslatePanel();
            if (!panel) return;
            const subjectEl = document.getElementById('emailTranslateSubject');
            const bodyEl = document.getElementById('emailTranslateBody');
            const subjectZh = String(payload?.subject_translation || '').trim();
            const bodyZh = String(payload?.body_translation || payload?.translation || '').trim();
            if (subjectEl) {
                if (subjectZh) {
                    subjectEl.style.display = '';
                    subjectEl.textContent = subjectZh;
                } else {
                    subjectEl.style.display = 'none';
                    subjectEl.textContent = '';
                }
            }
            if (bodyEl) {
                bodyEl.textContent = bodyZh || '（译文为空）';
            }
            const foot = panel.querySelector('.email-translate-panel__foot');
            if (foot) {
                const notes = [providerDisplayName(payload?.provider || providerKey, payload?.model)];
                if (payload?.truncated) notes.push('原文过长已截断');
                foot.textContent = notes.join(' · ');
            }
            emailTranslateActiveProvider = providerKey;
            emailTranslateViewMode = 'translation';
            applyEmailTranslateViewMode();
        }

        function resetEmailTranslateUi() {
            emailTranslateViewMode = 'original';
            emailTranslateActiveProvider = '';
            emailTranslateInFlightProvider = '';
            const panel = document.querySelector('#emailDetail .email-translate-panel');
            if (panel) panel.remove();
            setEmailOriginalBodyVisible(true);
            updateTranslateEmailButtonLabel();
        }

        function buildEmailTranslateRequestPayload() {
            const body = String(currentEmailDetail?.body || '');
            const isHtml = currentEmailDetail?.body_type === 'html'
                || (body && (body.includes('<html') || body.includes('<div') || body.includes('<p>')));
            return {
                subject: String(currentEmailDetail?.subject || '').trim(),
                html: isHtml ? body : '',
                text: isHtml ? '' : body,
                source_lang: 'en',
            };
        }

        async function ensureAiTranslateReady() {
            try {
                const response = await fetchWithTimeout('/api/ai/status');
                const data = await response.json().catch(() => ({}));
                if (!data.enabled) {
                    showToast('请先在 /ai 启用 AI', 'error');
                    return false;
                }
                if (!data.ready) {
                    showToast('请先在 /ai 配置当前提供商 API Key', 'error');
                    return false;
                }
                return true;
            } catch (error) {
                showToast(error?.message || '无法读取 AI 状态', 'error');
                return false;
            }
        }

        async function toggleEmailTranslation(provider = 'mymemory') {
            const providerKey = provider === 'ai' ? 'ai' : 'mymemory';
            if (!currentEmailDetail) {
                showToast('请先选择一封邮件', 'warning');
                return;
            }
            if (emailTranslateInFlightProvider) return;

            const cacheKey = getEmailTranslateCacheKey(currentEmailDetail);
            if (!cacheKey) {
                showToast('无法识别当前邮件', 'error');
                return;
            }

            const bucket = getEmailTranslateBucket(cacheKey);
            const cached = bucket[providerKey];
            if (cached) {
                if (
                    emailTranslateViewMode === 'translation'
                    && emailTranslateActiveProvider === providerKey
                ) {
                    emailTranslateViewMode = 'original';
                    applyEmailTranslateViewMode();
                } else {
                    renderEmailTranslatePanel(cached, providerKey);
                }
                return;
            }

            if (providerKey === 'ai') {
                const ready = await ensureAiTranslateReady();
                if (!ready) return;
            }

            const payload = buildEmailTranslateRequestPayload();
            if (!payload.subject && !payload.html && !payload.text) {
                showToast('当前邮件没有可翻译内容', 'warning');
                return;
            }

            emailTranslateInFlightProvider = providerKey;
            updateTranslateEmailButtonLabel();
            const endpoint = providerKey === 'ai' ? '/api/ai/translate' : '/api/emails/translate';
            try {
                const response = await fetchWithTimeout(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    timeoutMs: providerKey === 'ai' ? 90000 : 60000,
                    timeoutMessage: '翻译超时，请稍后重试',
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    handleApiError(data, data.error || '翻译失败');
                    return;
                }
                bucket[providerKey] = {
                    translation: data.translation || '',
                    subject_translation: data.subject_translation || '',
                    body_translation: data.body_translation || '',
                    provider: data.provider || providerKey,
                    model: data.model || '',
                    truncated: !!data.truncated,
                };
                renderEmailTranslatePanel(bucket[providerKey], providerKey);
                showToast(data.truncated ? '已翻译（原文过长已截断）' : '翻译完成', 'success');
            } catch (error) {
                showToast(error?.message || '翻译失败', 'error');
            } finally {
                emailTranslateInFlightProvider = '';
                updateTranslateEmailButtonLabel();
            }
        }

        async function openRawEmailModal() {
            if (!currentEmailDetail || !currentEmailDetail.id || !currentAccount) {
                showToast('请先选择一封邮件', 'warning');
                return;
            }

            const modal = document.getElementById('rawEmailModal');
            const content = document.getElementById('rawEmailContent');
            const title = document.getElementById('rawEmailTitle');
            const warning = document.getElementById('rawEmailWarning');
            if (!modal || !content) return;

            currentRawEmailSource = '';
            currentRawEmailFilename = `${currentEmailDetail.id || 'message'}.eml`;
            title.textContent = currentEmailDetail.subject ? `原始邮件：${currentEmailDetail.subject}` : '原始邮件';
            warning.textContent = '原始邮件包含完整邮件头和路由信息，请谨慎分享。';
            content.textContent = '正在加载原始邮件源码...';
            modal.classList.add('show');
            updateModalBodyState();

            const folder = encodeURIComponent(currentEmailDetail.folder || currentFolder || 'inbox');
            const method = encodeURIComponent(getCurrentEmailRemoteActionMethod(currentEmailDetail));
            try {
                const response = await fetchWithTimeout(
                    `/api/email/${encodeURIComponent(currentAccount)}/${encodeURIComponent(currentEmailDetail.id)}/raw?method=${method}&folder=${folder}`,
                    {
                        timeoutMs: EMAIL_DETAIL_REQUEST_TIMEOUT_MS,
                        timeoutMessage: '加载原始邮件超时，请稍后重试'
                    }
                );
                const data = await response.json();
                if (!data.success) {
                    handleApiError(data, '加载原始邮件失败');
                    content.textContent = data.error && data.error.message ? data.error.message : (data.error || '加载原始邮件失败');
                    return;
                }
                currentRawEmailSource = data.raw || '';
                currentRawEmailFilename = data.filename || currentRawEmailFilename;
                if (data.warning) {
                    warning.textContent = data.warning;
                }
                content.textContent = currentRawEmailSource || '原始邮件为空';
            } catch (error) {
                const errorMessage = isTimeoutAbortError(error)
                    ? '加载原始邮件超时，请重试'
                    : '网络错误，请重试';
                content.textContent = errorMessage;
                showToast(errorMessage, 'error');
            }
        }

        function closeRawEmailModal() {
            const modal = document.getElementById('rawEmailModal');
            if (!modal) return;
            modal.classList.remove('show');
            updateModalBodyState();
        }

        function closeRawEmailOnBackdrop(event) {
            if (event.target.id === 'rawEmailModal') {
                closeRawEmailModal();
            }
        }

        async function copyRawEmailSource() {
            if (!currentRawEmailSource) {
                showToast('暂无可复制的原始邮件内容', 'warning');
                return;
            }
            try {
                await navigator.clipboard.writeText(currentRawEmailSource);
                showToast('原始邮件已复制');
            } catch (error) {
                showToast('复制失败，请手动选择复制', 'error');
            }
        }

        function downloadRawEmailSource() {
            if (!currentRawEmailSource) {
                showToast('暂无可下载的原始邮件内容', 'warning');
                return;
            }
            const blob = new Blob([currentRawEmailSource], { type: 'message/rfc822;charset=utf-8' });
            triggerAttachmentDownload(blob, currentRawEmailFilename || 'message.eml');
            showToast('原始邮件下载已开始');
        }

        function closeFullscreenEmailOnBackdrop(event) {
            // 只有点击背景时才关闭，点击内容区域不关闭
            if (event.target.id === 'fullscreenEmailModal') {
                closeFullscreenEmail();
            }
        }

        function adjustFullscreenIframeHeight(iframe) {
            cleanupFullscreenIframeResizeResources();
            try {
                const adjustHeight = () => {
                    if (!iframe.isConnected) {
                        return;
                    }
                    if (iframe.contentDocument && iframe.contentDocument.body) {
                        const body = iframe.contentDocument.body;
                        const html = iframe.contentDocument.documentElement;
                        const height = Math.max(
                            body.scrollHeight,
                            body.offsetHeight,
                            html.clientHeight,
                            html.scrollHeight,
                            html.offsetHeight
                        );
                        iframe.style.height = (height + 100) + 'px';
                    }
                };

                adjustHeight();
                [100, 300, 500, 1000].forEach(delay => {
                    fullscreenIframeResizeResources.timers.push(window.setTimeout(adjustHeight, delay));
                });

                if (iframe.contentDocument) {
                    fullscreenIframeResizeResources.observer = new MutationObserver(adjustHeight);
                    fullscreenIframeResizeResources.observer.observe(iframe.contentDocument.body, {
                        childList: true,
                        subtree: true,
                        attributes: true
                    });

                    const images = iframe.contentDocument.querySelectorAll('img');
                    images.forEach(img => {
                        img.addEventListener('load', adjustHeight);
                        img.addEventListener('error', adjustHeight);
                    });
                }
            } catch (e) {
                console.log('Cannot adjust fullscreen iframe height:', e);
            }
        }
        // 显示邮件列表（移动端）
        function showEmailList({ scheduleLoadCheck = true } = {}) {
            document.getElementById('emailListPanel').classList.remove('hidden');
            isListVisible = true;
            document.getElementById('toggleListText').textContent = '隐藏列表';
            closeMobilePanels();
            closeNavbarActionsMenu();
            updateMobileContext();
            if (scheduleLoadCheck) {
                scheduleEmailListLoadCheck(0);
            }
        }

        function syncEmailSearchClearButton() {
            const clearBtn = document.getElementById('emailSearchClearBtn');
            if (clearBtn) {
                clearBtn.hidden = !getEmailSearchKeyword();
            }
        }

        function setEmailKeyword(keyword, options = {}) {
            currentEmailKeyword = String(keyword || '').trim();
            const input = document.getElementById('emailKeywordInput');
            if (input && input.value !== currentEmailKeyword) {
                input.value = currentEmailKeyword;
            }
            syncEmailSearchClearButton();
            if (options.search === false) {
                return;
            }
            if (typeof renderEmailList === 'function') {
                renderEmailList(currentEmails);
            }
            const emailCount = document.getElementById('emailCount');
            if (emailCount && Array.isArray(currentEmails)) {
                emailCount.textContent = `(${getVisibleEmailsForCurrentFilter(currentEmails).length})`;
            }
            if (currentEmailKeyword) {
                void hydrateEmailSearchFromLocal();
            }
        }

        async function hydrateEmailSearchFromLocal() {
            const keyword = getEmailSearchKeyword();
            if (!keyword || isTempEmailGroup) {
                return false;
            }
            if (typeof isNormalMailLocalRetentionEnabled !== 'function' || !isNormalMailLocalRetentionEnabled()) {
                return false;
            }
            if (!isAggregatedInboxMode() && !currentAccount) {
                return false;
            }
            const account = currentAccount;
            const folder = currentFolder;
            try {
                const data = await fetchRemoteEmails(
                    isAggregatedInboxMode() ? AGGREGATED_INBOX_ACCOUNT_KEY : account,
                    `${isAggregatedInboxMode() ? getAggregatedInboxCacheAccountKey() : account}_${folder}`,
                    {
                        aggregated: isAggregatedInboxMode(),
                        source: 'local',
                        folder,
                        skip: 0,
                        top: 200,
                        keyword,
                        mergeWithCurrentList: true,
                        keepSyncStatus: true,
                        method: isAggregatedInboxMode() ? 'aggregated' : 'local',
                        methodLabel: 'Local Retention',
                        context: {
                            account: isAggregatedInboxMode() ? AGGREGATED_INBOX_ACCOUNT_KEY : account,
                            folder,
                            aggregated: isAggregatedInboxMode()
                        }
                    }
                );
                return Boolean(data && data.success);
            } catch (error) {
                return false;
            }
        }

        function initEmailKeywordSearch() {
            const input = document.getElementById('emailKeywordInput');
            if (!input || input.dataset.bound === 'true') {
                return;
            }
            input.dataset.bound = 'true';
            const applySearch = typeof debounce === 'function'
                ? debounce(() => setEmailKeyword(input.value), 280)
                : () => setEmailKeyword(input.value);
            input.addEventListener('input', applySearch);
            input.addEventListener('keydown', event => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    setEmailKeyword(input.value);
                }
                if (event.key === 'Escape') {
                    event.preventDefault();
                    setEmailKeyword('');
                }
            });
            const clearBtn = document.getElementById('emailSearchClearBtn');
            if (clearBtn) {
                clearBtn.addEventListener('click', () => setEmailKeyword(''));
            }
            syncEmailSearchClearButton();
        }

        function clampEmailFetchTop(value) {
            const parsed = Number.parseInt(String(value ?? '').trim(), 10);
            if (!Number.isFinite(parsed)) {
                return EMAIL_FETCH_TOP_DEFAULT;
            }
            return Math.min(EMAIL_FETCH_TOP_MAX, Math.max(EMAIL_FETCH_TOP_MIN, parsed));
        }

        function getEmailFetchTop() {
            const input = document.getElementById('emailFetchTopInput');
            if (input) {
                return clampEmailFetchTop(input.value);
            }
            try {
                return clampEmailFetchTop(window.localStorage.getItem(EMAIL_FETCH_TOP_STORAGE_KEY));
            } catch (error) {
                return EMAIL_FETCH_TOP_DEFAULT;
            }
        }

        function persistEmailFetchTop(value) {
            const top = clampEmailFetchTop(value);
            const input = document.getElementById('emailFetchTopInput');
            if (input) {
                input.value = String(top);
            }
            try {
                window.localStorage.setItem(EMAIL_FETCH_TOP_STORAGE_KEY, String(top));
            } catch (error) {
                // ignore quota / private mode
            }
            const refreshBtn = document.querySelector('.refresh-btn');
            if (refreshBtn && !refreshBtn.classList.contains('spinning')) {
                refreshBtn.title = `获取最近 ${top} 封`;
            }
            return top;
        }

        function initEmailFetchTopInput() {
            const input = document.getElementById('emailFetchTopInput');
            if (!input || input.dataset.bound === 'true') {
                return;
            }
            input.dataset.bound = 'true';
            let stored = EMAIL_FETCH_TOP_DEFAULT;
            try {
                stored = clampEmailFetchTop(window.localStorage.getItem(EMAIL_FETCH_TOP_STORAGE_KEY));
            } catch (error) {
                stored = EMAIL_FETCH_TOP_DEFAULT;
            }
            input.value = String(stored);
            persistEmailFetchTop(stored);
            const commit = () => persistEmailFetchTop(input.value);
            input.addEventListener('change', commit);
            input.addEventListener('blur', commit);
            input.addEventListener('keydown', event => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    persistEmailFetchTop(input.value);
                    refreshEmails();
                }
            });
        }

        async function fetchRecentEmails() {
            if (isFetchingRecentEmails) {
                return;
            }
            if (isTempEmailGroup) {
                showToast('临时邮箱请使用普通刷新', 'warning');
                return;
            }
            if (!currentAccount && !isAggregatedInboxMode()) {
                showToast('请先选择一个邮箱账号', 'error');
                return;
            }

            const fetchTop = persistEmailFetchTop(getEmailFetchTop());
            const runSeq = mailboxViewSeq;
            const context = getCurrentMailboxContext();
            const stillSameView = () => runSeq === mailboxViewSeq && isCurrentMailboxContext(context);
            isFetchingRecentEmails = true;
            setEmailListLoadingState(true);
            setMailSyncStatus(`正在获取最近 ${fetchTop} 封邮件…`);
            const aggregated = context.aggregated === true;
            const account = aggregated ? AGGREGATED_INBOX_ACCOUNT_KEY : context.account;
            const cacheAccountKey = aggregated ? getAggregatedInboxCacheAccountKey() : context.account;
            const cacheKey = `${cacheAccountKey}_${context.folder}`;
            if (typeof invalidateEmailListCache === 'function') {
                invalidateEmailListCache(cacheAccountKey, context.folder);
            }

            try {
                let skip = 0;
                let pageIndex = 0;
                while (skip < fetchTop) {
                    if (!stillSameView()) {
                        return;
                    }
                    const pageTop = Math.min(EMAIL_FETCH_PAGE_SIZE, fetchTop - skip);
                    const data = await fetchRemoteEmails(account, cacheKey, {
                        aggregated,
                        skip,
                        top: pageTop,
                        keyword: '',
                        mergeWithCurrentList: pageIndex > 0,
                        keepSyncStatus: true,
                        method: aggregated ? 'aggregated' : undefined,
                        context
                    });
                    if (!stillSameView()) {
                        return;
                    }
                    if (!data || data.success !== true) {
                        break;
                    }
                    pageIndex += 1;
                    skip += pageTop;
                    setMailSyncStatus(`正在获取最近 ${fetchTop} 封邮件…已 ${currentEmails.length} 封`);
                    if (data.has_more !== true || currentEmails.length >= fetchTop) {
                        break;
                    }
                }
                if (!stillSameView()) {
                    return;
                }
                const reachedCap = currentEmails.length >= fetchTop && hasMoreEmails;
                const message = reachedCap
                    ? `已获取最近 ${currentEmails.length} 封`
                    : `已获取 ${currentEmails.length} 封邮件`;
                setMailSyncStatus(message);
                showToast(message);
            } catch (error) {
                if (stillSameView()) {
                    showToast(isTimeoutAbortError(error) ? '获取最近邮件超时' : '获取最近邮件失败', 'error');
                }
            } finally {
                if (runSeq === mailboxViewSeq) {
                    isFetchingRecentEmails = false;
                    setEmailListLoadingState(false);
                }
            }
        }

        // 刷新邮件
        function refreshEmails() {
            if (currentAccount) {
                if (isTempEmailGroup) {
                    if (currentMethod === 'cloudflare-admin') {
                        loadCloudflareGlobalMessages();
                    } else {
                        loadTempEmailMessages(currentAccount);
                    }
                } else {
                    void fetchRecentEmails();
                }
            } else {
                showToast('请先选择一个邮箱账号', 'error');
            }
        }

        function copyTextToClipboard(text, successMessage = '内容已复制') {
            const fallbackCopy = () => {
                const textarea = document.createElement('textarea');
                textarea.value = text;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                showToast(successMessage, 'success');
            };

            if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
                return navigator.clipboard.writeText(text).then(() => {
                    showToast(successMessage, 'success');
                }).catch(() => {
                    fallbackCopy();
                });
            }

            fallbackCopy();
            return Promise.resolve();
        }

        // 复制邮箱地址
        function copyEmail(email) {
            copyTextToClipboard(email, '邮箱地址已复制');
        }

        // 复制当前邮箱
        function copyCurrentEmail() {
            const emailElement = document.getElementById('currentAccountEmail');
            if (emailElement && emailElement.textContent) {
                const email = emailElement.textContent.replace(' (临时)', '').trim();
                copyEmail(email);
            }
        }

        // 退出登录
        async function logout() {
            if (await showConfirmModal('确定要退出登录吗？', { title: '退出登录', confirmText: '确认退出', danger: false })) {
                window.location.href = '/logout';
            }
        }
