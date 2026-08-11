        /* global currentAccount, currentFolder, escapeHtml, isAggregatedInboxMode, isTempEmailGroup, loadEmails, showToast, switchFolder */

        let mailFolderTreeNodes = [];
        let mailFolderTreeExpanded = new Set();
        let mailFolderTreeLoading = false;
        let mailFolderTreeAccount = '';
        // 前端会话内按账号缓存目录树；存在则不再自动请求，仅「刷新文件夹」强制拉取
        const mailFolderTreeCacheByAccount = {};
        const MAIL_FOLDER_TREE_PANEL_COLLAPSED_KEY = 'outlook_mail_folder_tree_panel_collapsed';
        let mailFolderTreePanelCollapsed = loadMailFolderTreePanelCollapsed();

        function loadMailFolderTreePanelCollapsed() {
            try {
                const saved = localStorage.getItem(MAIL_FOLDER_TREE_PANEL_COLLAPSED_KEY);
                if (saved === null || saved === undefined || saved === '') {
                    return true; // 默认收起
                }
                return String(saved) !== 'false';
            } catch (error) {
                return true;
            }
        }

        function saveMailFolderTreePanelCollapsed(collapsed) {
            try {
                localStorage.setItem(MAIL_FOLDER_TREE_PANEL_COLLAPSED_KEY, collapsed ? 'true' : 'false');
            } catch (error) {
                // ignore storage failures
            }
        }

        function syncMailFolderTreePanelCollapsed() {
            const tree = document.getElementById('mailFolderTree');
            const btn = document.getElementById('mailFolderTreeCollapseBtn');
            if (tree) {
                tree.classList.toggle('is-collapsed', mailFolderTreePanelCollapsed);
            }
            if (btn) {
                btn.setAttribute('aria-expanded', mailFolderTreePanelCollapsed ? 'false' : 'true');
                btn.title = mailFolderTreePanelCollapsed ? '展开文件夹' : '收起文件夹';
                btn.textContent = mailFolderTreePanelCollapsed ? '▸ 文件夹' : '▾ 文件夹';
            }
        }

        function toggleMailFolderTreePanel(forceCollapsed) {
            if (typeof forceCollapsed === 'boolean') {
                mailFolderTreePanelCollapsed = forceCollapsed;
            } else {
                mailFolderTreePanelCollapsed = !mailFolderTreePanelCollapsed;
            }
            saveMailFolderTreePanelCollapsed(mailFolderTreePanelCollapsed);
            syncMailFolderTreePanelCollapsed();
        }

        function isCustomMailFolderKey(folder) {
            const value = String(folder || '').trim().toLowerCase();
            return value.startsWith('graph:') || value.startsWith('imap:');
        }

        function buildMailFolderStorageKey(node) {
            if (!node) return '';
            if (node.provider === 'graph' || node.folder_id) {
                return `graph:${node.folder_id || node.id}`;
            }
            return `imap:${node.mailbox || node.id}`;
        }

        function applyMailFolderParamsToSearch(params, folder = currentFolder) {
            const query = params instanceof URLSearchParams ? params : new URLSearchParams(params || {});
            const value = String(folder || 'inbox').trim();
            const lower = value.toLowerCase();
            if (lower.startsWith('graph:')) {
                query.delete('folder');
                query.delete('mailbox');
                query.set('folder_id', value.slice(6));
                return query;
            }
            if (lower.startsWith('imap:')) {
                query.delete('folder');
                query.delete('folder_id');
                query.set('mailbox', value.slice(5));
                return query;
            }
            query.delete('folder_id');
            query.delete('mailbox');
            query.set('folder', value || 'inbox');
            return query;
        }

        function buildMailFolderListParams(base = {}) {
            const folder = String(base.folder || currentFolder || 'inbox');
            const params = { ...base };
            delete params.folder;
            delete params.folder_id;
            delete params.mailbox;
            const query = new URLSearchParams(params);
            if (String(base.source || '').toLowerCase() === 'local') {
                query.set('folder', folder);
                return query;
            }
            applyMailFolderParamsToSearch(query, folder);
            return query;
        }

        function setMailFolderNavMode({ showTree = false, showTabs = false } = {}) {
            const tree = document.getElementById('mailFolderTree');
            const tabs = document.getElementById('folderTabs');
            if (tree) {
                tree.style.display = showTree ? 'flex' : 'none';
                tree.hidden = !showTree;
                if (showTree) {
                    syncMailFolderTreePanelCollapsed();
                }
            }
            if (tabs) {
                tabs.style.display = showTabs ? 'flex' : 'none';
            }
        }

        function syncMailFolderTreeSelection() {
            const allBtn = document.getElementById('mailFolderTreeAllBtn');
            if (allBtn) {
                allBtn.classList.toggle('active', currentFolder === 'all');
            }
            document.querySelectorAll('.mail-folder-node').forEach((nodeEl) => {
                const key = nodeEl.dataset.folderKey || '';
                nodeEl.classList.toggle('active', Boolean(key) && key === currentFolder);
            });
        }

        function selectMailFolderWellKnown(folder) {
            switchFolder(folder || 'all');
            syncMailFolderTreeSelection();
        }

        function selectMailFolderNode(folderKey) {
            if (!folderKey) return;
            switchFolder(folderKey);
            syncMailFolderTreeSelection();
        }

        function toggleMailFolderExpanded(nodeId) {
            const id = String(nodeId || '');
            if (!id) return;
            if (mailFolderTreeExpanded.has(id)) {
                mailFolderTreeExpanded.delete(id);
            } else {
                mailFolderTreeExpanded.add(id);
            }
            if (mailFolderTreeAccount && mailFolderTreeCacheByAccount[mailFolderTreeAccount]) {
                mailFolderTreeCacheByAccount[mailFolderTreeAccount].expanded = Array.from(mailFolderTreeExpanded);
            }
            renderMailFolderTree();
        }

        function renderMailFolderTree() {
            const listEl = document.getElementById('mailFolderTreeList');
            if (!listEl) return;

            if (mailFolderTreeLoading) {
                listEl.innerHTML = '<div class="mail-folder-tree-empty">正在加载文件夹...</div>';
                syncMailFolderTreeSelection();
                return;
            }

            const nodes = Array.isArray(mailFolderTreeNodes) ? mailFolderTreeNodes : [];
            if (!nodes.length) {
                listEl.innerHTML = '<div class="mail-folder-tree-empty">暂无文件夹</div>';
                syncMailFolderTreeSelection();
                return;
            }

            const nodeById = new Map(nodes.map((node) => [String(node.id), node]));
            const childrenMap = new Map();
            nodes.forEach((node) => {
                let parentId = node.parent_id == null || node.parent_id === '' ? '' : String(node.parent_id);
                if (parentId && !nodeById.has(parentId)) {
                    parentId = '';
                }
                if (!childrenMap.has(parentId)) childrenMap.set(parentId, []);
                childrenMap.get(parentId).push(node);
            });

            const html = [];
            const walk = (parentId, depth) => {
                (childrenMap.get(parentId) || []).forEach((node) => {
                    const id = String(node.id || '');
                    const childList = childrenMap.get(id) || [];
                    const hasChildren = childList.length > 0;
                    const expanded = mailFolderTreeExpanded.has(id);
                    const folderKey = buildMailFolderStorageKey(node);
                    const selectable = node.selectable !== false;
                    const pad = 8 + depth * 14;
                    const safeId = encodeURIComponent(id);
                    const safeKey = encodeURIComponent(folderKey);
                    html.push(`
                        <div class="mail-folder-node" data-folder-key="${escapeHtml(folderKey)}" style="padding-left:${pad}px;">
                            <button type="button" class="mail-folder-toggle" ${hasChildren ? '' : 'disabled'}
                                data-node-id="${safeId}" onclick="toggleMailFolderExpanded(decodeURIComponent(this.dataset.nodeId))"
                                aria-label="${expanded ? '折叠' : '展开'}">
                                ${hasChildren ? (expanded ? '▾' : '▸') : '·'}
                            </button>
                            <button type="button" class="mail-folder-label" ${selectable ? '' : 'disabled'}
                                data-folder-key="${safeKey}"
                                onclick="selectMailFolderNode(decodeURIComponent(this.dataset.folderKey))">
                                ${escapeHtml(node.display_name || node.name || id)}
                            </button>
                        </div>
                    `);
                    if (hasChildren && expanded) {
                        walk(id, depth + 1);
                    }
                });
            };
            walk('', 0);

            listEl.innerHTML = html.join('') || '<div class="mail-folder-tree-empty">暂无文件夹</div>';
            syncMailFolderTreeSelection();
        }

        function applyCachedMailFolderTree(accountEmail, cached) {
            mailFolderTreeAccount = accountEmail;
            mailFolderTreeLoading = false;
            mailFolderTreeNodes = Array.isArray(cached?.folders) ? cached.folders : [];
            mailFolderTreeExpanded = new Set(
                Array.isArray(cached?.expanded) && cached.expanded.length
                    ? cached.expanded.map((id) => String(id))
                    : mailFolderTreeNodes
                        .filter((node) => !node.parent_id)
                        .slice(0, 12)
                        .map((node) => String(node.id))
            );
            setMailFolderNavMode({ showTree: true, showTabs: false });
            renderMailFolderTree();
        }

        async function loadMailFolderTree(email = currentAccount, options = {}) {
            const accountEmail = String(email || '').trim();
            if (!accountEmail || isAggregatedInboxMode() || isTempEmailGroup) {
                setMailFolderNavMode({ showTree: false, showTabs: true });
                return;
            }

            const forceRefresh = options.refresh === true;
            const cached = mailFolderTreeCacheByAccount[accountEmail];
            if (!forceRefresh && cached && Array.isArray(cached.folders)) {
                applyCachedMailFolderTree(accountEmail, cached);
                return;
            }

            setMailFolderNavMode({ showTree: true, showTabs: false });
            mailFolderTreeLoading = true;
            mailFolderTreeAccount = accountEmail;
            renderMailFolderTree();

            try {
                const query = new URLSearchParams();
                if (forceRefresh) query.set('refresh', '1');
                const response = await fetch(`/api/emails/${encodeURIComponent(accountEmail)}/folders?${query.toString()}`);
                const data = await response.json();
                if (mailFolderTreeAccount !== accountEmail) {
                    return;
                }
                if (!data.success) {
                    mailFolderTreeNodes = [];
                    const listEl = document.getElementById('mailFolderTreeList');
                    if (listEl) {
                        listEl.innerHTML = `<div class="mail-folder-tree-error">${escapeHtml(data.error || '加载文件夹失败')}</div>`;
                    }
                    return;
                }
                mailFolderTreeNodes = Array.isArray(data.folders) ? data.folders : [];
                mailFolderTreeExpanded = new Set(
                    mailFolderTreeNodes
                        .filter((node) => !node.parent_id)
                        .slice(0, 12)
                        .map((node) => String(node.id))
                );
                mailFolderTreeCacheByAccount[accountEmail] = {
                    folders: mailFolderTreeNodes,
                    expanded: Array.from(mailFolderTreeExpanded),
                };
            } catch (error) {
                if (mailFolderTreeAccount === accountEmail) {
                    mailFolderTreeNodes = [];
                    const listEl = document.getElementById('mailFolderTreeList');
                    if (listEl) {
                        listEl.innerHTML = `<div class="mail-folder-tree-error">${escapeHtml(error.message || '加载文件夹失败')}</div>`;
                    }
                }
            } finally {
                if (mailFolderTreeAccount === accountEmail) {
                    mailFolderTreeLoading = false;
                    renderMailFolderTree();
                }
            }
        }

        async function refreshMailFolderTree() {
            if (!currentAccount || isAggregatedInboxMode() || isTempEmailGroup) {
                showToast('请先选择普通邮箱账号', 'error');
                return;
            }
            await loadMailFolderTree(currentAccount, { refresh: true });
            showToast('文件夹已刷新', 'success');
        }

        function hideMailFolderTree() {
            // 仅隐藏 UI，保留会话内目录树缓存供再次点入账号时复用
            mailFolderTreeAccount = '';
            mailFolderTreeLoading = false;
            setMailFolderNavMode({ showTree: false, showTabs: false });
        }
