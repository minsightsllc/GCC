document.addEventListener("DOMContentLoaded", () => {
    const fileCheckboxes = Array.from(document.querySelectorAll("[data-storage-select-file]"));
    const selectAllControls = Array.from(document.querySelectorAll("[data-storage-select-all-status]"));
    const actionButtons = Array.from(document.querySelectorAll("#gcc-storage-bulk-form button[name='bulk_action']"));
    const selectionCount = document.querySelector("[data-storage-selection-count]");
    const bulkForm = document.querySelector("#gcc-storage-bulk-form");
    const deleteDialog = document.querySelector("[data-storage-delete-dialog]");
    const deleteOpenButton = document.querySelector("[data-storage-delete-open]");
    const deleteConfirmButton = document.querySelector("[data-storage-delete-confirm]");
    const deleteCancelButton = document.querySelector("[data-storage-delete-cancel]");
    const deleteList = document.querySelector("[data-storage-delete-list]");
    const confirmDeleteInput = document.querySelector("#gcc-storage-confirm-delete");

    const updateSelection = () => {
        const selected = fileCheckboxes.filter((checkbox) => checkbox.checked);
        const selectedStatuses = new Set(selected.map((checkbox) => checkbox.dataset.storageStatus));
        const lockedStatus = selectedStatuses.size === 1 ? selectedStatuses.values().next().value : null;

        fileCheckboxes.forEach((checkbox) => {
            const isProtected = checkbox.dataset.storageSelectProtected === "true";
            checkbox.disabled = isProtected || Boolean(lockedStatus && checkbox.dataset.storageStatus !== lockedStatus);
        });

        selectAllControls.forEach((control) => {
            const status = control.dataset.storageSelectAllStatus;
            const group = fileCheckboxes.filter((checkbox) => (
                checkbox.dataset.storageStatus === status
                && checkbox.dataset.storageSelectProtected !== "true"
            ));
            const groupSelected = group.filter((checkbox) => checkbox.checked);
            control.disabled = group.length === 0 || Boolean(lockedStatus && status !== lockedStatus);
            control.checked = group.length > 0 && groupSelected.length === group.length;
            control.indeterminate = groupSelected.length > 0 && groupSelected.length < group.length;
        });

        actionButtons.forEach((button) => {
            const action = button.value;
            const status = lockedStatus;
            const isSupported = action === "trash"
                ? status === "in-use" || status === "recovered"
                : (action === "restore" || action === "delete") && status === "trash";
            button.disabled = selected.length === 0 || !isSupported;
        });

        if (selectionCount) {
            selectionCount.textContent = `${selected.length} selected`;
        }
    };

    selectAllControls.forEach((control) => {
        control.addEventListener("change", () => {
            const status = control.dataset.storageSelectAllStatus;
            fileCheckboxes.forEach((checkbox) => {
                if (checkbox.dataset.storageStatus === status && checkbox.dataset.storageSelectProtected !== "true") {
                    checkbox.checked = control.checked;
                } else if (control.checked) {
                    checkbox.checked = false;
                }
            });
            updateSelection();
        });
    });

    fileCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", updateSelection));

    deleteOpenButton?.addEventListener("click", (event) => {
        event.preventDefault();
        if (!deleteDialog || !deleteList) return;

        const selectedTrashFiles = fileCheckboxes.filter((checkbox) => (
            checkbox.checked && checkbox.dataset.storageStatus === "trash"
        ));
        if (!selectedTrashFiles.length) return;

        confirmDeleteInput.value = "";
        deleteList.replaceChildren();
        selectedTrashFiles.forEach((checkbox) => {
            const row = document.createElement("li");
            const fileName = document.createElement("strong");
            const fileSize = document.createElement("span");
            fileName.textContent = checkbox.dataset.storageFileName || "Unnamed file";
            fileSize.textContent = checkbox.dataset.storageFileSize || "Size unavailable";
            row.append(fileName, fileSize);
            deleteList.append(row);
        });

        if (typeof deleteDialog.showModal === "function") deleteDialog.showModal();
    });

    deleteCancelButton?.addEventListener("click", () => {
        if (confirmDeleteInput) confirmDeleteInput.value = "";
        deleteDialog?.close();
    });

    deleteConfirmButton?.addEventListener("click", () => {
        if (confirmDeleteInput) confirmDeleteInput.value = "yes";
    });

    bulkForm?.addEventListener("submit", (event) => {
        if (event.submitter === deleteOpenButton) event.preventDefault();
    });

    updateSelection();
});
