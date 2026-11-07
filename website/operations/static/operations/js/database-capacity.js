(() => {
    const dialog = document.querySelector("[data-db-confirm-dialog]");
    if (!dialog || typeof dialog.showModal !== "function") return;

    const title = dialog.querySelector("#gcc-db-confirm-title");
    const detail = dialog.querySelector("#gcc-db-confirm-detail");
    const counts = dialog.querySelector("[data-db-confirm-counts]");
    const accept = dialog.querySelector("[data-db-confirm-accept]");
    let pendingForm = null;
    let pendingSubmitter = null;

    document.querySelectorAll("[data-db-confirm-trigger]").forEach((trigger) => {
        trigger.addEventListener("click", (event) => {
            const form = trigger.closest("form");
            if (!form) return;
            event.preventDefault();
            pendingForm = form;
            pendingSubmitter = trigger;
            title.textContent = form.dataset.confirmTitle || "Permanently delete these records?";
            detail.textContent = form.dataset.confirmDetail || "This action cannot be undone.";
            counts.replaceChildren();
            [
                ["Activity records", form.dataset.confirmActivity],
                ["Security events", form.dataset.confirmSecurity],
            ].forEach(([label, value]) => {
                if (value === undefined) return;
                const item = document.createElement("li");
                const number = Number.parseInt(value, 10);
                item.textContent = `${label}: ${Number.isFinite(number) ? number.toLocaleString() : "0"}`;
                counts.append(item);
            });
            dialog.showModal();
        });
    });

    dialog.querySelector("[data-db-confirm-cancel]")?.addEventListener("click", () => {
        dialog.close();
        pendingForm = null;
        pendingSubmitter = null;
    });

    accept?.addEventListener("click", () => {
        if (!pendingForm || !pendingSubmitter) return;
        let confirmation = pendingForm.querySelector('input[name="confirmed"]');
        if (!confirmation) {
            confirmation = document.createElement("input");
            confirmation.type = "hidden";
            confirmation.name = "confirmed";
            pendingForm.append(confirmation);
        }
        confirmation.value = "1";
        dialog.close();
        pendingForm.requestSubmit(pendingSubmitter);
    });
})();
