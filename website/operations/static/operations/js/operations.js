(function () {
    "use strict";

    function each(selector, callback, root) {
        (root || document).querySelectorAll(selector).forEach(callback);
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
        var match = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(value || "");
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
        var match = /^(\\d{2}):(\\d{2})$/.exec(value || "");
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
                state.month -= 1;
                if (state.month < 0) {
                    state.month = 11;
                    state.year -= 1;
                }
                render();
            });
            next.addEventListener("click", function (event) {
                event.preventDefault();
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
                        state[definition.key] = definition.key === "period" ? value : Number(value);
                        renderOptions();
                    });
                    options.appendChild(option);
                });
                column.appendChild(options);
                columns.appendChild(column);
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
            if (workingCheckbox) {
                workingCheckbox.addEventListener("change", syncFields);
            }
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

        each("[data-native-media-action]", function (button) {
            button.addEventListener("click", function (event) {
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
        });

        post({type: "native-media-page-ready"});
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
                fetch(form.action, {
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
                }).catch(function () {
                    form.submit();
                });
            });
        });

        each("[data-notification-clear]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.action, {
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
                }).catch(function () {
                    form.submit();
                });
            });
        });
        each("[data-notification-clear-all]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.action, {
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
                }).catch(function () {
                    form.submit();
                });
            });
        });

        each("[data-notification-read-all]", function (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                fetch(form.action, {
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
                }).catch(function () {
                    form.submit();
                });
            });
        });
    }

    function initCalendarDayForms() {
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
        });
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
        content.innerHTML = '<img class="modal-image" src="' + src.replace(/"/g, "&quot;") + '" alt="' + alt.replace(/"/g, "&quot;") + '">';
        backdrop.hidden = false;
        document.body.classList.add("modal-open");
    }

    function closeModal() {
        var backdrop = document.querySelector("[data-modal]");
        if (!backdrop) {
            return;
        }
        backdrop.hidden = true;
        document.body.classList.remove("modal-open");
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
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                closeModal();
            }
        });
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

    function init() {
        initNavigation();
        initLeadDetailTabs();
        initProjectFilters();
        initGoogleReviewsCarousel();
        initFileInputs();
        initCalendarDayForms();
        initEmployeeScheduleForms();
        initStandaloneCalendarPickers();
        initAdminIpBlockForms();
        initWorkerSelectors();
        initNotificationActions();
        initEstimateEditors();
        initEstimateDeliveryDialog();
        initModal();
        initToastDismissal();
        initNativeMediaBridge();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
}());
