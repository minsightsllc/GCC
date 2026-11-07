from __future__ import annotations

from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Case, F, IntegerField, Max, Q, Value, When
from django.http import HttpRequest
from django.urls import reverse
from unfold.dataclasses import SearchResult

from .models import (
    Client,
    ClientMessage,
    EmployeeProfile,
    Estimate,
    EstimateLineItem,
    Lead,
    LeadAttachment,
    MediaAsset,
    Milestone,
    ProcessStep,
    Project,
    ProjectDocument,
    ProjectUpdate,
    ScheduleEvent,
    Service,
    SiteSettings,
    Task,
)


SEARCH_LIMIT = 20


def _tokens(term: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(token.casefold() for token in (term or "").split() if token))


def _any_token(tokens: tuple[str, ...], fields: tuple[str, ...]) -> Q:
    query = Q()
    for token in tokens:
        for field in fields:
            query |= Q(**{f"{field}__icontains": token})
    return query


def _word_start(token: str, fields: tuple[str, ...]) -> Q:
    query = Q()
    for field in fields:
        query |= Q(**{f"{field}__istartswith": token})
        query |= Q(**{f"{field}__icontains": f" {token}"})
    return query


def _ranked_queryset(
    queryset,
    term: str,
    *,
    fields: tuple[str, ...],
    primary_fields: tuple[str, ...],
    ordering: tuple[str, ...],
    limit: int | None = SEARCH_LIMIT,
    extra_match: Q | None = None,
    extra_token_matches: dict[str, Q] | None = None,
    extra_exact: Q | None = None,
):
    normalized = (term or "").strip().casefold()
    tokens = _tokens(normalized)
    if not tokens:
        return queryset.none()

    matches = _any_token(tokens, fields)
    if extra_match is not None:
        matches |= extra_match

    exact = Q()
    phrase_prefix = Q()
    for field in primary_fields:
        exact |= Q(**{f"{field}__iexact": normalized})
        phrase_prefix |= Q(**{f"{field}__istartswith": normalized})
    if extra_exact is not None:
        exact |= extra_exact

    extra_token_matches = extra_token_matches or {}
    annotations = {
        "gcc_search_exact": Case(
            When(exact, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
        "gcc_search_phrase_prefix": Case(
            When(phrase_prefix, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    }
    token_flags = []
    word_start_flags = []
    for index, token in enumerate(tokens):
        token_query = _any_token((token,), fields)
        if token in extra_token_matches:
            token_query |= extra_token_matches[token]

        token_flag = f"gcc_search_token_{index}"
        annotations[token_flag] = Max(
            Case(
                When(token_query, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            output_field=IntegerField(),
        )
        token_flags.append(token_flag)

        word_flag = f"gcc_search_word_start_{index}"
        annotations[word_flag] = Max(
            Case(
                When(_word_start(token, fields), then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            output_field=IntegerField(),
        )
        word_start_flags.append(word_flag)

    ranked = (
        queryset.filter(matches)
        .annotate(**annotations)
        .annotate(
            gcc_search_token_count=sum((F(flag) for flag in token_flags), Value(0)),
            gcc_search_word_start_count=sum(
                (F(flag) for flag in word_start_flags), Value(0)
            ),
        )
        .order_by(
            "-gcc_search_exact",
            "-gcc_search_phrase_prefix",
            "-gcc_search_token_count",
            "-gcc_search_word_start_count",
            *ordering,
        )
    )
    return ranked[:limit] if limit is not None else ranked


def _score(record) -> tuple[int, int, int, int]:
    return (
        int(record.gcc_search_exact or 0),
        int(record.gcc_search_phrase_prefix or 0),
        int(record.gcc_search_token_count or 0),
        int(record.gcc_search_word_start_count or 0),
    )


def _result(title, description: str, link: str, icon: str) -> SearchResult:
    return SearchResult(
        title=str(title),
        description=description,
        link=link,
        icon=icon,
    )


def _dashboard_url(section: str, **params: object) -> str:
    url = reverse("operations:dashboard-section", kwargs={"section": section})
    if params:
        return f'{url}?{urlencode({key: str(value) for key, value in params.items()})}'
    return url


def _query(
    queryset,
    term: str,
    *,
    fields: tuple[str, ...],
    primary_fields: tuple[str, ...],
    ordering: tuple[str, ...],
    make_result,
    limit: int | None = SEARCH_LIMIT,
    extra_match: Q | None = None,
    extra_token_matches: dict[str, Q] | None = None,
    extra_exact: Q | None = None,
):
    records = _ranked_queryset(
        queryset,
        term,
        fields=fields,
        primary_fields=primary_fields,
        ordering=ordering,
        limit=limit,
        extra_match=extra_match,
        extra_token_matches=extra_token_matches,
        extra_exact=extra_exact,
    )
    return [(_score(record), make_result(record)) for record in records]


def _search_users(term: str):
    user_model = get_user_model()
    return _query(
        user_model.objects.filter(client_record__isnull=True),
        term,
        fields=("username", "first_name", "last_name", "email"),
        primary_fields=("username", "email", "first_name", "last_name"),
        ordering=("username",),
        limit=None,
        make_result=lambda user: _result(
            user,
            "Auth - User",
            reverse(
                f"admin:{user._meta.app_label}_{user._meta.model_name}_change",
                args=(user.pk,),
            ),
            "data_object",
        ),
    )


def _search_groups(term: str):
    return _query(
        Group.objects.all(),
        term,
        fields=("name",),
        primary_fields=("name",),
        ordering=("name",),
        limit=None,
        make_result=lambda group: _result(
            group,
            "Auth - Group",
            reverse(
                f"admin:{group._meta.app_label}_{group._meta.model_name}_change",
                args=(group.pk,),
            ),
            "data_object",
        ),
    )


def _search_leads(term: str):
    return _query(
        Lead.objects.select_related("client").filter(deleted_at__isnull=True),
        term,
        fields=(
            "name", "email", "phone", "service", "location", "budget", "timeline",
            "source", "note", "client__name", "client__email", "client__company",
        ),
        primary_fields=("name",),
        ordering=("-priority", "-created_at"),
        make_result=lambda lead: _result(
            lead.name,
            f"Lead · {lead.service} · {lead.location}",
            _dashboard_url("leads", lead=lead.pk),
            "person_search",
        ),
    )


def _search_clients(term: str):
    return _query(
        Client.objects.all(),
        term,
        fields=("name", "company", "email", "phone"),
        primary_fields=("name",),
        ordering=("name",),
        make_result=lambda client: _result(
            client.name,
            f"Client · {client.company or client.email}",
            _dashboard_url("clients", client=client.pk),
            "groups",
        ),
    )


def _search_employees(term: str):
    return _query(
        EmployeeProfile.objects.select_related("user"),
        term,
        fields=(
            "job_title", "phone", "user__username", "user__email",
            "user__first_name", "user__last_name",
        ),
        primary_fields=("user__first_name", "user__last_name", "user__username"),
        ordering=("user__first_name",),
        make_result=lambda profile: _result(
            profile.user.get_full_name() or profile.user.username,
            f"Employee · {profile.job_title}",
            _dashboard_url("team"),
            "badge",
        ),
    )


def _search_tasks(term: str):
    return _query(
        Task.objects.select_related(
            "lead", "lead__client", "project", "project__client"
        ).prefetch_related("assigned_to").filter(
            Q(lead__isnull=True) | Q(lead__deleted_at__isnull=True),
        ),
        term,
        fields=(
            "title", "lead__name", "lead__email", "lead__client__name",
            "lead__client__email", "description", "project__title",
            "project__client__name", "project__client__email",
            "assigned_to__username", "assigned_to__first_name", "assigned_to__last_name",
        ),
        primary_fields=("title",),
        ordering=("-created_at",),
        make_result=lambda task: _result(
            task.title,
            f'Task · {task.project.title if task.project else task.lead.name if task.lead else "Unlinked work"}',
            _dashboard_url("tasks", task=task.pk),
            "event_upcoming",
        ),
    )


def _search_lead_attachments(term: str):
    return _query(
        LeadAttachment.objects.select_related("lead").filter(
            lead__deleted_at__isnull=True,
        ),
        term,
        fields=("original_name", "file", "lead__name", "lead__email"),
        primary_fields=("original_name", "file"),
        ordering=("-created_at",),
        make_result=lambda attachment: _result(
            attachment.original_name or attachment.file.name.rsplit("/", 1)[-1],
            f"Lead attachment · {attachment.lead.name}",
            _dashboard_url("leads", lead=attachment.lead.pk),
            "attach_file",
        ),
    )


def _search_schedule(term: str):
    return _query(
        ScheduleEvent.objects.select_related("project", "task"),
        term,
        fields=("title", "location", "notes", "project__title", "task__title"),
        primary_fields=("title",),
        ordering=("-start_at",),
        make_result=lambda event: _result(
            event.title,
            f'Calendar · {event.project.title if event.project else "Internal event"}',
            _dashboard_url("calendar", event=event.pk),
            "calendar_month",
        ),
    )


def _search_estimates(term: str):
    extra_match = None
    extra_exact = None
    extra_token_matches = {}
    normalized = (term or "").strip()
    if normalized.isdigit():
        try:
            number_query = Q(number=int(normalized))
        except ValueError:
            pass
        else:
            extra_match = extra_exact = number_query
            extra_token_matches[normalized.casefold()] = number_query

    return _query(
        Estimate.objects.select_related("lead", "client"),
        term,
        fields=(
            "title", "notes", "lead__name", "lead__email", "lead__service",
            "client__name", "client__email", "client__company",
        ),
        primary_fields=("title",),
        ordering=("-created_at",),
        extra_match=extra_match,
        extra_token_matches=extra_token_matches,
        extra_exact=extra_exact,
        make_result=lambda estimate: _result(
            f"Estimate #{estimate.number}",
            f"Estimate · {estimate.title} · {estimate.client.name if estimate.client else 'Unassigned client'}",
            _dashboard_url("estimates", estimate=estimate.pk),
            "request_quote",
        ),
    )


def _search_line_items(term: str):
    return _query(
        EstimateLineItem.objects.select_related("estimate", "estimate__client"),
        term,
        fields=(
            "description", "estimate__title", "estimate__client__name",
            "estimate__client__email",
        ),
        primary_fields=("description",),
        ordering=("-estimate__created_at", "sort_order"),
        make_result=lambda line: _result(
            line.description,
            f"Estimate #{line.estimate.number} · Line item",
            _dashboard_url("estimates", estimate=line.estimate.pk),
            "format_list_bulleted",
        ),
    )


def _search_documents(term: str):
    return _query(
        ProjectDocument.objects.select_related("project"),
        term,
        fields=("title", "category", "description", "file", "project__title"),
        primary_fields=("title", "file"),
        ordering=("-created_at",),
        make_result=lambda document: _result(
            document.title,
            f"Document · {document.project.title}",
            _dashboard_url("documents", project=document.project.pk),
            "description",
        ),
    )


def _search_projects(term: str):
    return _query(
        Project.objects.filter(is_portfolio_only=False).select_related("client", "lead"),
        term,
        fields=(
            "title", "location", "project_type", "next_step", "summary",
            "client__name", "client__email", "client__company", "lead__name",
            "lead__email",
        ),
        primary_fields=("title",),
        ordering=("-updated_at", "title"),
        make_result=lambda project: _result(
            project.title,
            f"Project · {project.location or 'Location not set'}",
            _dashboard_url("projects", project=project.pk),
            "engineering",
        ),
    )


def _search_milestones(term: str):
    return _query(
        Milestone.objects.select_related("project"),
        term,
        fields=("title", "project__title", "project__location"),
        primary_fields=("title",),
        ordering=("-project__updated_at", "sort_order"),
        make_result=lambda milestone: _result(
            milestone.title,
            f"Milestone · {milestone.project.title}",
            _dashboard_url("projects", project=milestone.project.pk),
            "task_alt",
        ),
    )


def _search_project_updates(term: str):
    return _query(
        ProjectUpdate.objects.select_related("project"),
        term,
        fields=("title", "body", "project__title", "project__location"),
        primary_fields=("title",),
        ordering=("-created_at",),
        make_result=lambda update: _result(
            update.title,
            f"Project update · {update.project.title}",
            _dashboard_url("projects", project=update.project.pk),
            "update",
        ),
    )


def _search_messages(term: str):
    return _query(
        ClientMessage.objects.select_related("client", "project"),
        term,
        fields=("body", "client__name", "client__email", "project__title"),
        primary_fields=("body", "client__name"),
        ordering=("-created_at",),
        make_result=lambda message: _result(
            message.client.name,
            f'Message · {message.project.title if message.project else "Client conversation"}',
            _dashboard_url("clients", client=message.client.pk),
            "chat",
        ),
    )


def _search_media(term: str):
    return _query(
        MediaAsset.objects.select_related("project"),
        term,
        fields=("title", "caption", "file", "project__title", "project__location"),
        primary_fields=("title", "file"),
        ordering=("-created_at",),
        make_result=lambda media: _result(
            media.title,
            f"Media · {media.project.title if media.project else 'Unassigned project'}",
            _dashboard_url(
                "media",
                media_project=media.project.pk if media.project else "all",
            ),
            "perm_media",
        ),
    )


def _search_services(term: str):
    return _query(
        Service.objects.all(),
        term,
        fields=("title", "slug", "description"),
        primary_fields=("title",),
        ordering=("sort_order", "title"),
        make_result=lambda service: _result(
            service.title,
            "Website content · Service",
            _dashboard_url("content"),
            "design_services",
        ),
    )


def _search_process_steps(term: str):
    return _query(
        ProcessStep.objects.all(),
        term,
        fields=("key", "title", "description"),
        primary_fields=("title",),
        ordering=("sort_order",),
        make_result=lambda step: _result(
            step.title,
            "Website content · Process step",
            _dashboard_url("content"),
            "format_list_numbered",
        ),
    )


def _search_site_settings(term: str):
    return _query(
        SiteSettings.objects.select_related("featured_project"),
        term,
        fields=(
            "headline", "subheadline", "featured_title", "featured_body",
            "google_review_url", "featured_project__title",
        ),
        primary_fields=("headline", "featured_title"),
        ordering=("id",),
        make_result=lambda settings_record: _result(
            str(settings_record),
            "Website content · Homepage",
            _dashboard_url("content"),
            "settings",
        ),
    )


def admin_search(request: HttpRequest, search_term: str) -> list[SearchResult]:
    if not (
        request.user.is_authenticated
        and request.user.is_active
        and request.user.is_staff
        and request.user.is_superuser
    ):
        return []

    term = (search_term or "").strip()
    if not _tokens(term):
        return []

    ranked_results = []
    for searcher in (
        _search_users,
        _search_groups,
        _search_leads,
        _search_clients,
        _search_employees,
        _search_tasks,
        _search_lead_attachments,
        _search_estimates,
        _search_line_items,
        _search_projects,
        _search_milestones,
        _search_project_updates,
        _search_schedule,
        _search_documents,
        _search_messages,
        _search_media,
        _search_services,
        _search_process_steps,
        _search_site_settings,
    ):
        ranked_results.extend(searcher(term))

    # Stable sorting preserves each search category's legacy order when relevance ties.
    ranked_results.sort(key=lambda item: item[0], reverse=True)
    return [result for _, result in ranked_results]
