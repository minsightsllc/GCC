(() => {
    const page = document.querySelector("[data-db-audit-page]");
    if (!page) return;

    const dialog = page.querySelector("[data-db-confirm-dialog]");
    const title = dialog?.querySelector("#gcc-db-confirm-title");
    const detail = dialog?.querySelector("#gcc-db-confirm-detail");
    const countList = dialog?.querySelector("[data-db-confirm-counts]");
    const recordSection = dialog?.querySelector("[data-db-confirm-records]");
    const recordList = dialog?.querySelector("[data-db-confirm-record-list]");
    const acceptButton = dialog?.querySelector("[data-db-confirm-accept]");
    let pendingForm = null;
    let pendingSubmitter = null;
    let requestController = null;
    let requestId = 0;
    let searchTimer = null;
    let searchComposing = false;
    let searchOriginUrl = null;
    let searchInteractionActive = false;
    const selectedRecords = new Map();
    const selectionLimit = 50;

    const getInventory = () => page.querySelector("[data-db-audit-inventory]");
    const getResults = () => page.querySelector("[data-db-audit-results]");
    const getSearchInput = () => page.querySelector("#gcc-db-audit-search");
    const getCheckboxes = () => Array.from(getResults()?.querySelectorAll("[data-db-select-record]") || []);
    const getSelectAll = () => getResults()?.querySelector("[data-db-select-all]");
    const getBulkForm = () => getResults()?.querySelector("[data-db-bulk-form]");

    const showRequestError = (message = "") => {
        const error = page.querySelector("[data-db-request-error]");
        if (!error) return;
        error.textContent = message;
        error.hidden = !message;
    };

    const updateSearchClearLink = () => {
        const input = getSearchInput();
        const clear = page.querySelector("[data-db-search-clear]");
        if (!input || !clear) return;
        clear.hidden = !input.value.trim();
        const url = new URL(window.location.href);
        url.searchParams.delete("q");
        url.searchParams.delete("preview_page");
        url.searchParams.delete("trash_page");
        clear.href = `${url.pathname}${url.search}`;
    };

    const syncSearchFields = (url) => {
        const form = page.querySelector(".gcc-db-audit-search");
        if (!form) return;
        ["cutoff", "kind", "sort", "direction"].forEach((name) => {
            const field = form.querySelector(`input[name="${name}"]`);
            if (field && url.searchParams.has(name)) field.value = url.searchParams.get(name);
        });
    };

    const announce = (message) => {
        const live = page.querySelector("[data-db-audit-announcement]");
        if (live) live.textContent = message;
    };

    const setRecordFromCheckbox = (checkbox) => {
        if (!checkbox?.value) return;
        selectedRecords.set(checkbox.value, {
            type: checkbox.dataset.dbRecordKind || "Audit record",
            summary: checkbox.dataset.dbRecordSummary || "Unnamed record",
            detail: checkbox.dataset.dbRecordDetail || "",
            actor: checkbox.dataset.dbRecordActor || "",
            created: checkbox.dataset.dbRecordCreated || "",
        });
    };

    const updateSelection = () => {
        const checkboxes = getCheckboxes();
        checkboxes.forEach((checkbox) => {
            checkbox.checked = selectedRecords.has(checkbox.value);
            checkbox.disabled = selectedRecords.size >= selectionLimit && !checkbox.checked;
        });

        const selectAll = getSelectAll();
        if (selectAll) {
            const checkedCount = checkboxes.filter((checkbox) => selectedRecords.has(checkbox.value)).length;
            selectAll.checked = checkboxes.length > 0 && checkedCount === checkboxes.length;
            selectAll.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
            selectAll.disabled = checkboxes.length === 0
                || (!selectAll.checked && selectedRecords.size + checkboxes.filter((checkbox) => !selectedRecords.has(checkbox.value)).length > selectionLimit);
        }

        const form = getBulkForm();
        if (form) {
            form.querySelectorAll("[data-db-hidden-selection]").forEach((input) => input.remove());
            const visibleValues = new Set(checkboxes.map((checkbox) => checkbox.value));
            selectedRecords.forEach((record, value) => {
                if (visibleValues.has(value)) return;
                const input = document.createElement("input");
                input.type = "hidden";
                input.name = "selected_records";
                input.value = value;
                input.dataset.dbHiddenSelection = "true";
                form.append(input);
            });
            form.dataset.confirmActivity = String(Array.from(selectedRecords.values()).filter((record) => record.type === "Activity").length);
            form.dataset.confirmSecurity = String(selectedRecords.size - Number(form.dataset.confirmActivity));
            form.querySelectorAll("[data-db-selection-action]").forEach((button) => {
                button.disabled = selectedRecords.size === 0;
            });
        }

        const count = page.querySelector("[data-db-selection-count]");
        if (count) count.textContent = `${selectedRecords.size} selected`;
    };

    const getRequestUrl = (url) => {
        const requestUrl = new URL(url.href);
        requestUrl.searchParams.set("records", "1");
        return requestUrl;
    };

    const replaceResults = async (url, { pushHistory = true, focusSelector = null, field = "filter" } = {}) => {
        const inventory = getInventory();
        const currentResults = getResults();
        if (!inventory || !currentResults) return;

        showRequestError();
        requestController?.abort();
        requestController = new AbortController();
        const activeRequestId = ++requestId;
        inventory.setAttribute("aria-busy", "true");

        try {
            const response = await fetch(getRequestUrl(url).href, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
                cache: "no-store",
                signal: requestController.signal,
            });
            if (!response.ok) {
                showRequestError(`Audit records could not be updated (HTTP ${response.status}). Your current list is still available.`);
                return;
            }
            const markup = await response.text();
            const parsedDocument = new DOMParser().parseFromString(markup, "text/html");
            const nextResults = parsedDocument.querySelector("[data-db-audit-results]");
            if (!nextResults || activeRequestId !== requestId) {
                if (activeRequestId === requestId) showRequestError("Audit records could not be updated. Your current list is still available.");
                return;
            }

            currentResults.replaceWith(document.importNode(nextResults, true));
            if (pushHistory) window.history.pushState({}, "", url.href);
            syncSearchFields(url);
            updateSelection();
            updateSearchClearLink();
            if (focusSelector) page.querySelector(focusSelector)?.focus({ preventScroll: true });
            if (field === "search") announce(url.searchParams.get("q") ? "Audit records filtered." : "Search cleared. Showing all matching audit records.");
            else if (field === "sort") announce("Audit records sorted.");
            else if (field === "kind") announce("Audit record filter updated.");
        } catch (error) {
            if (error.name !== "AbortError") {
                console.error("Could not update audit records.", error);
                showRequestError("Audit records could not be updated. Your current list is still available; try again.");
            }
        } finally {
            if (activeRequestId === requestId) inventory.removeAttribute("aria-busy");
        }
    };

    const getSearchUrl = () => {
        const input = getSearchInput();
        const url = new URL(window.location.href);
        const query = input?.value.trim() || "";
        if (query) url.searchParams.set("q", query);
        else url.searchParams.delete("q");
        url.searchParams.delete("preview_page");
        url.searchParams.delete("trash_page");
        url.searchParams.delete("records");
        return url;
    };

    const scheduleSearch = (delay = 250) => {
        const input = getSearchInput();
        if (!input) return;
        updateSearchClearLink();
        if (!searchInteractionActive) {
            searchInteractionActive = true;
            searchOriginUrl = window.location.href;
        }
        window.clearTimeout(searchTimer);
        requestController?.abort();
        searchTimer = window.setTimeout(() => {
            searchTimer = null;
            const url = getSearchUrl();
            if (url.href === window.location.href) {
                searchInteractionActive = false;
                searchOriginUrl = null;
                return;
            }
            const pushHistory = window.location.href === searchOriginUrl;
            window.history[pushHistory ? "pushState" : "replaceState"]({}, "", url.href);
            replaceResults(url, { pushHistory: false, field: "search" }).finally(() => {
                if (getSearchInput()?.value.trim() === (url.searchParams.get("q") || "")) {
                    searchInteractionActive = false;
                    searchOriginUrl = null;
                }
            });
        }, delay);
    };

    const submitConfirmedAction = () => {
        if (!pendingForm || !pendingSubmitter) return;
        let confirmed = pendingForm.querySelector('input[name="confirmed"]');
        if (!confirmed) {
            confirmed = document.createElement("input");
            confirmed.type = "hidden";
            confirmed.name = "confirmed";
            pendingForm.append(confirmed);
        }
        confirmed.value = "1";
        if (acceptButton) {
            acceptButton.disabled = true;
            acceptButton.textContent = "Deleting…";
        }
        pendingForm.requestSubmit(pendingSubmitter);
    };

    const showConfirmation = (form, submitter) => {
        if (!dialog || typeof dialog.showModal !== "function") return false;
        const isSelectedAction = form.matches("[data-db-bulk-form]");
        title.textContent = form.dataset.confirmTitle || "Permanently delete these records?";
        detail.textContent = form.dataset.confirmDetail || "This action cannot be undone.";
        countList.replaceChildren();
        recordList?.replaceChildren();

        const activityCount = Number.parseInt(form.dataset.confirmActivity || "0", 10) || 0;
        const securityCount = Number.parseInt(form.dataset.confirmSecurity || "0", 10) || 0;
        [["Activity records", activityCount], ["Security events", securityCount]].forEach(([label, value]) => {
            if (!value) return;
            const item = document.createElement("li");
            item.textContent = `${label}: ${value.toLocaleString()}`;
            countList.append(item);
        });

        if (isSelectedAction && recordList) {
            Array.from(selectedRecords.values()).forEach((record) => {
                const item = document.createElement("li");
                const summary = record.summary || "Unnamed record";
                const detailText = record.detail ? ` — ${record.detail}` : "";
                const actorText = record.actor ? ` · ${record.actor}` : "";
                const createdText = record.created ? ` · ${record.created}` : "";
                item.textContent = `${record.type}: ${summary}${detailText}${actorText}${createdText}`;
                recordList.append(item);
            });
            if (recordSection) recordSection.hidden = selectedRecords.size === 0;
        } else if (recordSection) {
            recordSection.hidden = true;
        }

        pendingForm = form;
        pendingSubmitter = submitter;
        acceptButton.disabled = false;
        acceptButton.textContent = "Delete permanently";
        dialog.showModal();
        dialog.querySelector("[data-db-confirm-cancel]")?.focus({ preventScroll: true });
        return true;
    };

    page.addEventListener("click", (event) => {
        const clearLink = event.target.closest?.("[data-db-search-clear]");
        if (clearLink) {
            event.preventDefault();
            const input = getSearchInput();
            if (input) {
                input.value = "";
                input.focus();
            }
            scheduleSearch(0);
            return;
        }

        const confirmTrigger = event.target.closest?.("[data-db-confirm-trigger]");
        if (confirmTrigger && !confirmTrigger.disabled) {
            const form = confirmTrigger.closest("form");
            if (form && showConfirmation(form, confirmTrigger)) event.preventDefault();
            return;
        }

        const link = event.target.closest?.("[data-db-kind-link], [data-db-sort-link], [data-db-page-link]");
        if (!link) return;
        event.preventDefault();
        const url = new URL(link.href, window.location.href);
        window.clearTimeout(searchTimer);
        searchTimer = null;
        const typedQuery = getSearchInput()?.value.trim() || "";
        const queryChanged = typedQuery !== (url.searchParams.get("q") || "");
        if (typedQuery) url.searchParams.set("q", typedQuery);
        else url.searchParams.delete("q");
        if (link.matches("[data-db-page-link]") && !queryChanged) {
            const activePageParam = page.dataset.dbAuditPage === "trash" ? "trash_page" : "preview_page";
            const inactivePageParam = activePageParam === "trash_page" ? "preview_page" : "trash_page";
            url.searchParams.delete(inactivePageParam);
        } else {
            url.searchParams.delete("preview_page");
            url.searchParams.delete("trash_page");
        }
        searchInteractionActive = false;
        searchOriginUrl = null;
        let focusSelector = null;
        let field = "filter";
        if (link.matches("[data-db-kind-link]")) {
            focusSelector = `[data-db-kind-link][data-db-kind="${url.searchParams.get("kind") || "all"}"]`;
            field = "kind";
        } else if (link.matches("[data-db-sort-link]")) {
            const sort = link.dataset.dbSortField || url.searchParams.get("sort");
            const direction = link.dataset.dbSortDirection || url.searchParams.get("direction");
            focusSelector = `[data-db-sort-link][data-db-sort-field="${sort}"][data-db-sort-direction="${direction}"]`;
            field = "sort";
        } else {
            focusSelector = `[data-db-page-link][data-db-page-step="${link.dataset.dbPageStep}"]`;
        }
        replaceResults(url, { focusSelector, field });
    });

    page.addEventListener("change", (event) => {
        const target = event.target;
        if (target.matches("[data-db-select-record]")) {
            if (target.checked) {
                if (selectedRecords.size >= selectionLimit && !selectedRecords.has(target.value)) {
                    target.checked = false;
                    announce(`Select up to ${selectionLimit} records at a time.`);
                    return;
                }
                setRecordFromCheckbox(target);
            } else {
                selectedRecords.delete(target.value);
            }
            updateSelection();
            return;
        }
        if (!target.matches("[data-db-select-all]")) return;
        const checkboxes = getCheckboxes();
        const newSelections = checkboxes.filter((checkbox) => !selectedRecords.has(checkbox.value));
        if (target.checked && selectedRecords.size + newSelections.length > selectionLimit) {
            target.checked = false;
            announce(`Select up to ${selectionLimit} records at a time.`);
            updateSelection();
            return;
        }
        checkboxes.forEach((checkbox) => {
            if (target.checked) setRecordFromCheckbox(checkbox);
            else selectedRecords.delete(checkbox.value);
        });
        updateSelection();
    });

    page.addEventListener("input", (event) => {
        if (!event.target.matches("#gcc-db-audit-search")) return;
        if (searchComposing || event.isComposing) {
            updateSearchClearLink();
            return;
        }
        scheduleSearch();
    });

    page.addEventListener("compositionstart", (event) => {
        if (event.target.matches("#gcc-db-audit-search")) searchComposing = true;
    });
    page.addEventListener("compositionend", (event) => {
        if (!event.target.matches("#gcc-db-audit-search")) return;
        searchComposing = false;
        scheduleSearch();
    });
    page.addEventListener("submit", (event) => {
        if (!event.target.matches(".gcc-db-audit-search")) return;
        event.preventDefault();
        scheduleSearch(0);
    });

    dialog?.querySelector("[data-db-confirm-cancel]")?.addEventListener("click", () => {
        dialog.close();
        pendingForm = null;
        pendingSubmitter = null;
    });
    acceptButton?.addEventListener("click", submitConfirmedAction);

    window.addEventListener("popstate", () => {
        window.clearTimeout(searchTimer);
        searchTimer = null;
        searchInteractionActive = false;
        searchOriginUrl = null;
        const url = new URL(window.location.href);
        const input = getSearchInput();
        if (input) input.value = url.searchParams.get("q") || "";
        syncSearchFields(url);
        updateSearchClearLink();
        replaceResults(url, { pushHistory: false });
    });

    updateSearchClearLink();
    updateSelection();
})();
