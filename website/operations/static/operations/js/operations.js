(function () {
    "use strict";

    function each(selector, callback, root) {
        (root || document).querySelectorAll(selector).forEach(callback);
    }

    var activeAdminToast = null;
    var contentPageHashHandlerBound = false;
    var taskFilterGlobalHandlersBound = false;
    var calendarPopstateBound = false;
    var activeCalendarPopstate = null;
    var modalEscapeHandlerBound = false;
    var activeModalTrigger = null;

    function showAdminToast(message, isError, actionHref, actionLabel) {
        var text = String(message || "").trim();
        var region = document.querySelector(".toast-region");
        if (!text || !region) return;

        if (!activeAdminToast || !activeAdminToast.isConnected) {
            var toast = document.createElement("div");
            toast.className = "toast admin-operation-toast";
            toast.dataset.persistent = "true";
            var content = document.createElement("span");
            content.className = "toast-content";
            var dismiss = document.createElement("button");
            dismiss.className = "toast-dismiss";
            dismiss.type = "button";
            dismiss.setAttribute("aria-label", "Dismiss notification");
            dismiss.title = "Dismiss";
            dismiss.textContent = "×";
            dismiss.addEventListener("click", function () {
                if (toast.dataset.dismissed === "true") return;
                toast.dataset.dismissed = "true";
                toast.classList.add("is-dismissing");
                window.setTimeout(function () {
                    toast.remove();
                    if (activeAdminToast === toast) activeAdminToast = null;
                }, 220);
            });
            toast.append(content, dismiss);
            region.appendChild(toast);
            activeAdminToast = toast;
        }

        activeAdminToast.classList.remove("is-dismissing");
        activeAdminToast.dataset.dismissed = "false";
        activeAdminToast.querySelector(".toast-content").textContent = text;
        activeAdminToast.classList.toggle("is-error", Boolean(isError));
        var existingAction = activeAdminToast.querySelector(".toast-action");
        if (existingAction) existingAction.remove();
        if (actionHref) {
            var action = document.createElement("a");
            action.className = "toast-action";
            action.href = actionHref;
            action.target = "_blank";
            action.rel = "noopener";
            action.textContent = actionLabel || "Open link";
            activeAdminToast.appendChild(action);
        }
    }

    function initNavigation() {
        var toggle = document.querySelector("[data-mobile-nav-toggle]");
        var nav = document.querySelector(".site-nav");
        if (toggle && nav) {
            toggle.addEventListener("click", function () {
                var open = nav.classList.toggle("is-open");
                toggle.setAttribute("aria-expanded", String(open));
            });
            each(".site-nav a", function (link) {
                link.addEventListener("click", function () {
                    nav.classList.remove("is-open");
                    toggle.setAttribute("aria-expanded", "false");
                });
            });
        }

        var adminToggle = document.querySelector("[data-admin-menu-toggle]");
        var sidebar = document.querySelector("[data-admin-sidebar]");
        if (adminToggle && sidebar) {
            adminToggle.addEventListener("click", function () {
                var open = sidebar.classList.toggle("is-open");
                adminToggle.setAttribute("aria-expanded", String(open));
            });
            each(".admin-nav a", function (link) {
                link.addEventListener("click", function () {
                    sidebar.classList.remove("is-open");
                    adminToggle.setAttribute("aria-expanded", "false");
                });
            });
        }
    }

    function selectContentPage(nav, hash) {
        if (!nav) return;
        var links = Array.prototype.slice.call(nav.querySelectorAll("[data-content-page-link]"));
        var matchingHash = links.some(function (link) { return link.getAttribute("href") === hash; })
            ? hash
            : "";
        if (!matchingHash && hash) {
            var target = document.getElementById(hash.slice(1));
            var panel = target && target.closest(".content-page-panel");
            if (panel) matchingHash = "#" + panel.id;
        }
        links.forEach(function (link) {
            if (link.getAttribute("href") === matchingHash) {
                link.setAttribute("aria-current", "location");
            } else {
                link.removeAttribute("aria-current");
            }
        });
    }

    function initContentPageNavigation(options) {
        options = options || {};
        var nav = document.querySelector(".admin-view.is-active .content-page-nav");
        if (!nav) return;
        var links = Array.prototype.slice.call(nav.querySelectorAll("[data-content-page-link]"));

        links.forEach(function (link) {
            link.addEventListener("click", function () {
                selectContentPage(nav, link.getAttribute("href"));
            });
        });
        if (!contentPageHashHandlerBound) {
            contentPageHashHandlerBound = true;
            window.addEventListener("hashchange", function () {
                selectContentPage(document.querySelector(".admin-view.is-active .content-page-nav"), window.location.hash);
            });
        }
        var pageFromQuery = new URL(window.location.href).searchParams.get("content_page_focus");
        var queryHash = pageFromQuery ? "#publish-page-" + pageFromQuery : "";
        selectContentPage(nav, window.location.hash || queryHash || (links[0] && links[0].getAttribute("href")));

        var view = nav.closest(".admin-view");
        var focusPage = view && (view.dataset.contentPageError || view.dataset.contentPageFocus);
        if (!options.skipAutoFocus && focusPage && links.some(function (link) { return link.getAttribute("href") === "#publish-page-" + focusPage; })) {
            var target = document.getElementById("publish-page-" + focusPage);
            if (target) {
                selectContentPage(nav, "#publish-page-" + focusPage);
                window.requestAnimationFrame(function () {
                    target.scrollIntoView({ block: "start", behavior: "auto" });
                });
            }
        }
    }

    function initTaskAssociationSelectors() {
        each("[data-task-association-form]", function (form) {
            var lead = form.querySelector("[data-task-lead-select]");
            var client = form.querySelector("[data-task-client-select]");
            var project = form.querySelector("[data-task-project-select]");
            var milestone = form.querySelector("[data-task-milestone-select]");
            var projectField = form.querySelector("[data-task-project-field]");
            if (!lead || !client || !project || !projectField) return;

            var projectClientMap = {};
            try {
                projectClientMap = JSON.parse(project.dataset.projectClientMap || "{}");
            } catch (error) {
                projectClientMap = {};
            }
            var availableProjects = Array.prototype.slice.call(project.options)
                .filter(function (option) { return option.value !== ""; })
                .map(function (option) { return option.cloneNode(true); });

            function updateProjectChoices(clientId, leadSelected, keepSelection) {
                var previousValue = keepSelection ? project.value : "";
                while (project.options.length) project.remove(0);

                var placeholder = document.createElement("option");
                placeholder.value = "";
                placeholder.selected = true;

                if (leadSelected) {
                    placeholder.textContent = "Lead selected";
                    placeholder.disabled = true;
                    project.appendChild(placeholder);
                    project.disabled = true;
                    return;
                }

                if (!clientId) {
                    placeholder.textContent = "Select a client first";
                    placeholder.disabled = true;
                    project.appendChild(placeholder);
                    project.disabled = true;
                    return;
                }

                var matchingProjects = availableProjects.filter(function (option) {
                    return projectClientMap[option.value] === clientId;
                });
                if (!matchingProjects.length) {
                    placeholder.textContent = "Project not yet created";
                    project.appendChild(placeholder);
                    project.disabled = false;
                    return;
                }

                placeholder.textContent = "Select a project (optional)";
                project.appendChild(placeholder);
                matchingProjects.forEach(function (option) {
                    project.appendChild(option.cloneNode(true));
                });
                project.disabled = false;
                if (previousValue && matchingProjects.some(function (option) {
                    return option.value === previousValue;
                })) {
                    project.value = previousValue;
                }
            }

            function sync() {
                var leadSelected = Boolean(lead.value);
                if (leadSelected) {
                    client.value = "";
                    if (milestone) milestone.value = "";
                    projectField.hidden = false;
                } else {
                    projectField.hidden = !client.value;
                }
                lead.disabled = Boolean(client.value);
                client.disabled = leadSelected;
                updateProjectChoices(client.value, leadSelected, !leadSelected);
                if (milestone) {
                    milestone.disabled = leadSelected || !project.value;
                    if (milestone.disabled) milestone.value = "";
                }
            }

            lead.addEventListener("change", sync);
            client.addEventListener("change", sync);
            project.addEventListener("change", function () {
                if (milestone) milestone.value = "";
                sync();
            });
            sync();
        });
    }

    function initTaskSearch() {
        each("[data-task-filter-form]", function (form) {
            var input = form.querySelector("[data-task-search]");
            var clearButton = form.querySelector("[data-task-search-clear]");
            if (!input || !clearButton) return;

            function syncClearButton() {
                clearButton.hidden = !input.value;
            }

            input.addEventListener("input", syncClearButton);
            clearButton.addEventListener("click", function () {
                input.value = "";
                syncClearButton();

                var focusMarker = document.createElement("input");
                focusMarker.type = "hidden";
                focusMarker.name = "focus_search";
                focusMarker.value = "1";
                form.appendChild(focusMarker);

                if (typeof form.requestSubmit === "function") {
                    form.requestSubmit();
                } else {
                    form.submit();
                }
            });

            var url = new URL(window.location.href);
            if (url.searchParams.get("focus_search") === "1") {
                url.searchParams.delete("focus_search");
                window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
                input.focus();
            }

            syncClearButton();
        });
    }

    function positionTaskFilterDropdown(dropdown) {
        var summary = dropdown && dropdown.querySelector("summary");
        var options = dropdown && dropdown.querySelector(".task-filter-options");
        if (!summary || !options) return;
        var rect = summary.getBoundingClientRect();
        var edgeGap = 12;
        var maxHeight = 320;
        var spaceBelow = Math.max(0, window.innerHeight - rect.bottom - edgeGap);
        var spaceAbove = Math.max(0, rect.top - edgeGap);
        var openAbove = spaceBelow < Math.min(maxHeight, 220) && spaceAbove > spaceBelow;
        var availableSpace = openAbove ? spaceAbove : spaceBelow;
        dropdown.classList.toggle("is-open-upward", openAbove);
        options.style.maxHeight = Math.max(72, Math.min(maxHeight, availableSpace)) + "px";
    }

    function initTaskFilterDropdowns() {
        each("[data-task-filter-form]", function (form) {
            var dropdowns = Array.prototype.slice.call(form.querySelectorAll("[data-task-filter-select]"));
            if (!dropdowns.length) return;

            function closeDropdown(dropdown) {
                dropdown.open = false;
            }

            dropdowns.forEach(function (dropdown) {
                var options = dropdown.querySelector(".task-filter-options");
                var selectedValue = dropdown.querySelector("[data-task-filter-value]");

                dropdown.addEventListener("toggle", function () {
                    if (dropdown.open) {
                        dropdowns.forEach(function (otherDropdown) {
                            if (otherDropdown !== dropdown && otherDropdown.open) {
                                closeDropdown(otherDropdown);
                            }
                        });
                        positionTaskFilterDropdown(dropdown);
                    } else {
                        dropdown.classList.remove("is-open-upward");
                        if (options) options.style.removeProperty("max-height");
                    }
                });

                dropdown.addEventListener("change", function (event) {
                    var radio = event.target;
                    if (!radio.matches || !radio.matches(".task-filter-radio") || !radio.checked) return;
                    var option = radio.closest(".task-filter-option");
                    var optionText = option && option.querySelector(".task-filter-option-text");
                    if (selectedValue && optionText) selectedValue.textContent = optionText.textContent.trim();
                    dropdown.querySelectorAll(".task-filter-option.is-selected").forEach(function (selectedOption) {
                        selectedOption.classList.remove("is-selected");
                    });
                    if (option) option.classList.add("is-selected");
                    positionTaskFilterDropdown(dropdown);
                });

                dropdown.addEventListener("focusout", function (event) {
                    if (event.relatedTarget && !dropdown.contains(event.relatedTarget)) {
                        closeDropdown(dropdown);
                    }
                });
            });

        });
        if (taskFilterGlobalHandlersBound) return;
        taskFilterGlobalHandlersBound = true;
        document.addEventListener("click", function (event) {
            document.querySelectorAll("[data-task-filter-select][open]").forEach(function (dropdown) {
                if (!dropdown.contains(event.target)) dropdown.open = false;
            });
        });
        document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            var dropdowns = Array.from(document.querySelectorAll("[data-task-filter-select][open]"));
            var openDropdown = dropdowns.find(function (dropdown) { return dropdown.contains(document.activeElement); })
                || dropdowns[0];
            if (!openDropdown) return;
            event.preventDefault();
            openDropdown.open = false;
            var summary = openDropdown.querySelector("summary");
            if (summary) summary.focus({ preventScroll: true });
        });
        var repositionOpenDropdowns = function () {
            document.querySelectorAll("[data-task-filter-select][open]").forEach(positionTaskFilterDropdown);
        };
        window.addEventListener("resize", repositionOpenDropdowns);
        window.addEventListener("scroll", repositionOpenDropdowns, true);
    }

    function initLeadDetailTabs() {
        each(".leads-split .record-detail", function (record, recordIndex) {
            var tabList = record.querySelector(".detail-tabs");
            var columns = record.querySelector(".detail-columns");
            if (!tabList || !columns || columns.children.length < 2) {
                return;
            }

            var tabNodes = Array.prototype.slice.call(tabList.querySelectorAll(".detail-tab"));
            if (tabNodes.length !== 2) {
                return;
            }

            var recordId = String(recordIndex + 1);
            var priorityForm = record.querySelector('form[action*="lead-priority"]');
            var priorityAction = priorityForm ? priorityForm.getAttribute("action") : "";
            var recordMatch = priorityAction.match(/dashboard\/leads\/([^/]+)\/priority/);
            if (recordMatch) {
                recordId = recordMatch[1];
            }

            var detailsPanel = columns.children[0];
            var notesPanel = columns.children[1];
            var panelDefinitions = [
                { key: "details", label: "Details", panel: detailsPanel },
                { key: "notes", label: "Notes & tasks", panel: notesPanel }
            ];

            tabList.setAttribute("role", "tablist");
            tabList.setAttribute("aria-label", "Lead record sections");
            columns.classList.add("lead-detail-tabbed-columns");
            detailsPanel.classList.add("lead-tab-panel", "lead-details-panel");
            notesPanel.classList.add("lead-tab-panel", "lead-notes-panel");

            var tabs = panelDefinitions.map(function (definition, index) {
                var original = tabNodes[index];
                var button = document.createElement("button");
                button.type = "button";
                button.className = original.className;
                button.textContent = definition.label;
                button.setAttribute("role", "tab");
                button.id = "lead-" + recordId + "-" + definition.key + "-tab";
                button.setAttribute("aria-controls", "lead-" + recordId + "-" + definition.key + "-panel");
                button.setAttribute("data-lead-tab", definition.key);
                tabList.replaceChild(button, original);

                definition.panel.id = "lead-" + recordId + "-" + definition.key + "-panel";
                definition.panel.setAttribute("role", "tabpanel");
                definition.panel.setAttribute("aria-labelledby", button.id);
                definition.panel.setAttribute("tabindex", "0");
                return button;
            });

            detailsPanel.classList.add("lead-facts-panel");
            notesPanel.classList.add("lead-notes-and-tasks-panel");
            var attachments = record.querySelector(".lead-attachments");
            if (attachments) {
                detailsPanel.appendChild(attachments);
            }

            function writeTabToAddress(tabKey, includeTaskHash) {
                if (!window.history || !window.history.replaceState || !window.URL) {
                    return;
                }
                var url = new URL(window.location.href);
                if (tabKey === "details") {
                    url.searchParams.delete("lead_tab");
                    if (url.hash === "#follow-up") {
                        url.hash = "";
                    }
                } else {
                    url.searchParams.set("lead_tab", tabKey);
                }
                if (includeTaskHash) {
                    url.hash = "follow-up";
                }
                window.history.replaceState(window.history.state, "", url.toString());
            }

            function activateTab(tabKey, updateAddress, includeTaskHash) {
                panelDefinitions.forEach(function (definition, index) {
                    var selected = definition.key === tabKey;
                    tabs[index].classList.toggle("is-current", selected);
                    tabs[index].setAttribute("aria-selected", String(selected));
                    tabs[index].setAttribute("tabindex", selected ? "0" : "-1");
                    definition.panel.hidden = !selected;
                });
                if (updateAddress) {
                    writeTabToAddress(tabKey, includeTaskHash);
                }
            }

            var requestedTab = new URLSearchParams(window.location.search).get("lead_tab");
            if (requestedTab !== "notes" && requestedTab !== "details") {
                requestedTab = "";
            }
            if (!requestedTab && document.body) {
                var serverRequestedTab = document.body.getAttribute("data-lead-detail-tab");
                if (serverRequestedTab === "notes" || serverRequestedTab === "details") {
                    requestedTab = serverRequestedTab;
                }
            }
            if (!requestedTab && window.location.hash === "#follow-up") {
                requestedTab = "notes";
            }
            activateTab(requestedTab === "notes" ? "notes" : "details", false, false);

            tabs.forEach(function (tab, index) {
                tab.addEventListener("click", function () {
                    activateTab(tab.getAttribute("data-lead-tab"), true, false);
                });
                tab.addEventListener("keydown", function (event) {
                    var nextIndex = index;
                    if (event.key === "ArrowRight") {
                        nextIndex = (index + 1) % tabs.length;
                    } else if (event.key === "ArrowLeft") {
                        nextIndex = (index + tabs.length - 1) % tabs.length;
                    } else if (event.key === "Home") {
                        nextIndex = 0;
                    } else if (event.key === "End") {
                        nextIndex = tabs.length - 1;
                    } else {
                        return;
                    }
                    event.preventDefault();
                    tabs[nextIndex].focus();
                    activateTab(tabs[nextIndex].getAttribute("data-lead-tab"), true, false);
                });
            });

            var addTaskShortcut = record.querySelector('a[href="#follow-up"]');
            if (addTaskShortcut) {
                addTaskShortcut.addEventListener("click", function (event) {
                    event.preventDefault();
                    activateTab("notes", true, true);
                    var taskTitle = record.querySelector('form[action*="lead-followup"] input[name$="-title"]');
                    if (taskTitle) {
                        taskTitle.focus();
                    }
                });
            }
        });
    }

    function initGoogleReviewsCarousel() {
        each("[data-google-reviews-carousel]", function (carousel) {
            var viewport = carousel.querySelector("[data-google-reviews-viewport]");
            var cards = Array.prototype.slice.call(carousel.querySelectorAll("[data-google-review-card]"));
            var previous = carousel.querySelector("[data-google-reviews-prev]");
            var next = carousel.querySelector("[data-google-reviews-next]");
            var dots = carousel.querySelector("[data-google-reviews-dots]");
            if (!viewport || !cards.length || !previous || !next || !dots) {
                return;
            }

            var page = 0;
            var previousPerView = 0;

            function cardsPerView() {
                if (window.innerWidth <= 560) {
                    return 1;
                }
                if (window.innerWidth <= 860) {
                    return 2;
                }
                return 3;
            }

            function pageCount() {
                return Math.max(1, Math.ceil(cards.length / cardsPerView()));
            }

            function pageStart(pageIndex) {
                return Math.max(
                    0,
                    Math.min(cards.length - cardsPerView(), pageIndex * cardsPerView())
                );
            }

            function reducedMotion() {
                return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            }

            function nearestCardIndex() {
                var closest = 0;
                var closestDistance = Infinity;
                cards.forEach(function (card, index) {
                    var distance = Math.abs(card.offsetLeft - viewport.scrollLeft);
                    if (distance < closestDistance) {
                        closest = index;
                        closestDistance = distance;
                    }
                });
                return closest;
            }

            function setPage(nextPage, shouldScroll) {
                var maxPage = pageCount() - 1;
                page = Math.max(0, Math.min(maxPage, nextPage));
                var target = cards[pageStart(page)];
                if (target && shouldScroll !== false) {
                    var left = target.offsetLeft;
                    if (typeof viewport.scrollTo === "function") {
                        viewport.scrollTo({
                            left: left,
                            behavior: reducedMotion() ? "auto" : "smooth",
                        });
                    } else {
                        viewport.scrollLeft = left;
                    }
                }
                updateState(page);
            }

            function buildDots() {
                dots.innerHTML = "";
                for (var index = 0; index < pageCount(); index += 1) {
                    var dot = document.createElement("button");
                    dot.type = "button";
                    dot.className = "google-reviews-dot";
                    dot.setAttribute("role", "tab");
                    dot.setAttribute("aria-label", "Show Google review group " + (index + 1));
                    dot.dataset.googleReviewsDot = String(index);
                    dot.addEventListener("click", function () {
                        setPage(Number(this.dataset.googleReviewsDot));
                    });
                    dots.appendChild(dot);
                }
            }

            function nearestPage() {
                var currentScrollLeft = viewport.scrollLeft;
                var closestPage = 0;
                var closestDistance = Infinity;
                for (var index = 0; index < pageCount(); index += 1) {
                    var target = cards[pageStart(index)];
                    var distance = Math.abs(target.offsetLeft - currentScrollLeft);
                    if (distance < closestDistance) {
                        closestPage = index;
                        closestDistance = distance;
                    }
                }
                return closestPage;
            }

            function updateState(forcePage) {
                page = typeof forcePage === "number" ? forcePage : nearestPage();
                previous.disabled = page === 0;
                next.disabled = page >= pageCount() - 1;
                Array.prototype.slice.call(dots.children).forEach(function (dot, index) {
                    var selected = index === page;
                    dot.setAttribute("aria-selected", String(selected));
                    dot.tabIndex = selected ? 0 : -1;
                });
            }

            function rebuild() {
                var currentPerView = cardsPerView();
                if (currentPerView !== previousPerView) {
                    previousPerView = currentPerView;
                    buildDots();
                    setPage(Math.min(page, pageCount() - 1), false);
                } else {
                    updateState();
                }
            }

            previous.addEventListener("click", function () {
                setPage(page - 1);
            });
            next.addEventListener("click", function () {
                setPage(page + 1);
            });
            viewport.addEventListener("scroll", function () {
                window.requestAnimationFrame(function () {
                    updateState();
                });
            });
            viewport.addEventListener("keydown", function (event) {
                if (event.key === "ArrowLeft") {
                    event.preventDefault();
                    setPage(page - 1);
                } else if (event.key === "ArrowRight") {
                    event.preventDefault();
                    setPage(page + 1);
                } else if (event.key === "Home") {
                    event.preventDefault();
                    setPage(0);
                } else if (event.key === "End") {
                    event.preventDefault();
                    setPage(pageCount() - 1);
                }
            });
            window.addEventListener("resize", rebuild);
            rebuild();
        });
    }
    function initProjectFilters() {
        var buttons = document.querySelectorAll("[data-project-filter]");
        var cards = document.querySelectorAll("[data-project-card]");
        if (!buttons.length || !cards.length) {
            return;
        }
        buttons.forEach(function (button) {
            button.addEventListener("click", function () {
                var filter = button.dataset.projectFilter;
                buttons.forEach(function (item) {
                    item.classList.toggle("is-selected", item === button);
                });
                cards.forEach(function (card) {
                    card.hidden = filter !== "all" && card.dataset.projectType !== filter;
                });
            });
        });
    }

    function initFileInputs() {
        each('input[type="file"]', function (input) {
            input.addEventListener("change", function () {
                var label = input.closest(".file-drop");
                if (!label) {
                    return;
                }
                var title = label.querySelector("strong");
                if (title && input.files.length) {
                    title.textContent = input.files.length === 1
                        ? input.files[0].name
                        : input.files.length + " files selected";
                }
            });
        });
    }

    var calendarPickerEventsBound = false;
    var calendarPickerSequence = 0;

    function padCalendarNumber(value) {
        var text = String(value);
        return text.length < 2 ? "0" + text : text;
    }

    function makeCalendarDate(year, monthIndex, day) {
        var date = new Date();
        date.setHours(0, 0, 0, 0);
        date.setFullYear(year, monthIndex, day);
        return date;
    }

    function parseCalendarDate(value) {
        var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
        if (!match) {
            return null;
        }
        var date = makeCalendarDate(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
        return date.getFullYear() === Number(match[1]) &&
            date.getMonth() === Number(match[2]) - 1 &&
            date.getDate() === Number(match[3]) ? date : null;
    }

    function calendarDateValue(date) {
        return date.getFullYear() + "-" + padCalendarNumber(date.getMonth() + 1) + "-" + padCalendarNumber(date.getDate());
    }

    function calendarDateLabel(value) {
        var date = parseCalendarDate(value);
        return date ? date.toLocaleDateString("en-US", {month: "short", day: "2-digit", year: "numeric"}) : "Choose a date";
    }

    function parseCalendarTime(value) {
        var match = /^(\d{2}):(\d{2})$/.exec(value || "");
        if (!match) {
            return null;
        }
        var hour24 = Number(match[1]);
        var minute = Number(match[2]);
        if (hour24 > 23 || minute > 59) {
            return null;
        }
        return {
            hour: hour24 % 12 || 12,
            minute: minute,
            period: hour24 >= 12 ? "PM" : "AM",
        };
    }

    function calendarTimeValue(time) {
        var hour = time.hour % 12;
        if (time.period === "PM") {
            hour += 12;
        }
        return padCalendarNumber(hour) + ":" + padCalendarNumber(time.minute);
    }

    function calendarTimeLabel(value) {
        var time = parseCalendarTime(value);
        return time ? padCalendarNumber(time.hour) + ":" + padCalendarNumber(time.minute) + " " + time.period : "Select time";
    }

    function emitCalendarPickerChange(input) {
        ["input", "change"].forEach(function (eventName) {
            var event;
            if (typeof Event === "function") {
                event = new Event(eventName, {bubbles: true});
            } else {
                event = document.createEvent("Event");
                event.initEvent(eventName, true, true);
            }
            input.dispatchEvent(event);
        });
    }

    function createCalendarPickerButton(text, className, label) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = className;
        button.textContent = text;
        if (label) {
            button.setAttribute("aria-label", label);
        }
        return button;
    }

    function closeCalendarPicker(picker) {
        picker.popover.hidden = true;
        picker.popover.classList.remove("is-open");
        picker.shell.classList.remove("is-open");
        picker.trigger.setAttribute("aria-expanded", "false");
        picker.popover.style.top = "";
        picker.popover.style.left = "";
        picker.popover.style.width = "";
    }

    function closeAllCalendarPickers(except) {
        each(".calendar-picker.is-open", function (shell) {
            if (except && shell === except) {
                return;
            }
            var popover = shell._calendarPicker && shell._calendarPicker.popover;
            if (popover) {
                closeCalendarPicker(shell._calendarPicker);
            }
        });
    }

    function disposeCalendarPickers(root) {
        each(".calendar-picker", function (shell) {
            var picker = shell._calendarPicker;
            if (!picker) {
                return;
            }
            closeCalendarPicker(picker);
            if (picker.popover.parentNode) {
                picker.popover.parentNode.removeChild(picker.popover);
            }
            shell._calendarPicker = null;
        }, root);
    }

    function positionCalendarPicker(picker) {
        if (!picker || picker.popover.hidden) {
            return;
        }
        var triggerBounds = picker.trigger.getBoundingClientRect();
        var viewportWidth = document.documentElement.clientWidth || window.innerWidth;
        var viewportHeight = window.innerHeight;
        var width = Math.min(360, Math.max(240, viewportWidth - 24));
        var left = Math.min(Math.max(12, triggerBounds.left), viewportWidth - width - 12);
        picker.popover.style.width = width + "px";
        var height = picker.popover.offsetHeight;
        var top = triggerBounds.bottom + 8;
        if (viewportHeight - triggerBounds.bottom < height + 12 && triggerBounds.top > height + 12) {
            top = triggerBounds.top - height - 8;
        }
        top = Math.min(Math.max(12, top), Math.max(12, viewportHeight - height - 12));
        picker.popover.style.left = left + "px";
        picker.popover.style.top = top + "px";
    }

    function bindCalendarPickerDocumentEvents() {
        if (calendarPickerEventsBound) {
            return;
        }
        document.addEventListener("click", function (event) {
            var target = event.target;
            if (target && target.closest && (target.closest(".calendar-picker") || target.closest(".calendar-picker-popover"))) {
                return;
            }
            closeAllCalendarPickers();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                closeAllCalendarPickers();
            }
        });
        window.addEventListener("resize", function () {
            each(".calendar-picker.is-open", function (shell) {
                if (shell._calendarPicker) {
                    positionCalendarPicker(shell._calendarPicker);
                }
            });
        });
        window.addEventListener("scroll", function () {
            each(".calendar-picker.is-open", function (shell) {
                if (shell._calendarPicker) {
                    positionCalendarPicker(shell._calendarPicker);
                }
            });
        }, true);
        calendarPickerEventsBound = true;
    }

    function createCalendarPicker(input, type) {
        if (!input || input.dataset.calendarPickerInitialized || !input.parentNode || !document.body) {
            return null;
        }

        var shell = document.createElement("div");
        shell.className = "calendar-picker calendar-picker-" + type;
        input.parentNode.insertBefore(shell, input);
        shell.appendChild(input);
        input.dataset.calendarPickerInitialized = "true";
        input.classList.add("calendar-native-picker-input");
        input.tabIndex = -1;
        input.setAttribute("aria-hidden", "true");

        var trigger = createCalendarPickerButton("", "calendar-picker-trigger", type === "date" ? "Choose date" : "Choose time");
        trigger.id = "calendar-picker-trigger-" + (++calendarPickerSequence);
        var icon = document.createElement("span");
        icon.className = "calendar-picker-icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = "";
        var value = document.createElement("span");
        value.className = "calendar-picker-value";
        var caret = document.createElement("span");
        caret.className = "calendar-picker-caret";
        caret.setAttribute("aria-hidden", "true");
        trigger.appendChild(icon);
        trigger.appendChild(value);
        trigger.appendChild(caret);
        shell.appendChild(trigger);

        var popover = document.createElement("div");
        popover.className = "calendar-picker-popover";
        popover.hidden = true;
        popover.id = "calendar-picker-popover-" + calendarPickerSequence;
        popover.setAttribute("role", "dialog");
        popover.setAttribute("aria-label", type === "date" ? "Choose a calendar date" : "Choose a time");
        trigger.setAttribute("aria-haspopup", "dialog");
        trigger.setAttribute("aria-controls", popover.id);
        trigger.setAttribute("aria-expanded", "false");
        document.body.appendChild(popover);

        var picker = {input: input, shell: shell, trigger: trigger, value: value, popover: popover};
        shell._calendarPicker = picker;
        bindCalendarPickerDocumentEvents();
        return picker;
    }

    function openCalendarPicker(picker) {
        closeAllCalendarPickers(picker.shell);
        picker.shell.classList.add("is-open");
        picker.popover.hidden = false;
        picker.popover.classList.add("is-open");
        picker.trigger.setAttribute("aria-expanded", "true");
        positionCalendarPicker(picker);
    }

    function initCalendarDatePicker(input) {
        var picker = createCalendarPicker(input, "date");
        if (!picker) {
            return;
        }

        var state = {year: new Date().getFullYear(), month: new Date().getMonth()};

        function syncTrigger() {
            var hasValue = Boolean(parseCalendarDate(input.value));
            picker.value.textContent = calendarDateLabel(input.value);
            picker.value.classList.toggle("is-placeholder", !hasValue);
            picker.trigger.setAttribute("aria-label", hasValue ? "Change date, " + calendarDateLabel(input.value) : "Choose date");
        }

        function chooseDate(date) {
            input.value = calendarDateValue(date);
            syncTrigger();
            emitCalendarPickerChange(input);
            closeCalendarPicker(picker);
            picker.trigger.focus();
        }

        function render() {
            while (picker.popover.firstChild) {
                picker.popover.removeChild(picker.popover.firstChild);
            }

            var header = document.createElement("div");
            header.className = "calendar-picker-date-header";
            var title = document.createElement("strong");
            title.className = "calendar-picker-title";
            title.textContent = makeCalendarDate(state.year, state.month, 1).toLocaleDateString("en-US", {month: "long", year: "numeric"});
            var navigation = document.createElement("div");
            navigation.className = "calendar-picker-navigation";
            var previous = createCalendarPickerButton("", "calendar-picker-nav is-previous", "Previous month");
            var next = createCalendarPickerButton("", "calendar-picker-nav is-next", "Next month");
            previous.addEventListener("click", function (event) {
                event.preventDefault();
                event.stopPropagation();
                state.month -= 1;
                if (state.month < 0) {
                    state.month = 11;
                    state.year -= 1;
                }
                render();
            });
            next.addEventListener("click", function (event) {
                event.preventDefault();
                event.stopPropagation();
                state.month += 1;
                if (state.month > 11) {
                    state.month = 0;
                    state.year += 1;
                }
                render();
            });
            navigation.appendChild(previous);
            navigation.appendChild(next);
            header.appendChild(title);
            header.appendChild(navigation);

            var weekdays = document.createElement("div");
            weekdays.className = "calendar-picker-weekdays";
            ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].forEach(function (weekday) {
                var weekdayLabel = document.createElement("span");
                weekdayLabel.textContent = weekday;
                weekdays.appendChild(weekdayLabel);
            });

            var grid = document.createElement("div");
            grid.className = "calendar-picker-date-grid";
            grid.setAttribute("role", "grid");
            var firstDay = makeCalendarDate(state.year, state.month, 1).getDay();
            var selectedValue = input.value;
            var todayValue = calendarDateValue(new Date());
            for (var index = 0; index < 42; index += 1) {
                var date = makeCalendarDate(state.year, state.month, index - firstDay + 1);
                var dateValue = calendarDateValue(date);
                var day = createCalendarPickerButton(String(date.getDate()), "calendar-picker-day", date.toLocaleDateString("en-US", {month: "long", day: "numeric", year: "numeric"}));
                day.dataset.date = dateValue;
                day.setAttribute("role", "gridcell");
                if (date.getMonth() !== state.month || date.getFullYear() !== state.year) {
                    day.classList.add("is-outside");
                }
                if (dateValue === selectedValue) {
                    day.classList.add("is-selected");
                    day.setAttribute("aria-pressed", "true");
                }
                if (dateValue === todayValue) {
                    day.classList.add("is-today");
                }
                day.addEventListener("click", function (event) {
                    event.preventDefault();
                    chooseDate(parseCalendarDate(event.currentTarget.dataset.date));
                });
                grid.appendChild(day);
            }

            var footer = document.createElement("div");
            footer.className = "calendar-picker-footer";
            var clear = createCalendarPickerButton("Clear", "calendar-picker-action", "Clear date");
            var today = createCalendarPickerButton("Today", "calendar-picker-action is-primary", "Choose today");
            clear.addEventListener("click", function (event) {
                event.preventDefault();
                input.value = "";
                syncTrigger();
                emitCalendarPickerChange(input);
                closeCalendarPicker(picker);
                picker.trigger.focus();
            });
            today.addEventListener("click", function (event) {
                event.preventDefault();
                var now = new Date();
                chooseDate(makeCalendarDate(now.getFullYear(), now.getMonth(), now.getDate()));
            });
            footer.appendChild(clear);
            footer.appendChild(today);
            picker.popover.appendChild(header);
            picker.popover.appendChild(weekdays);
            picker.popover.appendChild(grid);
            picker.popover.appendChild(footer);
        }

        picker.trigger.addEventListener("click", function (event) {
            event.preventDefault();
            if (!picker.popover.hidden) {
                closeCalendarPicker(picker);
                return;
            }
            var current = parseCalendarDate(input.value) || new Date();
            state.year = current.getFullYear();
            state.month = current.getMonth();
            render();
            openCalendarPicker(picker);
        });
        input.addEventListener("input", syncTrigger);
        input.addEventListener("change", syncTrigger);
        syncTrigger();
    }

    function initCalendarTimePicker(input) {
        var picker = createCalendarPicker(input, "time");
        if (!picker) {
            return;
        }

        var state = {hour: 9, minute: 0, period: "AM"};
        var columns = document.createElement("div");
        columns.className = "calendar-time-columns";
        picker.popover.appendChild(columns);
        var heading = document.createElement("div");
        heading.className = "calendar-picker-time-heading";
        heading.textContent = "Select time";
        picker.popover.insertBefore(heading, columns);
        var footer = document.createElement("div");
        footer.className = "calendar-picker-footer";
        var cancel = createCalendarPickerButton("Cancel", "calendar-picker-action", "Cancel time selection");
        var done = createCalendarPickerButton("Done", "calendar-picker-action is-primary", "Use selected time");
        footer.appendChild(cancel);
        footer.appendChild(done);
        picker.popover.appendChild(footer);

        function syncTrigger() {
            var hasValue = Boolean(parseCalendarTime(input.value));
            picker.value.textContent = calendarTimeLabel(input.value);
            picker.value.classList.toggle("is-placeholder", !hasValue);
            picker.trigger.setAttribute("aria-label", hasValue ? "Change time, " + calendarTimeLabel(input.value) : "Choose time");
        }

        function renderOptions() {
            var scrollPositions = Array.prototype.map.call(
                columns.querySelectorAll(".calendar-time-options"),
                function (options) { return options.scrollTop; }
            );
            while (columns.firstChild) {
                columns.removeChild(columns.firstChild);
            }
            [
                {key: "hour", label: "Hour", values: Array.from({length: 12}, function (_, index) { return index + 1; })},
                {key: "minute", label: "Minute", values: Array.from({length: 60}, function (_, index) { return index; })},
                {key: "period", label: "", values: ["AM", "PM"]},
            ].forEach(function (definition) {
                var column = document.createElement("div");
                column.className = "calendar-time-column";
                var label = document.createElement("span");
                label.className = "calendar-time-column-label";
                label.textContent = definition.label;
                if (definition.label) {
                    column.appendChild(label);
                }
                var options = document.createElement("div");
                options.className = "calendar-time-options";
                options.setAttribute("role", "listbox");
                options.setAttribute("aria-label", definition.label || "AM or PM");
                definition.values.forEach(function (value) {
                    var option = createCalendarPickerButton(definition.key === "minute" ? padCalendarNumber(value) : String(value), "calendar-time-option", definition.label ? definition.label + " " + value : String(value));
                    var selected = String(state[definition.key]) === String(value);
                    option.classList.toggle("is-selected", selected);
                    option.setAttribute("aria-selected", String(selected));
                    option.setAttribute("role", "option");
                    option.addEventListener("click", function (event) {
                        event.preventDefault();
                        event.stopPropagation();
                        state[definition.key] = definition.key === "period" ? value : Number(value);
                        renderOptions();
                    });
                    options.appendChild(option);
                });
                column.appendChild(options);
                columns.appendChild(column);
            });
            Array.prototype.forEach.call(columns.querySelectorAll(".calendar-time-options"), function (options, index) {
                if (scrollPositions[index] !== undefined) {
                    options.scrollTop = scrollPositions[index];
                }
            });
        }

        picker.trigger.addEventListener("click", function (event) {
            event.preventDefault();
            if (!picker.popover.hidden) {
                closeCalendarPicker(picker);
                return;
            }
            state = parseCalendarTime(input.value) || {hour: 9, minute: 0, period: "AM"};
            renderOptions();
            openCalendarPicker(picker);
        });
        cancel.addEventListener("click", function (event) {
            event.preventDefault();
            closeCalendarPicker(picker);
            picker.trigger.focus();
        });
        done.addEventListener("click", function (event) {
            event.preventDefault();
            input.value = calendarTimeValue(state);
            syncTrigger();
            emitCalendarPickerChange(input);
            closeCalendarPicker(picker);
            picker.trigger.focus();
        });
        input.addEventListener("input", syncTrigger);
        input.addEventListener("change", syncTrigger);
        syncTrigger();
    }

    function initCalendarPickers(form) {
        each('input[type="date"]', function (input) {
            initCalendarDatePicker(input);
        }, form);
        each('input[type="time"]', function (input) {
            initCalendarTimePicker(input);
        }, form);
    }

    function initStandaloneCalendarPickers() {
        each("[data-calendar-picker-form]", function (form) {
            initCalendarPickers(form);
        });
    }

    function initAdminIpBlockForms() {
        each("[data-admin-ip-block-form]", function (form) {
            if (form.dataset.adminIpBlockInitialized) {
                return;
            }
            form.dataset.adminIpBlockInitialized = "true";
            var input = form.querySelector("[name='ip_address']");
            var button = form.querySelector("button[type='submit']");
            var warning = form.querySelector("[data-admin-ip-warning]");
            var currentIp = (form.dataset.currentIp || "").trim().toLowerCase();
            if (!input || !button) {
                return;
            }

            function sync() {
                var matchesCurrentIp = Boolean(currentIp) &&
                    input.value.trim().toLowerCase() === currentIp;
                button.disabled = matchesCurrentIp;
                button.setAttribute("aria-disabled", String(matchesCurrentIp));
                if (matchesCurrentIp) {
                    button.setAttribute(
                        "title",
                        "The current connection IP cannot be blocked from this session."
                    );
                } else {
                    button.removeAttribute("title");
                }
                if (warning) {
                    warning.hidden = !matchesCurrentIp;
                }
            }

            input.addEventListener("input", sync);
            input.addEventListener("change", sync);
            sync();
        });
    }

    function initEmployeeScheduleForms() {
        each("[data-employee-schedule-form]", function (form) {
            if (form.dataset.employeeScheduleInitialized) {
                return;
            }
            form.dataset.employeeScheduleInitialized = "true";
            var status = form.querySelector("[name='status']");
            var workingCheckbox = form.querySelector("input[type='checkbox'][name$='is_working']");
            var workingCheckboxes = form.querySelectorAll("input[type='checkbox'][name$='is_working']");
            var weeklyRows = form.querySelectorAll("[data-employee-schedule-row]");
            var timeFields = form.querySelectorAll(
                "[data-employee-schedule-field='start_time'], [data-employee-schedule-field='end_time']"
            );
            var reasonFields = form.querySelectorAll("[data-employee-schedule-field='reason']");

            function setFieldVisibility(fields, visible) {
                fields.forEach(function (field) {
                    field.hidden = !visible;
                    field.setAttribute("aria-hidden", String(!visible));
                    var control = field.querySelector("input, select, textarea");
                    if (!control) {
                        return;
                    }
                    control.disabled = !visible;
                    if (control.type === "time") {
                        control.required = visible;
                    }
                });
            }

            function syncFields() {
                if (weeklyRows.length) {
                    Array.prototype.forEach.call(weeklyRows, function (row) {
                        var rowCheckbox = row.querySelector("input[type='checkbox'][name$='is_working']");
                        var rowIsWorking = Boolean(rowCheckbox && rowCheckbox.checked);
                        row.setAttribute("data-employee-schedule-status", rowIsWorking ? "working" : "off");
                        setFieldVisibility(
                            row.querySelectorAll(
                                "[data-employee-schedule-field='start_time'], [data-employee-schedule-field='end_time']"
                            ),
                            rowIsWorking
                        );
                    });
                    closeAllCalendarPickers();
                    return;
                }
                var isWorking = status
                    ? status.value === "working"
                    : Boolean(workingCheckbox && workingCheckbox.checked);
                var isClear = Boolean(status && status.value === "clear");
                form.setAttribute("data-employee-schedule-status", isWorking ? "working" : isClear ? "clear" : "off");
                setFieldVisibility(timeFields, isWorking);
                setFieldVisibility(reasonFields, !isClear);
                closeAllCalendarPickers();
            }

            if (status) {
                status.addEventListener("change", syncFields);
            }
            Array.prototype.forEach.call(workingCheckboxes, function (checkbox) {
                checkbox.addEventListener("change", syncFields);
            });
            syncFields();
            initCalendarPickers(form);
        });
    }

    function initWorkerSelectors() {
        each("select[multiple][data-worker-selector]", function (select) {
            if (select.dataset.workerSelectorInitialized) {
                return;
            }
            select.dataset.workerSelectorInitialized = "true";
            var shell = document.createElement("div");
            shell.className = "worker-selector-shell";
            select.parentNode.insertBefore(shell, select);
            shell.appendChild(select);
            select.classList.add("worker-selector-native");

            var header = document.createElement("div");
            header.className = "worker-selector-header";
            var search = document.createElement("input");
            search.type = "search";
            search.className = "worker-selector-search";
            search.placeholder = "Search workers";
            search.setAttribute("aria-label", "Search workers");
            var count = document.createElement("span");
            count.className = "worker-selector-count";
            header.appendChild(search);
            header.appendChild(count);
            shell.appendChild(header);

            var selectedList = document.createElement("div");
            selectedList.className = "worker-selector-selected";
            selectedList.setAttribute("aria-live", "polite");
            shell.appendChild(selectedList);

            var optionList = document.createElement("div");
            optionList.className = "worker-selector-options";
            optionList.setAttribute("role", "listbox");
            optionList.setAttribute("aria-multiselectable", "true");
            shell.appendChild(optionList);

            var footer = document.createElement("div");
            footer.className = "worker-selector-footer";
            var clear = createCalendarPickerButton("Clear all", "worker-selector-clear", "Clear all selected workers");
            footer.appendChild(clear);
            shell.appendChild(footer);

            function selectedOptions() {
                return Array.prototype.filter.call(select.options, function (option) { return option.selected; });
            }

            function toggleOption(option) {
                option.selected = !option.selected;
                sync();
            }

            function renderOptions() {
                while (optionList.firstChild) {
                    optionList.removeChild(optionList.firstChild);
                }
                var query = (search.value || "").trim().toLowerCase();
                var visibleOptions = Array.prototype.filter.call(select.options, function (option) {
                    return !query || option.textContent.toLowerCase().indexOf(query) !== -1;
                });
                visibleOptions.forEach(function (option) {
                    var button = document.createElement("button");
                    button.type = "button";
                    button.className = "worker-selector-option";
                    button.setAttribute("role", "option");
                    button.setAttribute("aria-selected", String(option.selected));
                    button.setAttribute("aria-label", (option.selected ? "Remove " : "Select ") + option.textContent);
                    var checkbox = document.createElement("span");
                    checkbox.className = "worker-selector-checkbox";
                    checkbox.setAttribute("aria-hidden", "true");
                    checkbox.textContent = option.selected ? "✓" : "";
                    var label = document.createElement("span");
                    label.textContent = option.textContent;
                    button.appendChild(checkbox);
                    button.appendChild(label);
                    button.addEventListener("click", function () {
                        toggleOption(option);
                    });
                    optionList.appendChild(button);
                });
                if (!visibleOptions.length) {
                    var empty = document.createElement("span");
                    empty.className = "worker-selector-empty";
                    empty.textContent = "No workers match this search.";
                    optionList.appendChild(empty);
                }
            }

            function sync() {
                var selected = selectedOptions();
                count.textContent = selected.length + (selected.length === 1 ? " worker selected" : " workers selected");
                while (selectedList.firstChild) {
                    selectedList.removeChild(selectedList.firstChild);
                }
                selected.forEach(function (option) {
                    var chip = document.createElement("span");
                    chip.className = "worker-selector-chip";
                    var chipLabel = document.createElement("span");
                    chipLabel.textContent = option.textContent;
                    var remove = createCalendarPickerButton("×", "worker-selector-remove", "Remove " + option.textContent);
                    remove.addEventListener("click", function () {
                        option.selected = false;
                        sync();
                    });
                    chip.appendChild(chipLabel);
                    chip.appendChild(remove);
                    selectedList.appendChild(chip);
                });
                renderOptions();
            }

            search.addEventListener("input", renderOptions);
            clear.addEventListener("click", function () {
                Array.prototype.forEach.call(select.options, function (option) { option.selected = false; });
                sync();
                search.focus();
            });
            select.addEventListener("change", sync);
            sync();
        });
    }

    function csrfToken() {
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        if (match) {
            return decodeURIComponent(match[1]);
        }
        var hidden = document.querySelector("input[name='csrfmiddlewaretoken']");
        return hidden ? hidden.value : "";
    }

    function initNativeMediaBridge() {
        if (!window.ReactNativeWebView || window.__grandCoastNativeMediaBound) {
            return;
        }
        window.__grandCoastNativeMediaBound = true;
        document.body.classList.add("native-media-enabled");
        var requestControls = {};

        function post(message) {
            window.ReactNativeWebView.postMessage(JSON.stringify(message));
        }

        function setStatus(requestId, text, isError) {
            var control = requestControls[requestId];
            var status = control && control.querySelector("[data-native-media-status]");
            if (!status) {
                status = document.querySelector("[data-native-media-status]");
            }
            if (status) {
                status.textContent = text;
                status.classList.toggle("is-error", Boolean(isError));
            }
            showAdminToast(text, isError);
        }

        function prepareGrant(file) {
            return fetch("/api/v1/native-upload-grants/", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({
                    target: file.target,
                    project_id: file.project_id,
                    target_id: file.target_id,
                    visibility: file.visibility,
                    context: file.context,
                    file_name: file.name,
                    file_size: file.size,
                    content_type: file.type,
                }),
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if (!response.ok) {
                        throw new Error(payload.error || "The server rejected this upload.");
                    }
                    payload.file_id = file.file_id;
                    return payload;
                });
            });
        }

        function prepareAndSend(requestId, files) {
            if (!files || !files.length) {
                setStatus(requestId, "No files were selected.", true);
                return;
            }
            setStatus(requestId, "Preparing protected upload...", false);
            var grantsByRequest = {};
            var failures = [];
            var work = files.map(function (file) {
                return prepareGrant(file).then(function (grant) {
                    grant.file_id = file.file_id;
                    grant.request_id = file.request_id || requestId;
                    var group = grant.request_id;
                    grantsByRequest[group] = grantsByRequest[group] || [];
                    grantsByRequest[group].push(grant);
                }).catch(function (error) {
                    failures.push(error);
                });
            });
            Promise.all(work).then(function () {
                var grantCount = Object.keys(grantsByRequest).reduce(function (total, key) {
                    return total + grantsByRequest[key].length;
                }, 0);
                if (failures.length || grantCount !== files.length) {
                    setStatus(requestId, failures[0] ? failures[0].message : "One or more files could not be prepared.", true);
                    return;
                }
                Object.keys(grantsByRequest).forEach(function (group) {
                    post({
                        type: "native-media-upload-grants",
                        request_id: group,
                        grants: grantsByRequest[group],
                    });
                    setStatus(group, "Uploading securely...", false);
                });
            });
        }

        window.__grandCoastReceiveNativeMedia = function (message) {
            if (!message || !message.type) {
                return;
            }
            if (message.type === "native-media-selected" || message.type === "native-media-queue-pending") {
                prepareAndSend(message.request_id || (message.files[0] && message.files[0].request_id), message.files || []);
                return;
            }
            if (message.type === "native-media-upload-progress") {
                setStatus(message.request_id, "Uploading " + (Number(message.progress) || 0) + "%...", false);
                return;
            }
            if (message.type === "native-media-uploaded") {
                setStatus(message.request_id, "Upload complete.", false);
                var control = requestControls[message.request_id];
                if (control) {
                    control.querySelectorAll("[data-native-media-action]").forEach(function (button) {
                        button.disabled = false;
                    });
                }
                document.dispatchEvent(new CustomEvent("grand-coast-native-media-uploaded", {detail: message}));
                return;
            }
            if (message.type === "native-media-canceled") {
                setStatus(message.request_id, "Capture canceled.", false);
                var canceledControl = requestControls[message.request_id];
                if (canceledControl) {
                    canceledControl.querySelectorAll("[data-native-media-action]").forEach(function (button) {
                        button.disabled = false;
                    });
                }
                return;
            }
            if (message.type === "native-media-upload-error" || message.type === "native-media-error") {
                setStatus(message.request_id, message.message || "The native action could not be completed.", true);
                var errorControl = requestControls[message.request_id];
                if (errorControl) {
                    errorControl.querySelectorAll("[data-native-media-action]").forEach(function (button) {
                        button.disabled = false;
                    });
                }
                return;
            }
        };

        document.addEventListener("click", function (event) {
            var button = event.target.closest && event.target.closest("[data-native-media-action]");
            if (!button) return;
            event.preventDefault();
            var control = button.closest("[data-native-media-control]") || button.parentElement;
            var requestId = "web-" + Date.now() + "-" + Math.random().toString(36).slice(2);
            requestControls[requestId] = control;
            control.querySelectorAll("[data-native-media-action]").forEach(function (item) {
                item.disabled = true;
            });
            setStatus(requestId, "Opening device picker...", false);
            post({
                type: "native-media-request",
                request_id: requestId,
                action: button.dataset.nativeMediaAction,
                target: button.dataset.nativeMediaTarget || "project_media",
                project_id: button.dataset.nativeMediaProject || "",
                target_id: button.dataset.nativeMediaTargetId || "",
                visibility: button.dataset.nativeMediaVisibility || "internal",
                context: button.dataset.nativeMediaContext || "progress",
                allow_multiple: button.dataset.nativeMediaMultiple !== "false",
                mime_types: button.dataset.nativeMediaMimeTypes || "*/*",
            });
        });

        post({type: "native-media-page-ready"});
    }

    function initProjectUpdateDrafts() {
        var dialog = document.querySelector("[data-project-update-draft-dialog]");
        var form = dialog && dialog.querySelector("[data-project-update-draft-form]");
        var titleField = form && form.querySelector('[name="project-update-draft-title"]');
        var bodyField = form && form.querySelector('[name="project-update-draft-body"]');
        var actionField = form && form.querySelector("[data-project-update-draft-action]");
        var state = dialog && dialog.querySelector("[data-project-update-draft-state]");
        var validation = dialog && dialog.querySelector("[data-project-update-draft-validation]");
        var actionBar = dialog && dialog.querySelector("[data-project-update-draft-actions]");
        var deleteConfirmation = dialog && dialog.querySelector("[data-project-update-draft-delete-confirm]");
        var deleteButton = dialog && dialog.querySelector("[data-delete-project-update-draft]");
        var cancelDeleteButton = dialog && dialog.querySelector("[data-cancel-project-update-draft-delete]");
        var closeButton = dialog && dialog.querySelector("[data-close-project-update-draft]");
        var activeTrigger = null;
        var savedDraft = false;

        if (!dialog || !form || !titleField || !bodyField || !actionField || !dialog.showModal) {
            return;
        }

        function resetDeleteConfirmation() {
            if (actionBar) actionBar.hidden = false;
            if (deleteConfirmation) deleteConfirmation.hidden = true;
        }

        function openDraft(trigger, preserveValues) {
            var row = trigger.closest("[data-project-update-entry]");
            var sourceTitle = row && row.querySelector(".project-update-entry-heading strong");
            var sourceBody = row && row.querySelector(".project-update-entry-content > p");
            var savedTemplate = row && row.querySelector("template[data-saved-client-draft]");
            var savedTitle = savedTemplate && savedTemplate.content.querySelector("[data-saved-draft-title]");
            var savedBody = savedTemplate && savedTemplate.content.querySelector("[data-saved-draft-body]");
            var actionUrl = trigger.dataset.projectUpdateDraftUrl;

            if (!row || !sourceTitle || !sourceBody || !actionUrl) return;

            activeTrigger = trigger;
            form.action = actionUrl;
            savedDraft = trigger.dataset.projectUpdateDraftSaved === "true";
            actionField.value = "keep";
            resetDeleteConfirmation();
            if (validation) validation.hidden = true;

            if (!preserveValues) {
                titleField.value = savedDraft && savedTitle ? savedTitle.textContent.trim() : sourceTitle.textContent.trim();
                bodyField.value = savedDraft && savedBody ? savedBody.textContent : sourceBody.textContent;
                titleField.removeAttribute("aria-invalid");
                bodyField.removeAttribute("aria-invalid");
                titleField.removeAttribute("aria-describedby");
                bodyField.removeAttribute("aria-describedby");
                each(".project-update-draft-error[id]", function (error) { error.hidden = true; });
            }

            if (deleteButton) {
                deleteButton.textContent = savedDraft ? "Delete draft" : "Discard draft";
            }
            if (state) {
                state.textContent = savedDraft
                    ? "Saved draft · private until sent"
                    : "Not saved yet · only visible to admin";
            }
            var description = dialog.querySelector("[data-project-update-draft-description]");
            if (description) {
                description.textContent = savedDraft
                    ? "Review or edit this saved copy. Sending publishes it in the client portal and notifies the client."
                    : "This separate copy is not saved yet. Sending publishes it in the client portal and notifies the client.";
            }
            if (!preserveValues) dialog.showModal();
            else if (!dialog.open) dialog.showModal();

            var invalidField = form.querySelector('[aria-invalid="true"]');
            (preserveValues && invalidField ? invalidField : titleField).focus();
        }

        each("[data-project-update-draft-trigger]", function (trigger) {
            trigger.addEventListener("click", function () {
                openDraft(trigger, false);
            });
        });

        if (dialog.dataset.autoOpen === "true" && dialog.dataset.sourceUpdateId) {
            var escapedSourceId = window.CSS && window.CSS.escape
                ? window.CSS.escape(dialog.dataset.sourceUpdateId)
                : dialog.dataset.sourceUpdateId;
            var retryTrigger = document.querySelector('[data-project-update-source-id="' + escapedSourceId + '"]');
            if (retryTrigger) openDraft(retryTrigger, true);
        }

        if (deleteButton) {
            deleteButton.addEventListener("click", function () {
                if (!savedDraft) {
                    dialog.close();
                    return;
                }
                if (actionBar) actionBar.hidden = true;
                if (deleteConfirmation) deleteConfirmation.hidden = false;
                if (cancelDeleteButton) cancelDeleteButton.focus();
            });
        }
        if (cancelDeleteButton) {
            cancelDeleteButton.addEventListener("click", function () {
                resetDeleteConfirmation();
                if (deleteButton) deleteButton.focus();
            });
        }
        if (closeButton) closeButton.addEventListener("click", function () { dialog.close(); });

        each("[data-project-update-draft-submit]", function (button) {
            button.addEventListener("click", function () {
                actionField.value = button.dataset.projectUpdateDraftSubmit;
            });
        });

        form.addEventListener("input", function (event) {
            var field = event.target;
            if (field === titleField || field === bodyField) {
                field.removeAttribute("aria-invalid");
                if (field.getAttribute("aria-describedby") === "project-update-draft-validation") {
                    field.removeAttribute("aria-describedby");
                }
                if (validation && titleField.value.trim() && bodyField.value.trim()) validation.hidden = true;
            }
        });

        form.addEventListener("submit", function (event) {
            var action = actionField.value;
            if (action !== "delete" && !form.checkValidity()) {
                event.preventDefault();
                if (validation) validation.hidden = false;
                var invalid = form.querySelector(":invalid");
                if (invalid) {
                    invalid.setAttribute("aria-invalid", "true");
                    invalid.setAttribute("aria-describedby", "project-update-draft-validation");
                    invalid.focus();
                }
                actionField.value = "keep";
                return;
            }
        });

        form.addEventListener("admin:submission-complete", function (event) {
            if (!event.detail || !event.detail.success || !activeTrigger) return;
            var row = activeTrigger.closest("[data-project-update-entry]");
            var draftAction = actionField.value;
            if (draftAction === "keep") {
                savedDraft = true;
                activeTrigger.dataset.projectUpdateDraftSaved = "true";
                if (row) {
                    var template = row.querySelector("template[data-saved-client-draft]");
                    if (!template) {
                        template = document.createElement("template");
                        template.dataset.savedClientDraft = "";
                        row.appendChild(template);
                    }
                    template.content.replaceChildren();
                    var savedTitleNode = document.createElement("span");
                    savedTitleNode.dataset.savedDraftTitle = "";
                    savedTitleNode.textContent = titleField.value;
                    var savedBodyNode = document.createElement("span");
                    savedBodyNode.dataset.savedDraftBody = "";
                    savedBodyNode.textContent = bodyField.value;
                    template.content.append(savedTitleNode, savedBodyNode);
                }
            } else if (draftAction === "send") {
                if (row) {
                    var draftLink = row.querySelector(".project-update-draft-link");
                    if (draftLink) draftLink.remove();
                    var heading = row.querySelector(".project-update-entry-heading");
                    if (heading && !heading.querySelector(".status-badge.green")) {
                        var badge = document.createElement("span");
                        badge.className = "status-badge green";
                        badge.textContent = "Client copy sent";
                        heading.appendChild(badge);
                    }
                    var savedTemplate = row.querySelector("template[data-saved-client-draft]");
                    if (savedTemplate) savedTemplate.remove();
                }
                activeTrigger.dataset.projectUpdateDraftSaved = "false";
            } else if (draftAction === "delete") {
                if (row) {
                    var deletedTemplate = row.querySelector("template[data-saved-client-draft]");
                    if (deletedTemplate) deletedTemplate.remove();
                }
                activeTrigger.dataset.projectUpdateDraftSaved = "false";
            }
            dialog.close();
        });

        dialog.addEventListener("close", function () {
            resetDeleteConfirmation();
            if (activeTrigger && activeTrigger.isConnected) activeTrigger.focus();
            activeTrigger = null;
        });
    }

    function initProjectSequenceConfirmation() {
        var dialog = document.querySelector("[data-project-sequence-confirmation]");
        var dialogForm = dialog && dialog.querySelector("[data-project-sequence-confirmation-form]");
        var copy = dialog && dialog.querySelector("[data-project-sequence-confirmation-copy]");
        var error = dialog && dialog.querySelector("[data-project-sequence-confirmation-error]");
        var cancelButton = dialog && dialog.querySelector("[data-project-sequence-cancel]");
        var confirmButton = dialog && dialog.querySelector("[data-project-sequence-confirm]");
        var pendingForm = null;
        var pendingCheckbox = null;
        var pendingValue = null;
        var submitting = false;

        if (!dialog || !dialogForm || !copy || !cancelButton || !confirmButton || typeof dialog.showModal !== "function") return;

        each("[data-project-sequence-form]", function (form) {
            var checkbox = form.querySelector("[data-project-sequence-checkbox]");
            var title = form.querySelector("[data-project-sequence-title]");
            if (!checkbox || !title) return;

            checkbox.addEventListener("change", function () {
                if (pendingForm || submitting) return;

                pendingValue = checkbox.checked;
                checkbox.checked = !pendingValue;
                pendingForm = form;
                pendingCheckbox = checkbox;

                var milestoneTitle = title.textContent.trim();
                if (pendingValue) {
                    copy.textContent = 'Mark “' + milestoneTitle + '” complete? This updates the shared progress tracker shown to the client and team, and notifies the client and any assigned team members. If a pending payment draw is linked to this milestone, it will also become ready to bill.';
                    confirmButton.textContent = "Mark complete";
                } else {
                    copy.textContent = 'Reopen “' + milestoneTitle + '”? This lowers the shared progress tracker shown to the client and team and notifies the client and any assigned team members.';
                    confirmButton.textContent = "Reopen milestone";
                }
                confirmButton.disabled = false;
                confirmButton.removeAttribute("aria-busy");
                if (error) error.hidden = true;
                dialog.showModal();
            });

            form.addEventListener("admin:submission-complete", function (event) {
                if (!submitting || pendingForm !== form) return;
                submitting = false;
                if (!event.detail || !event.detail.success) {
                    pendingCheckbox.checked = !pendingValue;
                }
                confirmButton.disabled = false;
                confirmButton.removeAttribute("aria-busy");
                confirmButton.textContent = pendingValue ? "Mark complete" : "Reopen milestone";
                dialog.close();
            });
        });

        dialogForm.addEventListener("submit", function (event) {
            var submitter = event.submitter || document.activeElement;
            if (!submitter || submitter.value !== "confirm") return;
            event.preventDefault();
            if (!pendingForm || !pendingCheckbox || pendingValue === null || submitting) return;

            submitting = true;
            pendingCheckbox.checked = pendingValue;
            confirmButton.disabled = true;
            confirmButton.setAttribute("aria-busy", "true");
            confirmButton.textContent = "Saving…";
            try {
                if (typeof pendingForm.requestSubmit === "function") {
                    pendingForm.requestSubmit();
                } else {
                    pendingForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
                }
            } catch (submissionError) {
                submitting = false;
                pendingCheckbox.checked = !pendingValue;
                confirmButton.disabled = false;
                confirmButton.removeAttribute("aria-busy");
                confirmButton.textContent = pendingValue ? "Mark complete" : "Reopen milestone";
                if (error) error.hidden = false;
            }
        });

        dialog.addEventListener("close", function () {
            if (!submitting && pendingCheckbox && pendingCheckbox.isConnected) pendingCheckbox.focus();
            if (!submitting) {
                pendingForm = null;
                pendingCheckbox = null;
                pendingValue = null;
            }
        });
    }

    function initNotificationActions() {
        function showNotificationEmptyState() {
            var inbox = document.querySelector(".notification-inbox");
            if (!inbox || inbox.querySelector(".notification-card")) {
                return;
            }
            var empty = document.createElement("div");
            empty.className = "empty-detail";
            empty.textContent = "You're all caught up. New assignments and workflow alerts will appear here.";
            inbox.appendChild(empty);
        }

        each("[data-notification-read]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.getAttribute("action") || window.location.href, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
                }).then(function (response) {
                    if (!response.ok) {
                        throw new Error("Unable to mark notification read");
                    }
                    var card = form.closest("[data-notification-id]");
                    if (card) {
                        card.classList.remove("is-unread");
                    }
                    form.remove();
                    showAdminToast("Notification marked as read.", false);
                }).catch(function () {
                    showAdminToast("Could not update the notification. Try again.", true);
                });
            });
        });

        each("[data-notification-clear]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.getAttribute("action") || window.location.href, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
                }).then(function (response) {
                    if (!response.ok) {
                        throw new Error("Unable to clear notification");
                    }
                    var card = form.closest("[data-notification-id]");
                    if (card) {
                        card.remove();
                    }
                    showNotificationEmptyState();
                    showAdminToast("Notification cleared.", false);
                }).catch(function () {
                    showAdminToast("Could not clear the notification. Try again.", true);
                });
            });
        });
        each("[data-notification-clear-all]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.getAttribute("action") || window.location.href, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
                }).then(function (response) {
                    if (!response.ok) {
                        throw new Error("Unable to clear notifications");
                    }
                    each(".notification-card", function (card) {
                        card.remove();
                    });
                    showNotificationEmptyState();
                    showAdminToast("Notifications cleared.", false);
                }).catch(function () {
                    showAdminToast("Could not clear notifications. Try again.", true);
                });
            });
        });

        each("[data-notification-read-all]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.getAttribute("action") || window.location.href, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
                }).then(function (response) {
                    if (!response.ok) {
                        throw new Error("Unable to mark notifications read");
                    }
                    each(".notification-card", function (card) {
                        card.classList.remove("is-unread");
                        var readForm = card.querySelector("[data-notification-read]");
                        if (readForm) {
                            readForm.remove();
                        }
                    });
                    showAdminToast("Notifications marked as read.", false);
                }).catch(function () {
                    showAdminToast("Could not update notifications. Try again.", true);
                });
            });
        });
    }

    function initCalendarDaySelection() {
        var calendar = document.querySelector(".calendar-month-panel");
        if (
            !calendar
            || typeof window.fetch !== "function"
            || typeof window.DOMParser !== "function"
            || typeof window.URL !== "function"
            || !window.history
            || typeof window.history.pushState !== "function"
        ) {
            return;
        }

        var calendarHost = calendar.parentNode;
        var requestSequence = 0;
        var activeRequest = null;

        function dayFromLink(link) {
            try {
                return new window.URL(link.href, window.location.href).searchParams.get("day") || "";
            } catch (error) {
                return "";
            }
        }

        function setSelectedDay(day) {
            each(".calendar-day", function (link) {
                link.classList.toggle("is-selected", Boolean(day && dayFromLink(link) === day));
            }, calendar);
            each(".calendar-mobile-day", function (card) {
                var dayLink = card.querySelector("[data-calendar-day-selection]");
                card.classList.toggle("is-selected", Boolean(day && dayLink && dayFromLink(dayLink) === day));
            }, calendar);
        }

        function setStatus(message) {
            var status = calendar.querySelector("[data-calendar-selection-status]");
            if (status) {
                status.textContent = message;
            }
        }

        function loadCalendar(url, updateHistory) {
            requestSequence += 1;
            var currentSequence = requestSequence;
            if (activeRequest) {
                activeRequest.abort();
            }
            activeRequest = typeof window.AbortController === "function" ? new window.AbortController() : null;

            calendar.setAttribute("aria-busy", "true");
            setStatus(url.searchParams.has("day") ? "Loading day details…" : "Loading month…");

            var options = {
                credentials: "same-origin",
                headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "text/html"},
            };
            if (activeRequest) {
                options.signal = activeRequest.signal;
            }

            window.fetch(url.href, options)
                .then(function (response) {
                    if (response.redirected) {
                        var destination = new window.URL(response.url, window.location.href);
                        if (destination.origin !== window.location.origin || destination.pathname !== url.pathname) {
                            window.location.assign(destination.href);
                            var redirectError = new Error("Calendar request redirected.");
                            redirectError.redirecting = true;
                            throw redirectError;
                        }
                    }
                    if (!response.ok) {
                        var responseError = new Error("Calendar request failed.");
                        responseError.status = response.status;
                        throw responseError;
                    }
                    return response.text();
                })
                .then(function (markup) {
                    if (currentSequence !== requestSequence) {
                        return;
                    }

                    var parsed = new window.DOMParser().parseFromString(markup, "text/html");
                    var freshCalendar = parsed.querySelector(".calendar-month-panel");
                    if (!freshCalendar) {
                        throw new Error("Calendar details were not present in the response.");
                    }

                    var requestedMonth = url.searchParams.get("month");
                    var replacePanel = !url.searchParams.has("day")
                        || !requestedMonth
                        || requestedMonth !== calendar.dataset.month;
                    if (replacePanel) {
                        var updatedCalendar = document.importNode(freshCalendar, true);
                        disposeCalendarPickers(calendar);
                        calendar.replaceWith(updatedCalendar);
                        calendar = updatedCalendar;
                        initCalendarDayForms(calendar);
                    } else {
                        var currentContent = calendar.querySelector("#calendar-day-selection-content");
                        var freshContent = freshCalendar.querySelector("#calendar-day-selection-content");
                        if (currentContent) {
                            disposeCalendarPickers(currentContent);
                        }
                        if (freshContent) {
                            var updatedContent = document.importNode(freshContent, true);
                            if (currentContent) {
                                currentContent.replaceWith(updatedContent);
                            } else {
                                var mobileList = calendar.querySelector(".calendar-mobile-list");
                                if (mobileList) {
                                    mobileList.insertAdjacentElement("afterend", updatedContent);
                                } else {
                                    calendar.appendChild(updatedContent);
                                }
                            }
                            initCalendarDayForms(updatedContent);
                        } else if (currentContent) {
                            currentContent.remove();
                        }
                    }

                    setSelectedDay(url.searchParams.get("day") || "");
                    if (updateHistory && url.href !== window.location.href) {
                        window.history.pushState({calendarSelection: true}, "", url.href);
                    }
                    calendar.removeAttribute("aria-busy");
                    setStatus("");
                    activeRequest = null;
                })
                .catch(function (error) {
                    if (currentSequence !== requestSequence || error.name === "AbortError" || error.redirecting) {
                        return;
                    }
                    calendar.removeAttribute("aria-busy");
                    activeRequest = null;
                    setStatus(
                        error.status === 403
                            ? "You no longer have access to this calendar."
                            : url.searchParams.has("day")
                                ? "Couldn’t load that day’s details. Select the day again to retry."
                                : "Couldn’t load that month. Select Previous or Next to retry."
                    );
                });
        }

        calendarHost.addEventListener("click", function (event) {
            var target = event.target;
            var monthLink = target && typeof target.closest === "function"
                ? target.closest("[data-calendar-month-selection]")
                : null;
            var dayLink = target && typeof target.closest === "function"
                ? target.closest("[data-calendar-day-selection]")
                : null;
            var link = monthLink || dayLink;
            if (
                !link
                || !calendarHost.contains(link)
                || event.defaultPrevented
                || event.button !== 0
                || event.metaKey
                || event.ctrlKey
                || event.shiftKey
                || event.altKey
                || (link.target && link.target !== "_self")
            ) {
                return;
            }

            var url = new window.URL(link.href, window.location.href);
            if (
                url.origin !== window.location.origin
                || url.pathname !== window.location.pathname
                || (monthLink && !url.searchParams.has("month"))
                || (dayLink && !url.searchParams.has("day"))
            ) {
                return;
            }

            event.preventDefault();
            loadCalendar(url, true);
        });

        activeCalendarPopstate = function () {
            var url = new window.URL(window.location.href);
            loadCalendar(url, false);
        };
        if (!calendarPopstateBound) {
            calendarPopstateBound = true;
            window.addEventListener("popstate", function () {
                if (activeCalendarPopstate) activeCalendarPopstate();
            });
        }
    }

    function initCalendarDayForms(root) {
        each("[data-calendar-day-form]", function (form) {
            var status = form.querySelector("[name='status']");
            if (!status) {
                return;
            }

            var shortFields = form.querySelectorAll(
                "[data-calendar-day-field='short_start'], [data-calendar-day-field='short_end']"
            );
            var reasonFields = form.querySelectorAll("[data-calendar-day-field='reason']");

            function setFieldVisibility(fields, visible) {
                fields.forEach(function (field) {
                    field.hidden = !visible;
                    field.setAttribute("aria-hidden", String(!visible));
                    var control = field.querySelector("input, select, textarea");
                    if (!control) {
                        return;
                    }
                    control.disabled = !visible;
                    if (control.type === "time") {
                        control.required = visible;
                    }
                });
            }

            function syncFields() {
                var isShort = status.value === "short";
                var isOverride = isShort || status.value === "closed";
                form.setAttribute("data-calendar-day-status", status.value || "normal");
                setFieldVisibility(shortFields, isShort);
                setFieldVisibility(reasonFields, isOverride);
                closeAllCalendarPickers();
            }

            status.addEventListener("change", syncFields);
            syncFields();
            initCalendarPickers(form);
        }, root);
    }

    function numberValue(input) {
        var value = Number.parseFloat(input && input.value);
        return Number.isFinite(value) ? value : 0;
    }

    function refreshEstimateTotal(form) {
        var total = 0;
        each("[data-form-row]", function (row) {
            var deleted = row.querySelector('input[name$="-DELETE"]');
            if (row.classList.contains("is-removed") || (deleted && deleted.checked)) {
                return;
            }
            total += numberValue(row.querySelector('input[name$="-quantity"]')) *
                numberValue(row.querySelector('input[name$="-unit_price"]'));
        }, form);
        var output = form.querySelector("[data-estimate-total]");
        if (output) {
            output.textContent = "$" + total.toLocaleString("en-US", {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
            });
        }
    }

    function addEstimateRow(container) {
        var totalInput = document.getElementById(container.dataset.totalInput);
        if (!totalInput) {
            return;
        }
        var index = Number.parseInt(totalInput.value, 10) || 0;
        var row = document.createElement("div");
        row.className = "line-item-row estimate-line-grid";
        row.dataset.formRow = "";
        row.innerHTML =
            '<input type="hidden" name="lines-' + index + '-id" id="id_lines-' + index + '-id">' +
            '<label>Description<input type="text" name="lines-' + index + '-description" id="id_lines-' + index + '-description"></label>' +
            '<label>Qty<input type="number" name="lines-' + index + '-quantity" id="id_lines-' + index + '-quantity" min="0.01" step="0.01" value="1.00"></label>' +
            '<label>Unit price<input type="number" name="lines-' + index + '-unit_price" id="id_lines-' + index + '-unit_price" min="0" step="0.01" value="0.00"></label>' +
            '<input type="hidden" name="lines-' + index + '-sort_order" value="' + index + '">' +
            '<label class="remove-line-label"><input type="checkbox" name="lines-' + index + '-DELETE" value="on"> Remove</label>';
        container.appendChild(row);
        totalInput.value = String(index + 1);
        var form = container.closest("form");
        if (form) {
            refreshEstimateTotal(form);
        }
    }

    function initEstimateEditors() {
        each(".estimate-editor-form", function (form) {
            var container = form.querySelector(".estimate-line-items");
            if (!container) {
                return;
            }
            var addButton = document.createElement("button");
            addButton.type = "button";
            addButton.className = "button button-quiet add-line-item";
            addButton.textContent = "+ Add line item";
            addButton.addEventListener("click", function () {
                addEstimateRow(container);
            });
            var template = form.querySelector("[data-line-template]");
            if (template) {
                template.before(addButton);
            } else {
                container.after(addButton);
            }

            form.addEventListener("input", function (event) {
                if (event.target.matches('input[name$="-quantity"], input[name$="-unit_price"]')) {
                    refreshEstimateTotal(form);
                }
            });
            form.addEventListener("change", function (event) {
                if (event.target.matches('input[name$="-DELETE"]')) {
                    event.target.closest("[data-form-row]").classList.toggle("is-removed", event.target.checked);
                    refreshEstimateTotal(form);
                }
            });
            refreshEstimateTotal(form);
        });
    }

    function openModal(source) {
        var backdrop = document.querySelector("[data-modal]");
        var content = backdrop && backdrop.querySelector("[data-modal-content]");
        if (!backdrop || !content) {
            return;
        }
        var src = source.dataset.lightboxSrc;
        var alt = source.dataset.lightboxAlt || "Project media";
        var image = document.createElement("img");
        image.className = "modal-image";
        image.src = src;
        image.alt = alt;
        content.replaceChildren(image);
        var dialog = backdrop.querySelector("[role=dialog]");
        if (dialog) {
            dialog.removeAttribute("aria-labelledby");
            dialog.setAttribute("aria-label", alt);
        }
        activeModalTrigger = source;
        backdrop.hidden = false;
        document.body.classList.add("modal-open");
        var closeButton = backdrop.querySelector('[data-action="close-modal"]');
        if (closeButton) closeButton.focus();
    }

    function closeModal() {
        var backdrop = document.querySelector("[data-modal]");
        if (!backdrop) {
            return;
        }
        backdrop.hidden = true;
        document.body.classList.remove("modal-open");
        if (activeModalTrigger && activeModalTrigger.isConnected) activeModalTrigger.focus();
        activeModalTrigger = null;
    }

    function initEstimateDeliveryDialog() {
        each("[data-estimate-delivery-dialog]", function (dialog) {
            var optionsPanel = dialog.querySelector("[data-estimate-delivery-options]");
            var smsPanel = dialog.querySelector("[data-estimate-sms-followup]");
            var form = dialog.querySelector("[data-estimate-delivery-form]");
            var deliveryError = dialog.querySelector("[data-estimate-delivery-error]");
            var smsLink = dialog.querySelector("[data-estimate-sms-link]");
            var channelChoices = form ? Array.prototype.slice.call(form.querySelectorAll('input[name="delivery_mode"]')) : [];
            var allThreeChoice = form ? form.querySelector("[data-estimate-delivery-all]") : null;

            function clearDeliveryQuery() {
                var url = new URL(window.location.href);
                url.searchParams.delete("delivery_prompt");
                url.searchParams.delete("sms_followup");
                url.searchParams.delete("sms_after_all");
                url.searchParams.delete("sms_after_other_channels");
                window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
            }

            function syncAllThreeChoice(selectAllChannels) {
                if (!allThreeChoice) return;
                if (selectAllChannels) {
                    channelChoices.forEach(function (choice) {
                        if (!choice.disabled) choice.checked = allThreeChoice.checked;
                    });
                    return;
                }
                allThreeChoice.checked = channelChoices.length > 0 && channelChoices.every(function (choice) {
                    return choice.checked;
                });
            }

            function openDeliveryState(state) {
                var showingSms = state === "sms";
                optionsPanel.hidden = showingSms;
                smsPanel.hidden = !showingSms;
                dialog.setAttribute(
                    "aria-labelledby",
                    showingSms ? "estimate-sms-followup-title" : "estimate-delivery-title"
                );
                dialog.setAttribute(
                    "aria-describedby",
                    showingSms ? "estimate-sms-followup-description" : "estimate-delivery-description"
                );
                if (!dialog.open) {
                    dialog.showModal();
                }
                if (!showingSms) {
                    var firstAvailableChoice = dialog.querySelector('input[name="delivery_mode"]:not(:disabled)');
                    if (firstAvailableChoice) firstAvailableChoice.focus();
                } else {
                    var smsAction = smsPanel.querySelector("a[href]");
                    if (smsAction) smsAction.focus();
                }
            }

            each("[data-open-estimate-delivery]", function (trigger) {
                trigger.addEventListener("click", function () {
                    openDeliveryState("delivery");
                });
            });
            each("[data-close-estimate-delivery]", function (button) {
                button.addEventListener("click", function () {
                    clearDeliveryQuery();
                    dialog.close();
                });
            });
            if (form) {
                if (allThreeChoice) {
                    allThreeChoice.addEventListener("change", function () {
                        syncAllThreeChoice(true);
                        if (deliveryError) deliveryError.hidden = true;
                    });
                }
                form.addEventListener("submit", function (event) {
                    var selected = form.querySelectorAll('input[name="delivery_mode"]:checked');
                    var allSelected = allThreeChoice && allThreeChoice.checked;
                    if (!selected.length && !allSelected) {
                        event.preventDefault();
                        if (deliveryError) {
                            deliveryError.hidden = false;
                            deliveryError.focus();
                        }
                        return;
                    }
                    if (deliveryError) deliveryError.hidden = true;
                    if (selected.length !== 1 || selected[0].value !== "sms") return;
                    event.preventDefault();
                    if (!smsLink) return;
                    var smsHref = smsLink.href;
                    clearDeliveryQuery();
                    dialog.close();
                    window.location.assign(smsHref);
                });
                form.addEventListener("admin:submission-complete", function (event) {
                    if (event.detail && event.detail.success && dialog.open) dialog.close();
                });
                form.addEventListener("change", function (event) {
                    if (event.target.matches('input[name="delivery_mode"]')) {
                        syncAllThreeChoice(false);
                        if (deliveryError) deliveryError.hidden = true;
                    }
                });
            }
            if (smsLink) {
                smsLink.addEventListener("click", function () {
                    clearDeliveryQuery();
                    dialog.close();
                });
            }
            dialog.addEventListener("close", clearDeliveryQuery);

            if (dialog.dataset.autoOpen === "sms") {
                openDeliveryState("sms");
            } else if (dialog.dataset.autoOpen === "delivery") {
                openDeliveryState("delivery");
            }
        });
    }

    function initModal() {
        each(".js-lightbox", function (source) {
            source.addEventListener("click", function () {
                openModal(source);
            });
        });
        each('[data-action="close-modal"]', function (button) {
            button.addEventListener("click", closeModal);
        });
        var backdrop = document.querySelector("[data-modal]");
        if (backdrop) {
            backdrop.addEventListener("click", function (event) {
                if (event.target === backdrop) {
                    closeModal();
                }
            });
        }
        if (!modalEscapeHandlerBound) {
            modalEscapeHandlerBound = true;
            document.addEventListener("keydown", function (event) {
                if (event.key === "Escape") closeModal();
            });
        }
    }

    function initToastDismissal() {
        each(".toast", function (toast) {
            const dismiss = function () {
                if (toast.dataset.dismissed === "true") return;
                toast.dataset.dismissed = "true";
                toast.classList.add("is-dismissing");
                window.setTimeout(function () { toast.remove(); }, 220);
            };
            const dismissButton = toast.querySelector("[data-toast-dismiss]");
            if (dismissButton) dismissButton.addEventListener("click", dismiss);
            if (toast.dataset.persistent !== "true") {
                window.setTimeout(dismiss, 7000);
            }
        });
    }

    function findUpdatedAdminForm(root, sourceForm) {
        var forms = Array.from(root.querySelectorAll("form"));
        if (sourceForm.id) {
            var byId = forms.find(function (candidate) { return candidate.id === sourceForm.id; });
            if (byId) return byId;
        }
        var sourceAction = sourceForm.getAttribute("action") || "";
        var sourceClass = sourceForm.className || "";
        return forms.find(function (candidate) {
            return (candidate.getAttribute("action") || "") === sourceAction
                && (candidate.className || "") === sourceClass;
        }) || null;
    }

    function snapshotSelectedFiles(form) {
        var counts = Object.create(null);
        return Array.from(form.querySelectorAll('input[type="file"]')).map(function (input) {
            var name = input.name || input.id || "";
            var index = counts[name] || 0;
            counts[name] = index + 1;
            return {name: name, index: index, files: Array.from(input.files || [])};
        }).filter(function (item) { return item.files.length > 0; });
    }

    function restoreSelectedFiles(root, form, snapshots) {
        if (!snapshots.length || typeof DataTransfer !== "function") return;
        var updatedForm = findUpdatedAdminForm(root, form);
        if (!updatedForm) return;
        snapshots.forEach(function (snapshot) {
            var inputs = Array.from(updatedForm.querySelectorAll('input[type="file"]')).filter(function (input) {
                return (input.name || input.id || "") === snapshot.name;
            });
            var input = inputs[snapshot.index];
            if (!input) return;
            try {
                var transfer = new DataTransfer();
                snapshot.files.forEach(function (file) { transfer.items.add(file); });
                input.files = transfer.files;
                input.dispatchEvent(new Event("change", {bubbles: true}));
            } catch (error) {
                // Browsers that disallow restoring a FileList still retain the server's field errors.
            }
        });
    }

    function initAdminWorkspaceContent() {
        initNavigation();
        initContentPageNavigation({skipAutoFocus: true});
        initTaskSearch();
        initTaskFilterDropdowns();
        initTaskAssociationSelectors();
        initLeadDetailTabs();
        initProjectFilters();
        initGoogleReviewsCarousel();
        initFileInputs();
        initCalendarDaySelection();
        initCalendarDayForms();
        initEmployeeScheduleForms();
        initStandaloneCalendarPickers();
        initAdminIpBlockForms();
        initWorkerSelectors();
        initProjectUpdateDrafts();
        initProjectSequenceConfirmation();
        initNotificationActions();
        initEstimateEditors();
        initEstimateDeliveryDialog();
        initModal();
        initMediaPublicationSaveButtons();
        initClientMediaUploadFields();
        initNativeMediaBridge();
        initToastDismissal();
    }

    function initAdminAsyncForms() {
        if (!document.body.classList.contains("admin-site")) return;

        document.addEventListener("submit", function (event) {
            var form = event.target;
            if (form && form.matches && form.matches("[data-service-delete]")
                && !window.confirm('Delete "' + (form.dataset.serviceTitle || "this service") + '" from the website? Its service image, if any, will also be deleted. This cannot be undone.')) {
                event.preventDefault();
                return;
            }
            if (
                event.defaultPrevented
                || !form
                || form.tagName !== "FORM"
                || (form.getAttribute("method") || "get").toLowerCase() !== "post"
                || form.matches("[data-website-post-create], [data-website-post-save]")
                || form.hasAttribute("onsubmit")
                || form.hasAttribute("target")
                || form.matches("[data-delete-form], .calendar-delete-form, .lead-detail-delete-form")
            ) return;

            var submitter = event.submitter || null;
            if (
                (submitter && submitter.formTarget)
                || (submitter && submitter.name === "action" && submitter.value === "preview")
                || (submitter && submitter.dataset.projectUpdateDraftSubmit === "delete")
                || form.querySelector('[name="draft_action"][value="delete"]')
            ) return;

            var action = submitter && submitter.hasAttribute("formaction")
                ? submitter.formAction
                : (form.getAttribute("action") || window.location.href);
            var method = submitter && submitter.hasAttribute("formmethod")
                ? submitter.formMethod
                : (form.getAttribute("method") || "get");
            var requestUrl;
            try {
                requestUrl = new URL(action || window.location.href, window.location.href);
            } catch (error) {
                return;
            }
            if (
                method.toLowerCase() !== "post"
                || requestUrl.origin !== window.location.origin
                || /(?:^|\/)(?:logout|delete)(?:\/|$)/i.test(requestUrl.pathname)
            ) return;

            event.preventDefault();
            if (form.dataset.adminAsyncPending === "true") return;
            form.dataset.adminAsyncPending = "true";
            form.setAttribute("aria-busy", "true");

            var wasDisabled = Boolean(submitter && "disabled" in submitter && submitter.disabled);
            if (submitter && "disabled" in submitter) {
                submitter.disabled = true;
                submitter.setAttribute("aria-busy", "true");
            }
            var payload = new FormData(form);
            if (submitter && submitter.name) payload.append(submitter.name, submitter.value);
            var hasFiles = Array.prototype.some.call(form.querySelectorAll('input[type="file"]'), function (input) {
                return input.files && input.files.length > 0;
            });
            var selectedFiles = snapshotSelectedFiles(form);
            var previousScrollY = window.scrollY;
            showAdminToast(hasFiles ? "Uploading files…" : "Saving changes…", false);

            (async function () {
                var success = false;
                var responseMessage = "";
                var responseAction = null;
                var responseDocument = null;
                var incomingApp = null;
                var updatedForm = null;
                try {
                    var response = await fetch(requestUrl.href, {
                        method: "POST",
                        body: payload,
                        credentials: "same-origin",
                        headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "text/html"}
                    });
                    var finalUrl = new URL(response.url || requestUrl.href, window.location.href);
                    if (response.status === 403 || finalUrl.pathname.indexOf("/accounts/login") === 0) {
                        responseMessage = "Your session needs attention. Sign in again, then retry this change.";
                    } else {
                        responseDocument = new DOMParser().parseFromString(await response.text(), "text/html");
                        incomingApp = responseDocument.querySelector(".admin-app[data-admin-root]");
                        updatedForm = incomingApp && findUpdatedAdminForm(incomingApp, form);
                        var errorScope = updatedForm || responseDocument;
                        var serverErrors = Array.from(errorScope.querySelectorAll(".errorlist"))
                            .map(function (item) { return item.textContent.trim(); })
                            .filter(Boolean)
                            .filter(function (message, index, all) { return all.indexOf(message) === index; });
                        var serverToast = responseDocument.querySelector(".toast-region .toast");
                        var serverMessage = serverToast && serverToast.querySelector(".toast-content");
                        var serverFailed = Boolean(serverToast && (serverToast.classList.contains("toast-error") || serverToast.classList.contains("toast-danger")));
                        success = response.ok && !serverErrors.length && !serverFailed;
                        responseMessage = serverErrors.join(" ") || (serverMessage && serverMessage.textContent.trim()) || (success
                            ? "Changes saved."
                            : response.status >= 500
                                ? "The server could not finish this change (HTTP " + response.status + "). Check the server log for the exact cause."
                                : "The server rejected this change (HTTP " + response.status + "). Review the form and try again.");
                        if (success && /invite|reset/i.test(requestUrl.pathname)) {
                            var handoff = responseDocument.querySelector(".dashboard-alert");
                            if (handoff && handoff.textContent.trim()) {
                                var handoffLink = handoff.querySelector("a[href]");
                                var handoffUrl = handoffLink && new URL(handoffLink.href, window.location.href);
                                responseMessage = Array.from(handoff.querySelectorAll("strong, span"))
                                    .map(function (node) { return node.textContent.replace(/\s+/g, " ").trim(); })
                                    .filter(Boolean)
                                    .join(" ") || "One-time setup link created.";
                                if (handoffUrl && handoffUrl.origin === window.location.origin) {
                                    responseAction = {href: handoffUrl.href, label: "Open one-time link"};
                                }
                            }
                        }
                        var currentApp = document.querySelector(".admin-app[data-admin-root]");
                        if (incomingApp && currentApp) {
                            incomingApp.querySelectorAll("[autofocus]").forEach(function (element) {
                                element.removeAttribute("autofocus");
                            });
                            currentApp.replaceWith(incomingApp);
                            var cleanUrl = new URL(finalUrl.href);
                            cleanUrl.searchParams.delete("focus_search");
                            window.history.replaceState(window.history.state, "", cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
                            initAdminWorkspaceContent();
                            if (!success) restoreSelectedFiles(incomingApp, form, selectedFiles);
                            window.scrollTo(window.scrollX, previousScrollY);
                        }
                    }
                } catch (error) {
                    responseMessage = "Connection lost. Your changes are still in the form; retry when connected.";
                }

                showAdminToast(responseMessage, !success, responseAction && responseAction.href, responseAction && responseAction.label);
                form.dispatchEvent(new CustomEvent("admin:submission-complete", {
                    detail: {success: success, message: responseMessage, submitter: submitter}
                }));

                delete form.dataset.adminAsyncPending;
                delete form.dataset.submitting;
                form.removeAttribute("aria-busy");
                if (submitter && submitter.isConnected && "disabled" in submitter) {
                    submitter.disabled = wasDisabled;
                    submitter.removeAttribute("aria-busy");
                }
            }());
        });
    }
    function initMediaPublicationSaveButtons(root) {
        each("[data-publication-save]", function (form) {
            if (form.dataset.publicationSaveBound === "true") return;
            form.dataset.publicationSaveBound = "true";
            form.addEventListener("submit", function (event) {
                if (form.dataset.submitting === "true") {
                    event.preventDefault();
                    return;
                }
                form.dataset.submitting = "true";
                form.setAttribute("aria-busy", "true");
            });
        }, root);
    }

    function initMediaPublishing() {
        // Keep the clicked submitter's name/value in native form submission.
        initMediaPublicationSaveButtons();
        window.addEventListener("pageshow", function () {
            document.querySelectorAll("[data-publication-save]").forEach(function (form) {
                delete form.dataset.submitting;
                form.removeAttribute("aria-busy");
            });
        });
        const websiteMutation = { busy: false };
        const setWebsiteStatus = function (message, isError) {
            showAdminToast(message, isError);
        };
        const galleryPostFormKey = function (form) {
            if (form.matches("[data-website-post-create]")) return "create";
            if (form.dataset.websitePostKey) return form.dataset.websitePostKey;
            const card = form.closest("[data-website-post-id]");
            return card ? card.dataset.websitePostId : "";
        };
        const captureGalleryPostForms = function (library, excludedKey) {
            return Array.from(library.querySelectorAll("[data-website-post-create], [data-website-post-save]"))
                .filter(function (form) { return !form.hasAttribute("data-website-post-delete-form"); })
                .map(function (form) {
                    const key = galleryPostFormKey(form);
                    if (key === excludedKey) return null;
                    return {
                        key: key,
                        values: Array.from(form.elements).filter(function (control) {
                            return control.name && !["submit", "button", "reset"].includes(control.type);
                        }).map(function (control, index, controls) {
                            return {
                                name: control.name,
                                index: controls.slice(0, index).filter(function (previous) { return previous.name === control.name; }).length,
                                type: control.type,
                                value: control.value,
                                checked: control.checked,
                                files: control.type === "file" ? Array.from(control.files || []) : [],
                                selected: control.multiple && control.options
                                    ? Array.from(control.selectedOptions).map(function (option) { return option.value; })
                                    : null
                            };
                        })
                    };
                }).filter(Boolean);
        };
        const restoreGalleryPostForms = function (library, snapshots) {
            snapshots.forEach(function (snapshot) {
                const form = Array.from(library.querySelectorAll("[data-website-post-create], [data-website-post-save]"))
                    .find(function (candidate) { return galleryPostFormKey(candidate) === snapshot.key; });
                if (!form) return;
                snapshot.values.forEach(function (saved) {
                    const namedControls = Array.from(form.elements).filter(function (candidate) {
                        return candidate.name === saved.name;
                    });
                    const control = saved.type === "checkbox" || saved.type === "radio"
                        ? namedControls.find(function (candidate) { return candidate.value === saved.value; })
                            || namedControls[saved.index]
                        : namedControls[saved.index];
                    if (!control) return;
                    if (saved.type === "checkbox" || saved.type === "radio") control.checked = saved.checked;
                    else if (saved.type === "file" && saved.files.length && typeof DataTransfer === "function") {
                        const transfer = new DataTransfer();
                        saved.files.forEach(function (file) { transfer.items.add(file); });
                        control.files = transfer.files;
                        control.dispatchEvent(new Event("change", { bubbles: true }));
                    } else if (control.multiple && saved.selected) {
                        Array.from(control.options).forEach(function (option) {
                            option.selected = saved.selected.includes(option.value);
                        });
                    } else if (saved.type !== "file") control.value = saved.value;
                });
            });
        };
        document.addEventListener("change", function (event) {
            const input = event.target;
            if (!input.matches || !input.matches("[data-website-post-create] input[type='file'], [data-website-post-save] input[type='file']")) return;
            const form = input.closest("form");
            const review = form && form.querySelector("[data-website-post-file-review]");
            if (!review) return;
            review.querySelectorAll("[data-object-url]").forEach(function (element) {
                URL.revokeObjectURL(element.dataset.objectUrl);
            });
            review.replaceChildren();
            const files = Array.from(input.files || []);
            if (!files.length) {
                const empty = document.createElement("span");
                empty.textContent = review.dataset.emptyMessage || "No new files selected.";
                review.appendChild(empty);
                return;
            }
            files.forEach(function (file, index) {
                const card = document.createElement("div");
                card.className = "website-post-file-chip";
                const previewUrl = URL.createObjectURL(file);
                const isVideo = file.type.indexOf("video/") === 0 || /\.(mp4|mov|webm)$/i.test(file.name);
                const preview = isVideo
                    ? document.createElement("video")
                    : document.createElement("img");
                preview.src = previewUrl;
                preview.dataset.objectUrl = previewUrl;
                if (preview.tagName === "VIDEO") {
                    preview.controls = true;
                    preview.preload = "metadata";
                } else {
                    preview.alt = "Preview of " + file.name;
                }
                const label = document.createElement("span");
                label.textContent = (index + 1) + ". " + file.name;
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "text-link-button website-post-file-remove";
                remove.dataset.removeWebsitePostFile = String(index);
                remove.setAttribute("aria-label", "Remove " + file.name + " from this post");
                remove.textContent = "Remove file";
                card.append(preview, label, remove);
                review.appendChild(card);
            });
        });
        document.addEventListener("click", function (event) {
            const removeButton = event.target.closest && event.target.closest("[data-remove-website-post-file]");
            if (removeButton) {
                event.preventDefault();
                const form = removeButton.closest("form");
                const input = form && form.querySelector("input[type='file']");
                if (!input || typeof DataTransfer !== "function") {
                    setWebsiteStatus("This browser cannot remove one selected file. Clear the selection and choose the files again.", true);
                    return;
                }
                const removeIndex = Number(removeButton.dataset.removeWebsitePostFile);
                const transfer = new DataTransfer();
                Array.from(input.files || []).forEach(function (file, index) {
                    if (index !== removeIndex) transfer.items.add(file);
                });
                input.files = transfer.files;
                input.dispatchEvent(new Event("change", { bubbles: true }));
                return;
            }

            const removeExistingAsset = event.target.closest && event.target.closest("[data-website-asset-remove-trigger]");
            if (removeExistingAsset && !window.confirm("Remove this file from Publish Content? The original file and any Client Media share will remain unchanged.")) {
                event.preventDefault();
                return;
            }

            const cancelDelete = event.target.closest && event.target.closest("[data-website-post-delete-cancel]");
            if (cancelDelete) {
                const dialog = cancelDelete.closest("dialog");
                if (dialog && dialog.open) dialog.close();
                return;
            }

            const deleteTrigger = event.target.closest && event.target.closest("[data-website-post-delete-trigger]");
            if (!deleteTrigger) return;
            const card = deleteTrigger.closest("[data-website-post-id]");
            const dialog = card && card.querySelector("[data-website-post-delete-dialog]");
            if (!dialog || typeof dialog.showModal !== "function") return;
            event.preventDefault();
            dialog._websitePostDeleteTrigger = deleteTrigger;
            if (!dialog.dataset.focusRestorationBound) {
                dialog.dataset.focusRestorationBound = "true";
                dialog.addEventListener("close", function () {
                    const trigger = dialog._websitePostDeleteTrigger;
                    if (trigger && trigger.isConnected) trigger.focus();
                });
            }
            dialog.showModal();
            const cancel = dialog.querySelector("[data-website-post-delete-cancel]");
            if (cancel) cancel.focus();
        });
        document.addEventListener("submit", async function (event) {
            const form = event.target;
            if (!form.matches || !form.matches("[data-website-post-create], [data-website-post-save]")) return;
            event.preventDefault();
            if (websiteMutation.busy) {
                setWebsiteStatus("Wait for the current post save or upload to finish.", true);
                return;
            }
            const submitter = event.submitter || null;
            const action = submitter && submitter.name === "action"
                ? submitter.value
                : submitter && submitter.name === "website_asset_remove"
                    ? "remove_asset"
                    : "save";
            const requestUrl = submitter && submitter.hasAttribute("formaction")
                ? submitter.formAction
                : (form.getAttribute("action") || window.location.href);
            const key = galleryPostFormKey(form);
            const hasFiles = Array.from(form.querySelectorAll("input[type='file']")).some(function (input) {
                return input.files && input.files.length > 0;
            });
            websiteMutation.busy = true;
            form.setAttribute("aria-busy", "true");
            if (submitter && "disabled" in submitter) submitter.disabled = true;
            setWebsiteStatus(
                action === "delete" ? "Deleting project post draft…"
                    : action === "remove_asset" ? "Removing file from Publish Content…"
                            : hasFiles ? "Uploading post media…"
                                : action === "publish" ? "Publishing project post…" : "Saving project post…",
                false
            );
            try {
                const payload = new FormData(form);
                if (action === "remove_asset") {
                    form.querySelectorAll("input[type='file'][name]").forEach(function (input) {
                        payload.delete(input.name);
                    });
                }
                if (submitter && submitter.name) payload.append(submitter.name, submitter.value);
                const response = await fetch(requestUrl, {
                    method: "POST",
                    body: payload,
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: { "X-Requested-With": "XMLHttpRequest" }
                });
                const finalUrl = new URL(response.url || requestUrl, window.location.href);
                if (response.status === 403 || finalUrl.pathname.indexOf("/accounts/login") === 0) {
                    setWebsiteStatus("Your session needs attention. Sign in again, then retry this post.", true);
                    return;
                }
                const responseDocument = new DOMParser().parseFromString(await response.text(), "text/html");
                const currentLibrary = form.closest("[data-website-media-library]");
                const incomingLibrary = responseDocument.querySelector("[data-website-media-library]");
                const incomingForm = key === "create"
                    ? responseDocument.querySelector("[data-website-post-create]")
                    : Array.from(responseDocument.querySelectorAll("[data-website-post-save]"))
                        .find(function (candidate) { return galleryPostFormKey(candidate) === key; });
                const errors = incomingForm
                    ? Array.from(incomingForm.querySelectorAll(".errorlist")).map(function (item) { return item.textContent.trim(); }).filter(Boolean)
                    : [];
                const toast = responseDocument.querySelector(".toast-region .toast");
                const toastContent = toast && toast.querySelector(".toast-content");
                const toastIsError = Boolean(toast && (toast.classList.contains("toast-error") || toast.classList.contains("toast-danger")));
                const success = response.ok && response.redirected && !errors.length && !toastIsError
                    && currentLibrary && incomingLibrary;
                if (!success) {
                    const errorText = errors.join(" ") || (toastContent && toastContent.textContent.trim())
                        || (response.status >= 500
                            ? "The server could not finish this post. Check the server log and retry."
                            : "The post could not be saved. Review the details and try again.");
                    const errorBox = form.querySelector("[data-website-post-response-errors]");
                    if (errorBox) {
                        errorBox.textContent = errorText;
                        errorBox.hidden = false;
                    }
                    setWebsiteStatus(errorText, true);
                    return;
                }
                const snapshots = captureGalleryPostForms(
                    currentLibrary,
                    action === "remove_asset" ? "" : key
                );
                currentLibrary.replaceWith(incomingLibrary);
                restoreGalleryPostForms(incomingLibrary, snapshots);
                const defaultSuccessText = action === "remove_asset"
                    ? "Removed from Publish Content. The original and any Client Media share remain unchanged."
                    : action === "publish"
                        ? "Project post published with its photos and videos."
                        : action === "unpublish"
                            ? "Post unpublished. Its original photos and videos are retained."
                            : action === "delete"
                                ? "Draft post deleted. Original files and Client Media shares are unchanged."
                                : hasFiles ? "Post draft and media saved." : "Post draft saved. The live post is unchanged.";
                const successText = toastContent && toastContent.textContent.trim() || defaultSuccessText;
                setWebsiteStatus(successText, false);
            } catch (error) {
                setWebsiteStatus("Connection lost. Your post edits and selected files are still in the form; retry when connected.", true);
            } finally {
                websiteMutation.busy = false;
                if (form.isConnected) form.removeAttribute("aria-busy");
                if (submitter && submitter.isConnected && "disabled" in submitter) submitter.disabled = false;
            }
        });
        document.addEventListener("click", function (event) {
            const button = event.target.closest && event.target.closest("[data-gallery-prev], [data-gallery-next]");
            if (!button) return;
            const carousel = button.closest("[data-gallery-carousel]");
            if (!carousel) return;
            if (carousel.dataset.galleryAnimating === "true") return;
            const slides = Array.from(carousel.querySelectorAll("[data-gallery-slide]"));
            if (slides.length < 2) return;
            const current = Math.max(0, slides.findIndex(function (slide) { return !slide.hidden; }));
            const offset = button.matches("[data-gallery-next]") ? 1 : -1;
            const next = (current + offset + slides.length) % slides.length;
            const previousSlide = slides[current];
            const nextSlide = slides[next];
            const enteringClass = offset > 0 ? "is-gallery-entering-next" : "is-gallery-entering-prev";
            const leavingClass = offset > 0 ? "is-gallery-leaving-prev" : "is-gallery-leaving-next";
            previousSlide.setAttribute("aria-hidden", "true");
            nextSlide.setAttribute("aria-hidden", "false");
            const previousVideo = previousSlide.querySelector("video");
            if (previousVideo) previousVideo.pause();
            carousel.dataset.galleryIndex = String(next);
            const counter = carousel.querySelector("[data-gallery-counter]");
            if (counter) counter.textContent = (next + 1) + " / " + slides.length;

            if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
                previousSlide.hidden = true;
                nextSlide.hidden = false;
                return;
            }

            carousel.dataset.galleryAnimating = "true";
            nextSlide.hidden = false;
            nextSlide.classList.add(enteringClass);
            void nextSlide.offsetWidth;
            let transitionFinished = false;
            let transitionFallback;
            const finishTransition = function () {
                if (transitionFinished) return;
                transitionFinished = true;
                window.clearTimeout(transitionFallback);
                nextSlide.removeEventListener("transitionend", onTransitionEnd);
                previousSlide.hidden = true;
                previousSlide.classList.remove(leavingClass);
                nextSlide.classList.remove(enteringClass);
                carousel.dataset.galleryAnimating = "false";
            };
            const onTransitionEnd = function (transitionEvent) {
                if (transitionEvent.target === nextSlide && transitionEvent.propertyName === "transform") {
                    finishTransition();
                }
            };
            nextSlide.addEventListener("transitionend", onTransitionEnd);
            transitionFallback = window.setTimeout(finishTransition, 500);
            window.requestAnimationFrame(function () {
                if (transitionFinished) return;
                previousSlide.classList.add(leavingClass);
                nextSlide.classList.remove(enteringClass);
            });
        });
    }

    function initClientMediaUploadFields(root) {
        each("form[data-publication-upload]", function (form) {
            if (form.dataset.clientUploadFieldsBound === "true") return;
            var input = form.querySelector('input[type="file"]');
            var list = form.querySelector("[data-upload-file-list]");
            var status = form.querySelector("[data-upload-status]");
            if (!input || !list) return;
            form.dataset.clientUploadFieldsBound = "true";
            var chosenFiles = Array.from(input.files || []);

            function renderFiles() {
                list.replaceChildren();
                if (!chosenFiles.length) {
                    var empty = document.createElement("span");
                    empty.textContent = "No files selected.";
                    list.appendChild(empty);
                    return;
                }
                chosenFiles.forEach(function (file, index) {
                    var row = document.createElement("div");
                    row.className = "publication-file-row";
                    var label = document.createElement("span");
                    label.textContent = file.name + " · " + (file.size / 1024 / 1024).toFixed(1) + " MB";
                    var remove = document.createElement("button");
                    remove.type = "button";
                    remove.className = "text-link-button";
                    remove.textContent = "Remove";
                    remove.setAttribute("aria-label", "Remove " + file.name + " from this upload");
                    remove.addEventListener("click", function () {
                        if (typeof DataTransfer !== "function") {
                            if (status) status.textContent = "This browser cannot remove one file. Clear the selection and choose files again.";
                            return;
                        }
                        chosenFiles.splice(index, 1);
                        var transfer = new DataTransfer();
                        chosenFiles.forEach(function (selected) { transfer.items.add(selected); });
                        input.files = transfer.files;
                        renderFiles();
                    });
                    row.append(label, remove);
                    list.appendChild(row);
                });
            }

            input.addEventListener("change", function () {
                chosenFiles = Array.from(input.files || []);
                if (status) status.textContent = "";
                renderFiles();
            });
            renderFiles();
        }, root);
    }
    function init() {
        // Bind the admin-wide POST handler first so other optional UI setup cannot
        // leave ordinary save and upload forms falling back to full-page submits.
        initAdminAsyncForms();
        initMediaPublishing();
        initAdminWorkspaceContent();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
}());
