/* Клиентский код страницы проводника АЛЬБИОН.
   Конфиг формирует шаблон HTML_TEMPLATE в window.ALBION_CONFIG.
   Классический (не модульный) скрипт: функции остаются глобальными,
   потому что их вызывают inline-обработчики в разметке строк и модалок. */
const CFG = window.ALBION_CONFIG || {};
const currentFolderId = CFG.currentFolderId || '';
const parentFolderId = CFG.parentFolderId || '';
const currentView = CFG.currentView || 'table';
const csrfToken = CFG.csrfToken || '';
const baseUrl = CFG.baseUrl || '';
        const nativeFetch = window.fetch.bind(window);
        let authRedirectStarted = false;
        window.fetch = (input, init = {}) => {
            const method = (init.method || 'GET').toUpperCase();
            if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
                const headers = new Headers(init.headers || {});
                headers.set('X-CSRF-Token', csrfToken);
                init = { ...init, headers };
            }
            return nativeFetch(input, init).then((response) => {
                // Сессия истекла (401 от /api/): сообщаем и уводим на вход.
                if (response.status === 401 && !authRedirectStarted) {
                    authRedirectStarted = true;
                    showToast('Сессия истекла, войдите заново', true);
                    const next = encodeURIComponent(window.location.pathname + window.location.search);
                    setTimeout(() => window.location.assign(`/login?next=${next}`), 800);
                }
                return response;
            });
        };
        let draggedFileId = null;
        let selectedFiles = new Set();
        let selectedFolders = new Set();
        const busyActions = new Set();
        function lockAction(key) {
            if (busyActions.has(key)) return false;
            busyActions.add(key);
            return true;
        }
        function unlockAction(key) { busyActions.delete(key); }

        let activeModal = null;
        let modalReturnFocus = null;
        function modalFocusable(modal) {
            return Array.from(modal.querySelectorAll(
                'button:not([disabled]), [href], input:not([type="hidden"]):not([disabled]), ' +
                'select:not([disabled]), textarea:not([disabled])'
            )).filter(el => el.offsetParent !== null);
        }
        function openModal(id) {
            const modal = document.getElementById(id);
            if (!modal) return;
            if (activeModal && activeModal !== modal) {
                activeModal.classList.remove('active');
                activeModal.setAttribute('aria-hidden', 'true');
            }
            modalReturnFocus = document.activeElement;
            activeModal = modal;
            modal.classList.add('active');
            modal.setAttribute('aria-hidden', 'false');
            setBackgroundInert(true);
            const target = modalFocusable(modal)[0];
            setTimeout(() => target?.focus(), 0);
        }
        function closeModal(id) {
            const modal = document.getElementById(id);
            if (!modal) return;
            modal.classList.remove('active');
            modal.setAttribute('aria-hidden', 'true');
            if (activeModal === modal) {
                activeModal = null;
                setBackgroundInert(false);
                if (modalReturnFocus && document.contains(modalReturnFocus)) {
                    modalReturnFocus.focus();
                }
                modalReturnFocus = null;
            }
        }
        // Пока открыта модалка, фон (шапка, контент, футер) делаем инертным,
        // чтобы скринридер и Tab не уходили за пределы диалога. Плашки toast
        // остаются доступными. `inert` поддерживается, но подстрахуемся aria-hidden.
        function setBackgroundInert(on) {
            document.querySelectorAll('body > header, .main, body > footer').forEach(el => {
                if (on) {
                    el.setAttribute('inert', '');
                    el.setAttribute('aria-hidden', 'true');
                } else {
                    el.removeAttribute('inert');
                    el.removeAttribute('aria-hidden');
                }
            });
        }
        document.addEventListener('keydown', (event) => {
            if (!activeModal) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                closeModal(activeModal.id);
                return;
            }
            if (event.key === 'Enter') {
                const target = event.target;
                if (target && (target.tagName === 'INPUT' || target.tagName === 'SELECT')) {
                    const primary = activeModal.querySelector('[data-modal-primary]');
                    if (primary && !primary.disabled) {
                        event.preventDefault();
                        primary.click();
                    }
                }
            }
            if (event.key !== 'Tab') return;
            const items = modalFocusable(activeModal);
            if (!items.length) return;
            const first = items[0];
            const last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            } else if (!activeModal.contains(document.activeElement)) {
                event.preventDefault();
                first.focus();
            }
        });
        
        const viewBtns = document.querySelectorAll('.view-btn[data-view]');
        function setView(view) {
            if (!['table', 'grid', 'list'].includes(view)) return;
            if (view === currentView) return;
            if (!queueGuard('смена вида')) return;
            const url = new URL(window.location.href);
            if (url.searchParams.get('view') === view) return;
            url.searchParams.set('view', view);
            window.location.assign(url.toString());
        }
        viewBtns.forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));

        // Селекты фильтра применяются сразу: выбирать значение и затем жать
        // «Применить» — лишний шаг. Поиск остаётся по Enter/кнопке: автосабмит
        // на каждый пауза в наборе перерисовывал бы список.
        const filterForm = document.querySelector('form.file-filters');
        if (filterForm) {
            filterForm.querySelectorAll('select').forEach(select => {
                select.addEventListener('change', () => filterForm.submit());
            });
        }

        function handleDragStart(event, fileId) {
            draggedFileId = fileId;
            event.target.classList.add('dragging');
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', fileId);
        }

        function handleDragEnd(event) {
            event.target.classList.remove('dragging');
            document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
            draggedFileId = null;
        }

        function handleDragOver(event) {
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
            event.currentTarget.classList.add('drag-over');
        }

        function handleDragLeave(event) {
            event.currentTarget.classList.remove('drag-over');
        }

        async function handleDropOnFolder(event, targetFolderId) {
            event.preventDefault();
            event.currentTarget.classList.remove('drag-over');
            const fileId = event.dataTransfer.getData('text/plain');
            if (!fileId) return;
            if ((targetFolderId || '') === (currentFolderId || '')) {
                showToast('Файл уже в этой папке');
                return;
            }
            const actionKey = `drop:${fileId}`;
            if (!lockAction(actionKey)) return;
            try {
                const response = await fetch('/api/move_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_id: fileId, target_folder_id: targetFolderId })
                });
                const result = await parseResponseJson(response);
                if (response.ok) {
                    showToast('✅ Файл перемещён');
                    if (result.moved) removeRows([fileId]);
                }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally { unlockAction(actionKey); }
        }

        // Количество строк в DOM кэшируется: после ленивой подгрузки их могут быть
        // тысячи, и пересчёт на каждый клик чекбокса становится заметным.
        let domTotals = { files: 0, folders: 0 };
        let selectionIncludesAllFound = false;

        function refreshDomTotals() {
            domTotals = {
                files: document.querySelectorAll('.file-checkbox').length,
                folders: document.querySelectorAll('.folder-checkbox').length,
            };
        }

        // Счётчики «Файлов/Папок», «Найдено» и футер обновляются без перезагрузки,
        // когда строки удаляются или уходят из папки (удаление/перемещение).
        function adjustCounters(fileDelta, folderDelta) {
            const bump = (id, delta) => {
                if (!delta) return;
                const el = document.getElementById(id);
                if (!el) return;
                const current = parseInt(el.textContent, 10);
                el.textContent = String(Math.max(0, (isNaN(current) ? 0 : current) + delta));
            };
            bump('titleFileCount', fileDelta);
            bump('titleFolderCount', folderDelta);
            bump('foundCount', fileDelta);
            bump('footerFileCount', fileDelta);
            bump('footerFolderCount', folderDelta);
            const shown = document.getElementById('shownCount');
            if (shown) shown.textContent = String(document.querySelectorAll(lazyFileSelector()).length);
            const foundBtn = document.getElementById('selectAllFoundBtn');
            const foundCount = document.getElementById('foundCount');
            if (foundBtn && foundCount) {
                foundBtn.textContent = `🔎 Выбрать все найденные (${foundCount.textContent})`;
                foundBtn.dataset.idleLabel = foundBtn.textContent;
            }
        }

        // Точечное удаление строк после удаления/перемещения — вместо полной
        // перезагрузки страницы. Убираем только те id, что подтвердил сервер;
        // счётчики и выделение приводим в согласованное состояние.
        function removeRows(fileIds, folderIds) {
            const files = Array.from(fileIds || []);
            const folders = Array.from(folderIds || []);
            if (!files.length && !folders.length) return;
            const esc = (value) => (window.CSS && CSS.escape) ? CSS.escape(String(value)) : String(value);
            let folderRowsRemoved = 0;
            files.forEach(id => {
                selectedFiles.delete(id);
                document.querySelectorAll(
                    `.file-row[data-file-id="${esc(id)}"], .tile[data-file-id="${esc(id)}"], .list-item[data-file-id="${esc(id)}"]`
                ).forEach(el => el.remove());
            });
            folders.forEach(id => {
                selectedFolders.delete(id);
                const rows = document.querySelectorAll(
                    `.folder-row[data-folder-id="${esc(id)}"], .tile[data-folder-id="${esc(id)}"], .list-item.folder-item[data-folder-id="${esc(id)}"]`
                );
                folderRowsRemoved += rows.length;
                rows.forEach(el => el.remove());
            });
            adjustCounters(-files.length, -folderRowsRemoved);
            refreshDomTotals();
            updateSelection();
        }

        // Выделение переживает перезагрузку страницы: применение фильтра и смена
        // вида перезагружают её, и раньше выделение пропадало молча.
        const selectionStorageKey = `albionSelection:${currentFolderId || 'root'}`;
        function saveSelection() {
            try {
                sessionStorage.setItem(selectionStorageKey, JSON.stringify({
                    files: Array.from(selectedFiles),
                    folders: Array.from(selectedFolders),
                }));
            } catch (err) { /* приватный режим — просто не сохраняем */ }
        }
        function restoreSelection() {
            try {
                const raw = sessionStorage.getItem(selectionStorageKey);
                if (!raw) return;
                const data = JSON.parse(raw) || {};
                (data.files || []).forEach(id => selectedFiles.add(id));
                (data.folders || []).forEach(id => selectedFolders.add(id));
            } catch (err) { /* повреждённые данные игнорируем */ }
        }

        // Подпись кнопки запоминается в data-idle-label, чтобы не восстанавливать
        // её жёсткой строкой после «Сохранение…».
        function setIdleLabel(btn, busyText) {
            if (!btn.dataset.idleLabel) btn.dataset.idleLabel = btn.textContent;
            btn.textContent = busyText;
            btn.classList.add('is-busy');
            btn.setAttribute('aria-busy', 'true');
        }
        function restoreIdleLabel(btn) {
            if (btn.dataset.idleLabel) btn.textContent = btn.dataset.idleLabel;
            btn.classList.remove('is-busy');
            btn.removeAttribute('aria-busy');
        }

        // Очередь загрузки нельзя сохранить между перезагрузками (File-объекты
        // существуют только в памяти), поэтому предупреждаем вместо тихой потери.
        function queueGuard(actionLabel) {
            if (selectedUploadFiles.length === 0) return true;
            return confirm(
                `В очереди загрузки ${selectedUploadFiles.length} файл(ов). ` +
                `Они будут потеряны, если продолжить «${actionLabel}» без обновления страницы.\n\n` +
                'Продолжить? Сначала можно загрузить очередь.'
            );
        }

        function syncSelectionCheckboxes(changed) {
            if (changed) return;   // сам чекбокс уже в нужном состоянии
            document.querySelectorAll('.file-checkbox').forEach(cb => {
                cb.checked = selectedFiles.has(cb.dataset.fileId);
            });
            document.querySelectorAll('.folder-checkbox').forEach(cb => {
                cb.checked = selectedFolders.has(cb.dataset.folderId);
            });
        }

        function markRowSelection(el) {
            const fileId = el.dataset.fileId;
            const folderId = el.dataset.folderId;
            const on = (fileId && selectedFiles.has(fileId)) || (folderId && selectedFolders.has(folderId));
            el.classList.toggle('selected', !!on);
        }

        function updateSelection(changed) {
            if (changed) {
                const isFile = changed.classList.contains('file-checkbox');
                const id = isFile ? changed.dataset.fileId : changed.dataset.folderId;
                const selection = isFile ? selectedFiles : selectedFolders;
                if (changed.checked) selection.add(id); else selection.delete(id);
                selectionIncludesAllFound = false;
                const row = changed.closest('.file-row, .folder-row, .tile, .list-item');
                if (row) markRowSelection(row);   // перерисовываем только затронутую строку
            } else {
                document.querySelectorAll('.file-row, .folder-row, .tile, .list-item').forEach(markRowSelection);
            }

            syncSelectionCheckboxes(changed);

            const total = selectedFiles.size + selectedFolders.size;
            const batchActions = document.getElementById('batchActions');
            const selectedCount = document.getElementById('selectedCount');
            if (total > 0) {
                batchActions.classList.add('active');
                selectedCount.textContent = total;
            } else {
                batchActions.classList.remove('active');
                selectedCount.textContent = '0';
            }
            document.querySelectorAll('.file-selection-action').forEach(btn => {
                btn.hidden = total === 0;
            });

            const selectAll = document.getElementById('selectAll');
            const loadedAll = domTotals.files + domTotals.folders > 0
                && selectedFiles.size >= domTotals.files
                && selectedFolders.size >= domTotals.folders;
            if (selectAll) {
                selectAll.checked = loadedAll;
                selectAll.indeterminate = total > 0 && !loadedAll;
            }
            const selectAllBtn = document.getElementById('selectAllBtn');
            if (selectAllBtn) {
                selectAllBtn.textContent = loadedAll ? '☑️ Всё выбрано' : '☑️ Выбрать все';
                selectAllBtn.setAttribute('aria-pressed', loadedAll ? 'true' : 'false');
            }
            const foundBtn = document.getElementById('selectAllFoundBtn');
            if (foundBtn) foundBtn.disabled = selectionIncludesAllFound;
            saveSelection();
        }

        async function selectAllFound() {
            const btn = document.getElementById('selectAllFoundBtn');
            if (!btn || !lockAction('select-found')) return;
            setIdleLabel(btn, '⏳ Выделение…');
            btn.disabled = true;
            try {
                const form = document.querySelector('form.file-filters');
                const response = await fetch('/api/select_by_filter', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        folder_id: currentFolderId || null,
                        q: form ? form.querySelector('input[name="q"]').value.trim() : '',
                        ext: form ? form.querySelector('select[name="ext"]').value : '',
                    })
                });
                const result = await parseResponseJson(response);
                if (!response.ok) { showToast('❌ ' + (result.error || 'Не удалось выделить'), true); return; }
                (result.file_ids || []).forEach(id => selectedFiles.add(id));
                (result.folder_ids || []).forEach(id => selectedFolders.add(id));
                selectionIncludesAllFound = true;
                updateSelection();
                const selected = selectedFiles.size + selectedFolders.size;
                if (result.truncated) {
                    showToast(`⚠️ Выделено ${selected}: больше ${result.limit} элементов за раз не выделяем`, true);
                } else {
                    showToast(`✅ Выделено найденных: ${selected}`);
                }
            } catch (err) {
                showToast('❌ Нет связи с сервером', true);
            } finally {
                restoreIdleLabel(btn);
                btn.disabled = selectionIncludesAllFound;
                unlockAction('select-found');
            }
        }

        function setAllSelection(selected) {
            selectedFiles.clear();
            selectedFolders.clear();
            if (selected) {
                document.querySelectorAll('.file-checkbox').forEach(cb => selectedFiles.add(cb.dataset.fileId));
                document.querySelectorAll('.folder-checkbox').forEach(cb => selectedFolders.add(cb.dataset.folderId));
            }
            selectionIncludesAllFound = false;
            updateSelection();
        }

        function toggleSelectAll() {
            const selectAll = document.getElementById('selectAll');
            setAllSelection(!!selectAll.checked);
        }

        // «Выбрать все» выделяет только уже подгруженные строки; для полного
        // набора по фильтру есть отдельная кнопка «Выбрать все найденные».
        function selectAllItems() {
            const selectedTotal = selectedFiles.size + selectedFolders.size;
            const loadedTotal = domTotals.files + domTotals.folders;
            const loadedAll = loadedTotal > 0
                && selectedFiles.size >= domTotals.files
                && selectedFolders.size >= domTotals.folders;
            if (selectedTotal > 0 && !loadedAll) {
                showToast('Выделены загруженные строки. Все найденные — кнопка «🔎 Выбрать все найденные»');
            }
            setAllSelection(!loadedAll);
        }

        function clearSelection() {
            setAllSelection(false);
        }

        function setBatchBusy(busy) {
            document.querySelectorAll('#batchActions button').forEach(btn => { btn.disabled = busy; });
            const bar = document.getElementById('batchActions');
            if (bar) {
                if (busy) bar.setAttribute('aria-busy', 'true');
                else bar.removeAttribute('aria-busy');
            }
        }

        function showBatchResult(url, count, size) {
            const box = document.getElementById('batchResult');
            if (!box) return;
            box.innerHTML = '';
            const link = document.createElement('a');
            link.href = url;
            link.download = '';
            link.textContent = `⬇ Скачать архив (${count} файл(ов), ${size})`;
            box.appendChild(link);
            box.hidden = false;
        }

        // Inline-статус в панели пакетных операций: сервер собирает архив одним
        // ответом, поэтому показываем индетерминированное состояние, без процентов.
        function showBatchStatus(text, isError = false) {
            const box = document.getElementById('batchResult');
            if (!box) return;
            box.textContent = text;
            box.classList.toggle('is-error', isError);
            box.hidden = !text;
        }

        async function batchDelete() {
            const fileCount = selectedFiles.size;
            const folderCount = selectedFolders.size;
            if (fileCount === 0 && folderCount === 0) return;
            if (!lockAction('batch-delete')) return;
            let question = `Удалить ${fileCount} файл(ов)`;
            if (folderCount) question += ` и ${folderCount} папок(и)`;
            question += folderCount ? '? Подпапки внутри будут удалены безвозвратно.' : '?';
            if (!confirm(question)) { unlockAction('batch-delete'); return; }
            const btn = document.getElementById('batchDeleteBtn');
            if (btn) { setIdleLabel(btn, '⏳ Удаление…'); btn.disabled = true; }
            try {
                const deletedFiles = [];
                const deletedFolders = [];
                const trashIds = [];
                if (fileCount) {
                    const response = await fetch('/api/delete_files_batch', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ file_ids: Array.from(selectedFiles) })
                    });
                    const result = await parseResponseJson(response);
                    if (!response.ok) { showToast('❌ ' + (result.error || 'Ошибка'), true); return; }
                    deletedFiles.push(...(result.deleted_ids || []));
                    trashIds.push(...(result.trash_ids || []));
                }
                if (folderCount) {
                    const response = await fetch('/api/delete_folders_batch', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder_ids: Array.from(selectedFolders) })
                    });
                    const result = await parseResponseJson(response);
                    if (!response.ok) { showToast('❌ ' + (result.error || 'Ошибка'), true); return; }
                    deletedFolders.push(...(result.deleted_folder_ids || []));
                }
                const total = deletedFiles.length + deletedFolders.length;
                if (deletedFiles.length || deletedFolders.length) removeRows(deletedFiles, deletedFolders);
                else reloadPreservingScroll(800);
                showToast(`✅ Удалено: ${total}`, false,
                    trashIds.length ? { label: 'Восстановить', onClick: () => undoDelete(trashIds) } : null);
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally {
                if (btn) { restoreIdleLabel(btn); btn.disabled = false; }
                unlockAction('batch-delete');
            }
        }

        async function executeBatchZip() {
            if (selectedFiles.size === 0 && selectedFolders.size === 0) return;
            if (!lockAction('batch-zip')) return;
            const btn = document.getElementById('batchZipBtn');
            setBatchBusy(true);
            showBatchStatus('⏳ Собираю архив…');
            if (btn) setIdleLabel(btn, '⏳ Создание архива…');
            try {
                const response = await fetch('/api/export_zip', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_ids: Array.from(selectedFiles), folder_ids: Array.from(selectedFolders) })
                });
                const result = await parseResponseJson(response);
                if (response.ok) {
                    copyToClipboard(result.url);
                    showBatchResult(result.url, result.count, result.size);
                    showToast(`✅ Архив готов: ${result.count} файл(ов), ${result.size}. Ссылка скопирована`);
                    setAllSelection(false);
                } else {
                    const message = result.error || 'Не удалось собрать архив';
                    showBatchStatus('❌ ' + message, true);
                    showToast('❌ ' + message, true);
                }
            } catch (err) {
                showBatchStatus('❌ Нет связи с сервером', true);
                showToast('❌ Нет связи с сервером', true);
            }
            finally {
                if (btn) restoreIdleLabel(btn);
                setBatchBusy(false);
                unlockAction('batch-zip');
            }
        }

        async function openBatchMoveModal() {
            if (selectedFiles.size === 0 && selectedFolders.size === 0) return;
            const loaded = await loadFolderSelect('batchMoveFolderId', '', Array.from(selectedFolders));
            if (loaded) openModal('batchMoveModal');
        }

        function closeBatchMoveModal() { closeModal('batchMoveModal'); }

        async function executeBatchMove() {
            if (!lockAction('batch-move')) return;
            const btn = document.getElementById('batchMoveBtn');
            const targetFolderId = document.getElementById('batchMoveFolderId').value || null;
            if (btn) { setIdleLabel(btn, 'Перемещение…'); btn.disabled = true; }
            try {
                const fileIds = Array.from(selectedFiles);
                const folderIds = Array.from(selectedFolders);
                const movedFiles = [];
                const movedFolders = [];
                if (fileIds.length) {
                    const response = await fetch('/api/move_files_batch', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ file_ids: fileIds, target_folder_id: targetFolderId })
                    });
                    const result = await parseResponseJson(response);
                    if (!response.ok) { showToast('❌ ' + (result.error || 'Ошибка'), true); return; }
                    movedFiles.push(...(result.moved_ids || []));
                }
                if (folderIds.length) {
                    const response = await fetch('/api/move_folders_batch', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder_ids: folderIds, target_folder_id: targetFolderId })
                    });
                    const result = await parseResponseJson(response);
                    if (!response.ok) { showToast('❌ ' + (result.error || 'Ошибка'), true); return; }
                    movedFolders.push(...(result.moved_ids || []));
                }
                showToast(`✅ Перемещено: ${movedFiles.length + movedFolders.length}`);
                closeBatchMoveModal();
                if (movedFiles.length || movedFolders.length) removeRows(movedFiles, movedFolders);
                else reloadPreservingScroll(800);
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally {
                if (btn) { restoreIdleLabel(btn); btn.disabled = false; }
                unlockAction('batch-move');
            }
        }

        async function moveFileUp(fileId) {
            const actionKey = `move:${fileId}`;
            if (!lockAction(actionKey)) return;
            try {
                const response = await fetch('/api/move_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_id: fileId, target_folder_id: parentFolderId || null })
                });
                const result = await parseResponseJson(response);
                if (response.ok) {
                    showToast('✅ Перемещён выше');
                    if (result.moved) removeRows([fileId]);
                }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally { unlockAction(actionKey); }
        }

        let editOriginalPhysicalFilename = '';
        let folderSelectRequestId = 0;

        async function openEditModal(internalId, displayName, physicalFilename, customAlias, folderId) {
            document.getElementById('editInternalId').value = internalId;
            document.getElementById('editDisplayName').value = displayName;
            document.getElementById('editPhysicalFilename').value = physicalFilename;
            document.getElementById('editCustomAlias').value = customAlias;
            editOriginalPhysicalFilename = physicalFilename;
            updateFilenameWarning();
            updateAliasPreview();
            const loaded = await loadFolderSelect('editFolderId', folderId);
            if (!loaded) return;
            openModal('editModal');
        }
        function closeEditModal() { closeModal('editModal'); }

        function updateFilenameWarning() {
            const warning = document.getElementById('filenameWarning');
            const current = document.getElementById('editPhysicalFilename').value.trim();
            warning.style.display = current !== editOriginalPhysicalFilename ? 'block' : 'none';
        }
        document.getElementById('editPhysicalFilename').addEventListener('input', updateFilenameWarning);

        // Папки, которые нельзя выбрать целью перемещения: сами перемещаемые
        // и все их потомки (иначе получилось бы перемещение папки внутрь себя).
        function excludedFolderIds(folders, folderIds) {
            const byParent = new Map();
            folders.forEach(f => {
                const key = f.parent_id || '';
                if (!byParent.has(key)) byParent.set(key, []);
                byParent.get(key).push(f.id);
            });
            const excluded = new Set();
            const queue = Array.from(folderIds || []);
            while (queue.length) {
                const id = queue.shift();
                if (excluded.has(id)) continue;
                excluded.add(id);
                (byParent.get(id) || []).forEach(child => queue.push(child));
            }
            return excluded;
        }

        async function loadFolderSelect(selectId, selectedValue = '', excludeIds = []) {
            const requestId = ++folderSelectRequestId;
            try {
                const response = await fetch('/api/get_all_folders');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const folders = await response.json();
                // A slower response from an older modal must not overwrite a newer one.
                if (requestId !== folderSelectRequestId) return false;
                const select = document.getElementById(selectId);
                select.innerHTML = '<option value="">🏠 Корень</option>';
                const excluded = excludedFolderIds(folders, excludeIds);
                folders.forEach(f => {
                    if (excluded.has(f.id)) return;
                    const option = document.createElement('option');
                    option.value = f.id;
                    option.textContent = `${'—'.repeat(f.depth)} 📁 ${f.name}`;
                    select.appendChild(option);
                });
                select.value = selectedValue || '';
                return true;
            } catch (err) {
                if (requestId === folderSelectRequestId) {
                    showToast('❌ Не удалось загрузить список папок', true);
                    console.error('Ошибка списка папок:', err);
                }
                return false;
            }
        }
        
        function updateAliasPreview() {
            const alias = document.getElementById('editCustomAlias').value.trim();
            const url = alias ? `${baseUrl}/${alias}` : '';
            document.getElementById('aliasPreview').textContent = url || '(прямая ссылка на файл)';
            document.getElementById('aliasPreview').href = url || '#';
        }
        
        async function saveFileEdit() {
            const data = {
                internal_id: document.getElementById('editInternalId').value,
                display_name: document.getElementById('editDisplayName').value.trim(),
                physical_filename: document.getElementById('editPhysicalFilename').value.trim(),
                custom_alias: document.getElementById('editCustomAlias').value.trim(),
                folder_id: document.getElementById('editFolderId').value || null,
                view: currentView
            };
            if (!data.display_name || !data.physical_filename) { showToast('❌ Имена не могут быть пустыми', true); return; }
            const btn = document.getElementById('editSaveBtn');
            if (btn) { setIdleLabel(btn, 'Сохранение…'); btn.disabled = true; }
            try {
                const response = await fetch('/api/update_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                const result = await parseResponseJson(response);
                if (response.ok) {
                    showToast('✅ Сохранено');
                    closeEditModal();
                    // Файл остался в этой папке — заменяем строку на месте; уехал — убираем.
                    const escaped = (window.CSS && CSS.escape) ? CSS.escape(data.internal_id) : data.internal_id;
                    const row = document.querySelector(`[data-file-id="${escaped}"]`);
                    const stayed = (result.folder_id || '') === (currentFolderId || '');
                    const template = document.createElement('template');
                    if (stayed && result.row_html && row) {
                        template.innerHTML = result.row_html.trim();
                        const node = template.content.firstElementChild;
                        if (node) row.replaceWith(node);
                        refreshDomTotals();
                        updateSelection();
                    } else if (row) {
                        removeRows([data.internal_id]);
                    } else {
                        reloadPreservingScroll(800);
                    }
                }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally {
                if (btn) { restoreIdleLabel(btn); btn.disabled = false; }
            }
        }

        function openCreateFolderModal() {
            const input = document.getElementById('newFolderName');
            input.value = '';
            openModal('createFolderModal');
            setTimeout(() => input.focus(), 0);
        }
        function closeCreateFolderModal() { closeModal('createFolderModal'); }
        async function createFolder() {
            const name = document.getElementById('newFolderName').value.trim();
            if (!name) { showToast('❌ Введите имя папки', true); return; }
            const btn = document.getElementById('createFolderBtn');
            if (btn) { setIdleLabel(btn, 'Создание…'); btn.disabled = true; }
            try {
                const response = await fetch('/api/create_folder', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, parent_id: currentFolderId || null, view: currentView })
                });
                const result = await parseResponseJson(response);
                if (response.ok) {
                    showToast('✅ Папка создана');
                    closeCreateFolderModal();
                    const added = insertRowsHtml(result.row_html, 'folder');
                    if (added) {
                        adjustCounters(0, added);
                        refreshDomTotals();
                        updateSelection();
                    } else {
                        reloadPreservingScroll(800);
                    }
                }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally {
                if (btn) { restoreIdleLabel(btn); btn.disabled = false; }
            }
        }

        function confirmDelete(internalId, fileName) {
            document.getElementById('deleteFileId').value = internalId;
            document.getElementById('deleteFileName').textContent = fileName;
            openModal('deleteModal');
        }
        function closeDeleteModal() { closeModal('deleteModal'); }
        async function executeDelete() {
            const internalId = document.getElementById('deleteFileId').value;
            const btn = document.getElementById('deleteFileBtn');
            if (btn) { setIdleLabel(btn, 'Удаление…'); btn.disabled = true; }
            try {
                const response = await fetch('/api/delete_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ internal_id: internalId })
                });
                const data = await parseResponseJson(response);
                if (response.ok) {
                    closeDeleteModal();
                    removeRows([internalId]);
                    const trashIds = data.trash_ids || [];
                    showToast('✅ Удалено', false,
                        trashIds.length ? { label: 'Восстановить', onClick: () => undoDelete(trashIds) } : null);
                }
                else { showToast('❌ ' + (data.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally {
                if (btn) { restoreIdleLabel(btn); btn.disabled = false; }
            }
        }

        function confirmDeleteFolder(folderId, folderName) {
            document.getElementById('deleteFolderId').value = folderId;
            document.getElementById('deleteFolderName').textContent = folderName;
            openModal('deleteFolderModal');
        }
        function closeDeleteFolderModal() { closeModal('deleteFolderModal'); }
        async function executeDeleteFolder() {
            const folderId = document.getElementById('deleteFolderId').value;
            const btn = document.getElementById('deleteFolderBtn');
            if (btn) { setIdleLabel(btn, 'Удаление…'); btn.disabled = true; }
            try {
                const response = await fetch('/api/delete_folder', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder_id: folderId })
                });
                if (response.ok) {
                    showToast('✅ Папка удалена');
                    closeDeleteFolderModal();
                    removeRows([], [folderId]);
                }
                else { const data = await parseResponseJson(response); showToast('❌ ' + (data.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
            finally {
                if (btn) { restoreIdleLabel(btn); btn.disabled = false; }
            }
        }

        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const folderInput = document.getElementById('folderInput');
        const fileList = document.getElementById('fileList');
        const uploadBtn = document.getElementById('uploadBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const toastStack = document.getElementById('toastStack');
        // Each queued item carries its own mode/path. This keeps ordinary files
        // and folder uploads from being reinterpreted by a single global flag.
        let selectedUploadFiles = [];  // {file, path, mode, key}

        dropZone.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', (e) => {
            addFiles(e.target.files, 'files');
            e.target.value = '';
        });
        folderInput.addEventListener('change', (e) => {
            addFiles(e.target.files, 'folder');
            e.target.value = '';
        });

        ['dragenter', 'dragover'].forEach(evt => dropZone.addEventListener(evt, (e) => { e.preventDefault(); dropZone.classList.add('dragover'); }));
        ['dragleave', 'drop'].forEach(evt => dropZone.addEventListener(evt, (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); }));
        dropZone.addEventListener('drop', (e) => { addFiles(e.dataTransfer.files, 'files'); });
        // A missed drop must not navigate the browser to the dropped local file.
        ['dragover', 'drop'].forEach(evt => document.addEventListener(evt, (e) => {
            if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) e.preventDefault();
        }));

        function addFiles(files, mode) {
            let skipped = 0;
            for (const file of files) {
                const path = mode === 'folder' ? (file.webkitRelativePath || file.name) : file.name;
                const key = `${mode}:${path}:${file.size}:${file.lastModified || 0}`;
                const duplicate = selectedUploadFiles.some(item => item.key === key);
                if (duplicate) { skipped += 1; continue; }
                selectedUploadFiles.push({ file, path, mode, key });
            }
            if (skipped) showToast(`Уже в очереди: ${skipped} файл(ов) пропущено`);
            renderFileList();
        }
        function renderFileList() {
            fileList.innerHTML = '';
            selectedUploadFiles.forEach((item, idx) => {
                const row = document.createElement('div');
                row.className = 'file-item';
                const prefix = item.mode === 'folder' ? '📁 ' : '';
                row.innerHTML = `<span class="name">${prefix}${escapeHtml(item.path || item.file.name)}</span><span class="size">${formatSize(item.file.size)}</span><button class="remove" data-idx="${idx}" aria-label="Убрать ${escapeHtml(item.path || item.file.name)} из очереди">✕</button>`;
                fileList.appendChild(row);
            });
            fileList.querySelectorAll('.remove').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    selectedUploadFiles.splice(parseInt(e.target.dataset.idx), 1);
                    renderFileList();
                });
            });
            uploadBtn.disabled = selectedUploadFiles.length === 0;
        }
        function formatSize(bytes) {
            if (bytes < 1024) return bytes + ' Б';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' КБ';
            if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' МБ';
            return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' ГБ';
        }
        function escapeHtml(str) { const div = document.createElement('div'); div.textContent = str; return div.innerHTML; }
        // Сообщения не вытесняют друг друга: каждое висит своей плашкой,
        // показываем до 4 одновременно (старейшее уходит первым).
        const TOAST_LIMIT = 4;
        function showToast(msg, isError = false, action = null) {
            if (!toastStack) return;
            while (toastStack.children.length >= TOAST_LIMIT) {
                toastStack.removeChild(toastStack.firstChild);
            }
            const item = document.createElement('div');
            item.className = 'toast' + (isError ? ' error' : '');
            const text = document.createElement('span');
            text.textContent = msg;
            item.appendChild(text);
            if (action && action.label) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'toast-action';
                button.textContent = action.label;
                button.addEventListener('click', () => { item.remove(); action.onClick(); });
                item.appendChild(button);
            }
            toastStack.appendChild(item);
            requestAnimationFrame(() => item.classList.add('show'));
            // С действием плашка живёт дольше — чтобы успеть нажать.
            const lifetime = action ? 10000 : 3500;
            setTimeout(() => {
                item.classList.remove('show');
                setTimeout(() => item.remove(), 300);
            }, lifetime);
        }
        // Возврат удалённых файлов из корзины (undo).
        async function undoDelete(trashIds) {
            if (!trashIds || !trashIds.length) return;
            try {
                const response = await fetch('/api/trash/restore', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        entry_ids: trashIds, folder_id: currentFolderId || null, view: currentView
                    })
                });
                const result = await parseResponseJson(response);
                if (!response.ok) { showToast('❌ ' + (result.error || 'Не удалось восстановить'), true); return; }
                const added = insertRowsHtml(result.rows_html, 'file');
                if (added) {
                    adjustCounters(added, 0);
                    refreshDomTotals();
                    updateSelection();
                    showToast(`✅ Восстановлено: ${result.count}`);
                } else {
                    reloadPreservingScroll(800);
                }
            } catch (err) { showToast('❌ Нет связи с сервером', true); }
        }
        async function parseResponseJson(response) {
            const text = await response.text();
            try {
                return JSON.parse(text);
            } catch (err) {
                return { error: `HTTP ${response.status}: ${text.slice(0, 200) || 'пустой ответ'}` };
            }
        }

        function reloadPreservingScroll(delay = 800) {
            const pane = document.querySelector('.files-content');
            try { sessionStorage.setItem('albionFilesScroll', String(pane ? pane.scrollTop : 0)); } catch (e) {}
            setTimeout(() => location.reload(), delay);
        }
        function reloadAtTop(delay = 800) {
            try { sessionStorage.removeItem('albionFilesScroll'); } catch (e) {}
            setTimeout(() => location.reload(), delay);
        }
        try {
            const savedScroll = sessionStorage.getItem('albionFilesScroll');
            if (savedScroll !== null) {
                const pane = document.querySelector('.files-content');
                if (pane) pane.scrollTop = Number(savedScroll) || 0;
                sessionStorage.removeItem('albionFilesScroll');
            }
        } catch (e) {}

        function copyToClipboard(text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text)
                    .then(() => showToast('✅ Ссылка скопирована'))
                    .catch(() => showToast('❌ Не удалось скопировать', true));
                return;
            }
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            try {
                document.execCommand('copy');
                showToast('✅ Ссылка скопирована');
            } catch (err) {
                showToast('❌ Скопируйте вручную: ' + text, true);
            } finally {
                textarea.remove();
            }
        }

        // Состояние выделения: считаем строки, возвращаем сохранённое выделение
        // (страница могла быть перезагружена применением фильтра или сменой вида).
        refreshDomTotals();
        restoreSelection();
        updateSelection();

        // ===== Ленивая подгрузка файлов вместо пагинации =====
        const lazyLoader = document.getElementById('lazyLoader');
        const loadMoreBtn = document.getElementById('loadMoreBtn');
        const lazyStatus = document.getElementById('lazyStatus');
        let lazyNextUrl = lazyLoader ? (lazyLoader.dataset.nextUrl || '') : '';
        let lazyLoading = false;
        let lazyObserver = null;

        function lazyTarget() {
            if (currentView === 'table') return document.querySelector('#view-table tbody');
            if (currentView === 'grid') return document.querySelector('#view-grid .files-grid');
            return document.querySelector('#view-list .files-list');
        }
        function lazyFileSelector() {
            if (currentView === 'table') return '#view-table .file-row';
            if (currentView === 'grid') return '#view-grid .tile[data-file-id]';
            return '#view-list .list-item[data-file-id]';
        }
        // Вставка готовых строк (HTML от сервера) в активное представление.
        // Папки идут перед файлами, файлы — в конец списка.
        function insertRowsHtml(html, kind) {
            const target = lazyTarget();
            if (!target || !html) return 0;
            const template = document.createElement('template');
            template.innerHTML = html.trim();
            const nodes = Array.from(template.content.children);
            if (!nodes.length) return 0;
            if (kind === 'folder') {
                const firstFile = target.querySelector(lazyFileSelector());
                nodes.forEach(node => firstFile ? target.insertBefore(node, firstFile) : target.appendChild(node));
            } else {
                nodes.forEach(node => target.appendChild(node));
            }
            return nodes.length;
        }
        async function loadNextPage() {
            if (!lazyNextUrl || lazyLoading) return;
            lazyLoading = true;
            loadMoreBtn.disabled = true;
            lazyStatus.textContent = 'Загрузка…';
            try {
                const response = await fetch(lazyNextUrl, { headers: { 'X-Requested-With': 'lazy' } });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const html = await response.text();
                const doc = new DOMParser().parseFromString(html, 'text/html');
                const target = lazyTarget();
                if (!target) throw new Error('Не найдено активное представление');
                doc.querySelectorAll(lazyFileSelector()).forEach(node => {
                    target.appendChild(document.importNode(node, true));
                });
                const nextLoader = doc.getElementById('lazyLoader');
                lazyNextUrl = nextLoader ? (nextLoader.dataset.nextUrl || '') : '';
                lazyLoader.dataset.nextUrl = lazyNextUrl;
                refreshDomTotals();
                updateSelection();
                if (!lazyNextUrl) {
                    lazyLoader.hidden = true;
                    lazyStatus.textContent = 'Все файлы загружены';
                    if (lazyObserver) lazyObserver.disconnect();
                } else {
                    lazyStatus.textContent = '';
                }
            } catch (err) {
                console.error('Ошибка ленивой загрузки:', err);
                lazyStatus.textContent = 'Не удалось загрузить ещё файлы';
                showToast('❌ Не удалось загрузить следующую страницу', true);
            } finally {
                lazyLoading = false;
                loadMoreBtn.disabled = false;
            }
        }
        function setupLazyLoading() {
            if (!lazyNextUrl) return;
            if ('IntersectionObserver' in window) {
                lazyObserver = new IntersectionObserver(entries => {
                    if (entries.some(entry => entry.isIntersecting)) loadNextPage();
                }, { root: document.querySelector('.files-content'), rootMargin: '300px' });
                lazyObserver.observe(lazyLoader);
            } else {
                lazyStatus.textContent = 'Прокрутите вниз или нажмите кнопку';
            }
        }
        setupLazyLoading();

        // ===== Параллельная загрузка: батчи файлов в несколько потоков =====
        const PARALLEL_UPLOADS = 4;   // одновременных запросов
        const FILES_PER_BATCH = 25;   // файлов в одном запросе

        uploadBtn.addEventListener('click', async () => {
            if (selectedUploadFiles.length === 0 || !lockAction('upload')) return;
            uploadBtn.disabled = true;
            progressContainer.style.display = 'block';
            progressFill.style.width = '0%'; progressFill.textContent = '0%';

            try {
            // Group by mode and target folder. A single batch never mixes
            // plain files with folder uploads or different folder paths.
            const groups = new Map();
            for (const item of selectedUploadFiles) {
                const folderPath = item.mode === 'folder'
                    ? item.path.split('/').slice(0, -1).join('/')
                    : '__plain__';
                const key = `${item.mode}:${folderPath}`;
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(item);
            }
            const batches = [];
            for (const items of groups.values()) {
                for (let i = 0; i < items.length; i += FILES_PER_BATCH) {
                    batches.push({ mode: items[0].mode, items: items.slice(i, i + FILES_PER_BATCH) });
                }
            }

            const totalBytes = selectedUploadFiles.reduce((sum, item) => sum + item.file.size, 0);
            const loadedByBatch = new Map();
            const completedKeys = new Set();
            let savedTotal = 0, failedTotal = 0, lastError = '';
            let uploadedRowsHtml = '';

            const updateProgress = () => {
                let loaded = 0;
                loadedByBatch.forEach(value => { loaded += value; });
                const pct = totalBytes ? Math.min(100, Math.round(loaded / totalBytes * 100)) : 100;
                progressFill.style.width = pct + '%'; progressFill.textContent = pct + '%';
            };

            const uploadBatch = (batch) => new Promise((resolve) => {
                const fd = new FormData();
                fd.append('folder_id', currentFolderId);
                batch.items.forEach((item, i) => {
                    fd.append(`file_${i}`, item.file);
                    if (batch.mode === 'folder') fd.append(`path_${i}`, item.path);
                });
                fd.append('count', String(batch.items.length));
                fd.append('view', currentView);

                const xhr = new XMLHttpRequest();
                xhr.open('POST', batch.mode === 'folder' ? '/upload_folder' : '/upload');
                xhr.setRequestHeader('X-CSRF-Token', csrfToken);
                const batchBytes = batch.items.reduce((sum, item) => sum + item.file.size, 0);
                xhr.upload.onprogress = (e) => {
                    // e.loaded включает служебные поля multipart, поэтому не даём
                    // прогрессу обогнать реальный объём файлов в батче.
                    if (e.lengthComputable) { loadedByBatch.set(batch, Math.min(e.loaded, batchBytes)); updateProgress(); }
                };
                xhr.onload = () => {
                    const expected = batch.items.length;
                    if (xhr.status === 200) {
                        let saved = 0;
                        try {
                            const payload = JSON.parse(xhr.responseText);
                            saved = Number(payload.count);
                            if (typeof payload.rows_html === 'string') uploadedRowsHtml += payload.rows_html;
                        } catch (err) {
                            saved = 0;
                        }
                        if (!Number.isFinite(saved) || saved < 0 || saved > expected) {
                            failedTotal += expected;
                            lastError = 'Некорректный ответ сервера';
                        } else {
                            savedTotal += saved;
                            if (saved === expected) {
                                batch.items.forEach(item => completedKeys.add(item.key));
                            } else {
                                failedTotal += expected - saved;
                                lastError = `Сервер принял ${saved} из ${expected} файлов`;
                            }
                        }
                    } else {
                        failedTotal += expected;
                        let msg = `Ошибка ${xhr.status}`;
                        try { const payload = JSON.parse(xhr.responseText); if (payload.error) msg = payload.error; } catch (err) {}
                        lastError = msg;
                        console.error('Ошибка батча:', xhr.status, xhr.responseText);
                    }
                    resolve();
                };
                xhr.onerror = () => { failedTotal += batch.items.length; lastError = 'Ошибка сети'; resolve(); };
                xhr.send(fd);
            });

            // Очередь батчей, PARALLEL_UPLOADS воркеров
            let idx = 0;
            const worker = async () => {
                while (idx < batches.length) {
                    await uploadBatch(batches[idx++]);
                }
            };
            await Promise.all(Array.from({ length: Math.min(PARALLEL_UPLOADS, batches.length) }, worker));
            if (failedTotal === 0) {
                    progressFill.style.width = '100%'; progressFill.textContent = '100%';
                    showToast(`✅ Загружено: ${savedTotal}`);
                    selectedUploadFiles = [];
                    renderFileList();
                    // Строки вставляются на месте — без перезагрузки страницы.
                    const added = insertRowsHtml(uploadedRowsHtml, 'file');
                    if (added) {
                        adjustCounters(added, 0);
                        refreshDomTotals();
                        updateSelection();
                        progressContainer.style.display = 'none';
                    } else {
                        reloadAtTop(800);
                    }
                } else {
                    selectedUploadFiles = selectedUploadFiles.filter(item => !completedKeys.has(item.key));
                    renderFileList();
                    progressContainer.style.display = 'none';
                    uploadBtn.disabled = selectedUploadFiles.length === 0;
                    showToast(`❌ Загружено: ${savedTotal}, не удалось: ${failedTotal}. ${lastError}. Очередь оставлена для повтора.`, true);
                }
            } catch (err) {
                console.error('Ошибка загрузки:', err);
                progressContainer.style.display = 'none';
                showToast('❌ Не удалось выполнить загрузку: ' + err.message, true);
            } finally {
                if (uploadBtn.disabled) uploadBtn.disabled = selectedUploadFiles.length === 0;
                unlockAction('upload');
            }
        });

        document.querySelectorAll('.modal-overlay').forEach(m => {
            m.addEventListener('click', (e) => { if (e.target === m) closeModal(m.id); });
        });
