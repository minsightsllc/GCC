const initializeStorageRetention = () => {
    const storagePage = document.querySelector(".gcc-storage-page");
    if (!storagePage) return;

    const selectedFiles = new Map();
    const statusPriorities = ["in-use", "trash", "recovered"];
    const deleteDialog = document.querySelector("[data-storage-delete-dialog]");
    const deleteList = document.querySelector("[data-storage-delete-list]");
    const deleteCancelButton = document.querySelector("[data-storage-delete-cancel]");
    const deleteConfirmButton = document.querySelector("[data-storage-delete-confirm]");
    let sortController = null;
    let sortRequestId = 0;
    let searchDebounce = null;
    let searchInteractionActive = false;
    let searchOriginUrl = null;
    let searchComposing = false;

    const getFileCheckboxes = () => Array.from(storagePage.querySelectorAll("[data-storage-select-file]"));
    const getSelectAllControls = () => Array.from(storagePage.querySelectorAll("[data-storage-select-all-status]"));
    const getSearchInput = () => storagePage.querySelector("#gcc-storage-search");
    const getInventoryError = () => storagePage.querySelector("[data-storage-request-error]");

    const showInventoryError = (message = "") => {
        const error = getInventoryError();
        if (!error) return;
        error.textContent = message;
        error.hidden = !message;
    };

    const updateSearchClearLink = () => {
        const input = getSearchInput();
        const clearLink = storagePage.querySelector("[data-storage-search-clear]");
        if (!input || !clearLink) return;
        clearLink.hidden = !input.value.trim();
        const clearUrl = new URL(window.location.href);
        clearUrl.searchParams.delete("q");
        clearUrl.searchParams.delete("page");
        clearLink.href = `${clearUrl.pathname}${clearUrl.search}`;
    };

    const getSearchUrl = () => {
        const input = getSearchInput();
        const url = new URL(window.location.href);
        const query = input?.value.trim() || "";
        if (query) url.searchParams.set("q", query);
        else url.searchParams.delete("q");
        url.searchParams.delete("page");
        return url;
    };

    const scheduleLiveSearch = (delay = 250) => {
        const input = getSearchInput();
        if (!input) return;
        updateSearchClearLink();
        if (!searchInteractionActive) {
            searchInteractionActive = true;
            searchOriginUrl = window.location.href;
        }
        window.clearTimeout(searchDebounce);
        sortController?.abort();
        searchDebounce = window.setTimeout(() => {
            searchDebounce = null;
            const url = getSearchUrl();
            if (url.href === window.location.href) {
                searchInteractionActive = false;
                searchOriginUrl = null;
                return;
            }
            if (window.location.href === searchOriginUrl) window.history.pushState({}, "", url.href);
            else window.history.replaceState({}, "", url.href);
            refreshInventory(url, {
                pushHistory: false,
                field: "search",
                preserveSearch: true,
            });
        }, delay);
    };

    const updateSelection = () => {
        const checkboxes = getFileCheckboxes();
        const selectedStatuses = new Set(Array.from(selectedFiles.values(), (file) => file.status));
        const lockedStatus = selectedStatuses.size === 1 ? selectedStatuses.values().next().value : null;

        checkboxes.forEach((checkbox) => {
            const value = checkbox.value;
            checkbox.checked = Boolean(value && selectedFiles.has(value));
            const isProtected = checkbox.dataset.storageSelectProtected === "true";
            const selectionLimitReached = selectedFiles.size >= 50 && !selectedFiles.has(value);
            checkbox.disabled = isProtected
                || Boolean(lockedStatus && checkbox.dataset.storageStatus !== lockedStatus)
                || selectionLimitReached;
        });

        getSelectAllControls().forEach((control) => {
            const status = control.dataset.storageSelectAllStatus;
            const group = checkboxes.filter((checkbox) => (
                checkbox.dataset.storageStatus === status
                && checkbox.dataset.storageSelectProtected !== "true"
            ));
            const groupSelected = group.filter((checkbox) => checkbox.value && selectedFiles.has(checkbox.value));
            const additionalSelections = group.filter((checkbox) => !checkbox.value || !selectedFiles.has(checkbox.value)).length;
            control.disabled = group.length === 0
                || Boolean(lockedStatus && status !== lockedStatus)
                || (!control.checked && selectedFiles.size + additionalSelections > 50);
            control.checked = group.length > 0 && groupSelected.length === group.length;
            control.indeterminate = groupSelected.length > 0 && groupSelected.length < group.length;
        });

        storagePage.querySelectorAll("#gcc-storage-bulk-form button[name='bulk_action']").forEach((button) => {
            const status = lockedStatus;
            const isSupported = button.value === "trash"
                ? status === "in-use" || status === "recovered"
                : (button.value === "restore" || button.value === "delete") && status === "trash";
            button.disabled = selectedFiles.size === 0 || !isSupported;
        });

        const selectionCount = storagePage.querySelector("[data-storage-selection-count]");
        if (selectionCount) selectionCount.textContent = `${selectedFiles.size} selected`;

        const form = storagePage.querySelector("#gcc-storage-bulk-form");
        if (form) {
            form.querySelectorAll("[data-storage-hidden-selection]").forEach((input) => input.remove());
            const visibleValues = new Set(checkboxes.map((checkbox) => checkbox.value).filter(Boolean));
            selectedFiles.forEach((_, value) => {
                if (visibleValues.has(value)) return;
                const input = document.createElement("input");
                input.type = "hidden";
                input.name = "selected_files";
                input.value = value;
                input.dataset.storageHiddenSelection = "true";
                form.append(input);
            });
        }
    };

    const setSelectedFile = (checkbox) => {
        if (!checkbox.value || checkbox.dataset.storageSelectProtected === "true") return;
        selectedFiles.set(checkbox.value, {
            status: checkbox.dataset.storageStatus,
            name: checkbox.dataset.storageFileName || "Unnamed file",
            size: checkbox.dataset.storageFileSize || "Size unavailable",
            type: checkbox.dataset.storageFileType || "File",
            record: checkbox.dataset.storageRecordTitle || "",
            project: checkbox.dataset.storageProjectTitle || "",
            statusLabel: checkbox.dataset.storageFileStatusLabel || "In Trash",
            statusClass: checkbox.dataset.storageFileStatusClass || "is-trashed",
        });
    };

    const updateSelectedFile = (checkbox) => {
        if (!checkbox.value) return;
        if (checkbox.checked) setSelectedFile(checkbox);
        else selectedFiles.delete(checkbox.value);
    };

    const announceSort = (parsedPage, field, url) => {
        const nextMain = parsedPage.querySelector(".gcc-storage-page");
        if (nextMain) {
            storagePage.dataset.storageCurrentSort = nextMain.dataset.storageCurrentSort;
            storagePage.dataset.storageCurrentDirection = nextMain.dataset.storageCurrentDirection;
            storagePage.dataset.storageCurrentStatusFirst = nextMain.dataset.storageCurrentStatusFirst;
        }
        const status = storagePage.querySelector("[data-storage-sort-status]");
        const direction = url.searchParams.get("direction") || "asc";
        const first = url.searchParams.get("status_first") || "in-use";
        const statusNames = { "in-use": "In use", trash: "In Trash", recovered: "Recovered" };
        const results = {
            file: `File names sorted ${direction === "asc" ? "A to Z" : "Z to A"}.`,
            size: `File sizes sorted ${direction === "asc" ? "smallest to biggest" : "biggest to smallest"}.`,
            record: `Records and projects sorted ${direction === "asc" ? "A to Z" : "Z to A"}.`,
            sharing: `Sharing status sorted ${direction === "asc" ? "A to Z" : "Z to A"}.`,
            status: `${statusNames[first] || "In use"} files shown first.`,
            search: url.searchParams.has("q") ? "File inventory filtered." : "Search cleared. Showing all files.",
        };
        if (status) status.textContent = results[field] || "Files sorted.";
    };

    const replaceInventoryPart = (currentInventory, nextInventory, selector, placement) => {
        const current = currentInventory.querySelector(selector);
        const next = nextInventory.querySelector(selector);
        const replacement = next ? document.importNode(next, true) : null;

        if (current && replacement) current.replaceWith(replacement);
        else if (current) current.remove();
        else if (replacement && placement === "before-table") {
            currentInventory.querySelector(".gcc-storage-table-wrap")?.before(replacement);
        } else if (replacement && placement === "after-table") {
            currentInventory.querySelector(".gcc-storage-table-wrap")?.after(replacement);
        } else if (replacement) {
            currentInventory.append(replacement);
        }
    };

    const refreshInventory = async (url, {
        pushHistory = true,
        field = "file",
        focusSelector = null,
        preserveSearch = false,
    } = {}) => {
        const inventory = storagePage.querySelector(".gcc-storage-inventory");
        const tableWrap = inventory?.querySelector(".gcc-storage-table-wrap");
        if (!inventory || !tableWrap) {
            showInventoryError("The file list could not be updated. Use Search to try again.");
            return;
        }

        showInventoryError();
        sortController?.abort();
        sortController = new AbortController();
        const requestId = ++sortRequestId;
        inventory.setAttribute("aria-busy", "true");
        storagePage.querySelectorAll("[data-storage-sort-field]").forEach((button) => {
            button.disabled = true;
        });

        try {
            const response = await fetch(url.href, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
                cache: "no-store",
                signal: sortController.signal,
            });
            if (!response.ok) {
                showInventoryError(`The file list could not be updated (HTTP ${response.status}). Use Search to try again.`);
                return;
            }

            const markup = await response.text();
            const parsedPage = new DOMParser().parseFromString(markup, "text/html");
            const nextInventory = parsedPage.querySelector(".gcc-storage-inventory");
            if (!nextInventory || !nextInventory.querySelector(".gcc-storage-table")) {
                showInventoryError("The file list could not be updated. Use Search to try again.");
                return;
            }
            if (requestId !== sortRequestId) return;

            const selectors = preserveSearch
                ? [".gcc-storage-filters"]
                : [".gcc-storage-filters", ".gcc-storage-search"];
            selectors.forEach((selector) => {
                replaceInventoryPart(inventory, nextInventory, selector);
            });
            replaceInventoryPart(inventory, nextInventory, "#gcc-storage-bulk-form", "before-table");
            replaceInventoryPart(inventory, nextInventory, "#gcc-storage-empty-trash-form");
            replaceInventoryPart(inventory, nextInventory, ".gcc-storage-table", "before-table");
            replaceInventoryPart(inventory, nextInventory, ".gcc-storage-pagination", "after-table");

            if (pushHistory) window.history.pushState({}, "", url.href);
            announceSort(parsedPage, field, url);
            if (!inventory.querySelector("#gcc-storage-bulk-form")) selectedFiles.clear();
            updateSelection();
            if (preserveSearch && getSearchInput()?.value.trim() === (url.searchParams.get("q") || "")) {
                searchInteractionActive = false;
                searchOriginUrl = null;
            }
            if (focusSelector) {
                storagePage.querySelector(focusSelector)?.focus({ preventScroll: true });
            }
        } catch (error) {
            if (error.name === "AbortError") return;
            console.error("Could not refresh the file inventory.", error);
            showInventoryError("The file list could not be updated. Your current view is still available; use Search to try again.");
        } finally {
            if (requestId === sortRequestId) {
                inventory.removeAttribute("aria-busy");
                storagePage.querySelectorAll("[data-storage-sort-field]").forEach((button) => {
                    button.disabled = false;
                });
            }
        }
    };

    const sortInventory = (button) => {
        const typedQuery = getSearchInput()?.value.trim() || "";
        const params = new URLSearchParams(window.location.search);
        const currentQuery = params.get("q") || "";
        const hasPendingSearch = searchDebounce !== null || typedQuery !== currentQuery;
        const pendingSearchOrigin = searchOriginUrl;
        if (hasPendingSearch) {
            window.clearTimeout(searchDebounce);
            searchDebounce = null;
            if (typedQuery) params.set("q", typedQuery);
            else params.delete("q");
        }
        searchInteractionActive = false;
        searchOriginUrl = null;
        const field = button.dataset.storageSortField;

        if (field === "status") {
            const requestedPriority = params.get("status_first");
            const currentPriority = statusPriorities.includes(requestedPriority)
                ? requestedPriority
                : storagePage.dataset.storageCurrentStatusFirst || "in-use";
            const index = statusPriorities.indexOf(currentPriority);
            const offset = button.dataset.storageStatusStep === "previous" ? -1 : 1;
            params.set("status_first", statusPriorities[(index + offset + statusPriorities.length) % statusPriorities.length]);
            params.set("sort", "status");
        } else {
            params.set("sort", field);
            params.set("direction", button.dataset.storageSortDirection);
        }
        params.delete("page");

        const url = new URL(window.location.href);
        url.search = params.toString();
        const focusSelector = field === "status"
            ? `[data-storage-sort-field="status"][data-storage-status-step="${button.dataset.storageStatusStep}"]`
            : `[data-storage-sort-field="${field}"][data-storage-sort-direction="${button.dataset.storageSortDirection}"]`;
        if (url.href !== window.location.href) {
            if (hasPendingSearch) {
                if (window.location.href === pendingSearchOrigin) window.history.pushState({}, "", url.href);
                else window.history.replaceState({}, "", url.href);
                refreshInventory(url, { pushHistory: false, field, focusSelector });
            } else {
                refreshInventory(url, { field, focusSelector });
            }
        }
    };

    const showDeleteConfirmation = (files, mode) => {
        if (!deleteDialog || !deleteList || !files.length) return;

        const title = storagePage.querySelector("[data-storage-delete-title]");
        const selectedConfirm = storagePage.querySelector("[data-storage-delete-confirm]");
        const emptyConfirm = storagePage.querySelector("[data-storage-empty-confirm]");
        const confirmDeleteInput = storagePage.querySelector("#gcc-storage-confirm-delete");
        if (confirmDeleteInput) confirmDeleteInput.value = "";
        if (title) {
            title.textContent = mode === "empty"
                ? "Are you sure you want to permanently delete all files in Trash?"
                : "Are you sure you want to permanently delete the following files?";
        }
        if (selectedConfirm) selectedConfirm.hidden = mode === "empty";
        if (emptyConfirm) emptyConfirm.hidden = mode !== "empty";
        deleteList.replaceChildren();

        files.forEach((file) => {
            const row = document.createElement("tr");
            const fileName = document.createElement("strong");
            const fileType = document.createElement("small");
            const fileCell = document.createElement("td");
            const sizeCell = document.createElement("td");
            const recordCell = document.createElement("td");
            const statusCell = document.createElement("td");
            const recordTitle = document.createElement("strong");
            const status = document.createElement("span");

            fileName.textContent = file.name;
            fileType.textContent = file.type || "File";
            fileCell.append(fileName, fileType);
            sizeCell.textContent = file.size || "Size unavailable";
            recordTitle.textContent = file.record || "—";
            recordCell.append(recordTitle);
            if (file.project) {
                const project = document.createElement("small");
                project.textContent = file.project;
                recordCell.append(project);
            }
            status.className = `gcc-storage-status ${file.statusClass || "is-trashed"}`;
            status.textContent = file.statusLabel || "In Trash";
            statusCell.append(status);
            row.append(fileCell, sizeCell, recordCell, statusCell);
            deleteList.append(row);
        });

        if (typeof deleteDialog.showModal === "function") deleteDialog.showModal();
    };

    const openDeleteConfirmation = () => {
        const files = Array.from(selectedFiles.values()).filter((file) => file.status === "trash");
        showDeleteConfirmation(files, "selected");
    };

    const openEmptyTrashConfirmation = () => {
        const template = storagePage.querySelector("template[data-storage-trash-modal-items]");
        const files = Array.from(template?.content.querySelectorAll("[data-storage-trash-item]") || [], (item) => ({
            name: item.dataset.fileName || "Unnamed file",
            size: item.dataset.fileSize || "Size unavailable",
            type: item.dataset.fileType || "File",
            record: item.dataset.recordTitle || "",
            project: item.dataset.projectTitle || "",
            statusLabel: item.dataset.fileStatusLabel || "In Trash",
            statusClass: item.dataset.fileStatusClass || "is-trashed",
        }));
        showDeleteConfirmation(files, "empty");
    };

    storagePage.addEventListener("click", (event) => {
        const clearSearchLink = event.target.closest?.("[data-storage-search-clear]");
        if (clearSearchLink) {
            event.preventDefault();
            const input = getSearchInput();
            if (input) {
                input.value = "";
                input.focus();
            }
            scheduleLiveSearch(0);
            return;
        }

        const sortButton = event.target.closest?.("[data-storage-sort-field]");
        if (sortButton && !sortButton.disabled) {
            event.preventDefault();
            sortInventory(sortButton);
            return;
        }

        const deleteOpenButton = event.target.closest?.("[data-storage-delete-open]");
        if (deleteOpenButton) {
            event.preventDefault();
            openDeleteConfirmation();
            return;
        }

        const deleteAllButton = event.target.closest?.("[data-storage-delete-open-all]");
        if (deleteAllButton) {
            event.preventDefault();
            openEmptyTrashConfirmation();
        }
    });

    storagePage.addEventListener("change", (event) => {
        const checkbox = event.target;
        if (checkbox.matches("[data-storage-select-file]")) {
            updateSelectedFile(checkbox);
            updateSelection();
            return;
        }

        if (!checkbox.matches("[data-storage-select-all-status]")) return;
        const status = checkbox.dataset.storageSelectAllStatus;
        const group = getFileCheckboxes().filter((fileCheckbox) => (
            fileCheckbox.dataset.storageStatus === status
            && fileCheckbox.dataset.storageSelectProtected !== "true"
        ));
        const additionalSelections = group.filter((fileCheckbox) => !selectedFiles.has(fileCheckbox.value)).length;
        if (checkbox.checked && selectedFiles.size + additionalSelections > 50) {
            checkbox.checked = false;
            const sortStatus = storagePage.querySelector("[data-storage-sort-status]");
            if (sortStatus) sortStatus.textContent = "Select up to 50 files at a time.";
            updateSelection();
            return;
        }
        group.forEach((fileCheckbox) => {
            if (checkbox.checked) setSelectedFile(fileCheckbox);
            else selectedFiles.delete(fileCheckbox.value);
        });
        updateSelection();
    });

    storagePage.addEventListener("input", (event) => {
        if (!event.target.matches("#gcc-storage-search")) return;
        if (searchComposing || event.isComposing) {
            updateSearchClearLink();
            return;
        }
        scheduleLiveSearch();
    });

    storagePage.addEventListener("compositionstart", (event) => {
        if (event.target.matches("#gcc-storage-search")) searchComposing = true;
    });

    storagePage.addEventListener("compositionend", (event) => {
        if (!event.target.matches("#gcc-storage-search")) return;
        searchComposing = false;
        scheduleLiveSearch();
    });

    storagePage.addEventListener("submit", (event) => {
        if (!event.target.matches(".gcc-storage-search")) return;
        event.preventDefault();
        const url = getSearchUrl();
        if (url.href === window.location.href) {
            window.clearTimeout(searchDebounce);
            searchDebounce = null;
            searchInteractionActive = false;
            searchOriginUrl = null;
            refreshInventory(url, { pushHistory: false, field: "search", preserveSearch: true });
            return;
        }
        scheduleLiveSearch(0);
    });

    deleteCancelButton?.addEventListener("click", () => {
        const confirmDeleteInput = storagePage.querySelector("#gcc-storage-confirm-delete");
        if (confirmDeleteInput) confirmDeleteInput.value = "";
        deleteDialog?.close();
    });

    deleteConfirmButton?.addEventListener("click", () => {
        const confirmDeleteInput = storagePage.querySelector("#gcc-storage-confirm-delete");
        if (confirmDeleteInput) confirmDeleteInput.value = "yes";
    });

    document.addEventListener("popstate", () => {
        window.clearTimeout(searchDebounce);
        searchDebounce = null;
        searchInteractionActive = false;
        searchOriginUrl = null;
        refreshInventory(new URL(window.location.href), { pushHistory: false });
    });

    updateSearchClearLink();
    updateSelection();
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeStorageRetention, { once: true });
} else {
    initializeStorageRetention();
}
