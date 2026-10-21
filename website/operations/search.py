from __future__ import annotations

from urllib.parse import urlencode

from django.db.models import Q
from django.http import HttpRequest
from django.urls import reverse
from unfold.dataclasses import SearchResult

from .models import (
    Estimate,
    EstimateLineItem,
    Client,
    ClientMessage,
    EmployeeProfile,
    ProjectDocument,
    ScheduleEvent,
    Task,
    Lead,
    LeadAttachment,
    MediaAsset,
    Milestone,
    ProcessStep,
    Project,
    ProjectUpdate,
    Service,
    SiteSettings,
)


SEARCH_LIMIT = 20


def _contains(term: str, *fields: str) -> Q:
    query = Q()
    for field in fields:
        query |= Q(**{f'{field}__icontains': term})
    return query


def _dashboard_url(section: str, **params: object) -> str:
    url = reverse('operations:dashboard-section', kwargs={'section': section})
    if params:
        return f'{url}?{urlencode({key: str(value) for key, value in params.items()})}'
    return url


def _result(title: object, description: str, link: str, icon: str) -> SearchResult:
    return SearchResult(
        title=str(title),
        description=description,
        link=link,
        icon=icon,
    )


def _estimate_client(estimate: Estimate) -> str:
    return estimate.client.name if estimate.client else 'Unassigned client'


def _project_location(project: Project) -> str:
    return project.location or 'Location not set'


def _media_project(media: MediaAsset) -> str:
    return media.project.title if media.project else 'Unassigned project'


def _search_leads(term: str) -> list[SearchResult]:
    leads = (
        Lead.objects.select_related('client')
        .filter(
            deleted_at__isnull=True,
        )
        .filter(
            _contains(
                term,
                'name',
                'email',
                'phone',
                'service',
                'location',
                'budget',
                'timeline',
                'source',
                'note',
                'client__name',
                'client__email',
                'client__company',
            )
        )
        .order_by('-priority', '-created_at')[:SEARCH_LIMIT]
    )
    return [
        _result(
            lead.name,
            f'Lead · {lead.service} · {lead.location}',
            _dashboard_url('leads', lead=lead.pk),
            'person_search',
        )
        for lead in leads
    ]


def _search_tasks(term: str) -> list[SearchResult]:
    tasks = (
        Task.objects.select_related('lead', 'lead__client', 'project', 'project__client').prefetch_related('assigned_to')
        .filter(
            Q(lead__isnull=True) | Q(lead__deleted_at__isnull=True),
        )
        .filter(
            _contains(
                term,
                'title',
                'lead__name',
                'lead__email',
                'lead__client__name',
                'lead__client__email',
                'description',
                'project__title',
                'project__client__name',
                'project__client__email',
                'assigned_to__username',
                'assigned_to__first_name',
                'assigned_to__last_name',
            )
        )
        .order_by('-created_at')[:SEARCH_LIMIT]
    )
    return [
        _result(
            task.title,
            f'Task · {task.project.title if task.project else task.lead.name if task.lead else "Unlinked work"}',
            _dashboard_url('tasks', task=task.pk),
            'event_upcoming',
        )
        for task in tasks
    ]


def _search_clients(term: str) -> list[SearchResult]:
    clients = Client.objects.filter(_contains(term, 'name', 'company', 'email', 'phone')).order_by('name')[:SEARCH_LIMIT]
    return [
        _result(client.name, f'Client \u00b7 {client.company or client.email}', _dashboard_url('clients', client=client.pk), 'groups')
        for client in clients
    ]


def _search_employees(term: str) -> list[SearchResult]:
    profiles = EmployeeProfile.objects.select_related('user').filter(
        _contains(term, 'job_title', 'phone', 'user__username', 'user__email', 'user__first_name', 'user__last_name')
    ).order_by('user__first_name')[:SEARCH_LIMIT]
    return [
        _result(profile.user.get_full_name() or profile.user.username, f'Employee \u00b7 {profile.job_title}', _dashboard_url('team'), 'badge')
        for profile in profiles
    ]


def _search_lead_attachments(term: str) -> list[SearchResult]:
    attachments = (
        LeadAttachment.objects.select_related('lead')
        .filter(
            lead__deleted_at__isnull=True,
        )
        .filter(
            _contains(
                term,
                'original_name',
                'file',
                'lead__name',
                'lead__email',
            )
        )
        .order_by('-created_at')[:SEARCH_LIMIT]
    )
    return [
        _result(
            attachment.original_name or attachment.file.name.rsplit('/', 1)[-1],
            f'Lead attachment · {attachment.lead.name}',
            _dashboard_url('leads', lead=attachment.lead.pk),
            'attach_file',
        )
        for attachment in attachments
    ]


def _search_schedule(term: str) -> list[SearchResult]:
    events = ScheduleEvent.objects.select_related('project', 'task').filter(
        _contains(term, 'title', 'location', 'notes', 'project__title', 'task__title')
    ).order_by('-start_at')[:SEARCH_LIMIT]
    return [
        _result(event.title, f'Calendar \u00b7 {event.project.title if event.project else "Internal event"}', _dashboard_url('calendar', event=event.pk), 'calendar_month')
        for event in events
    ]


def _search_estimates(term: str) -> list[SearchResult]:
    query = _contains(
        term,
        'title',
        'notes',
        'lead__name',
        'lead__email',
        'lead__service',
        'client__name',
        'client__email',
        'client__company',
    )
    if term.isdigit():
        query |= Q(number=int(term))

    estimates = (
        Estimate.objects.select_related('lead', 'client')
        .filter(query)
        .order_by('-created_at')[:SEARCH_LIMIT]
    )
    return [
        _result(
            f'Estimate #{estimate.number}',
            f'Estimate · {estimate.title} · {_estimate_client(estimate)}',
            _dashboard_url('estimates', estimate=estimate.pk),
            'request_quote',
        )
        for estimate in estimates
    ]


def _search_line_items(term: str) -> list[SearchResult]:
    line_items = (
        EstimateLineItem.objects.select_related(
            'estimate',
            'estimate__client',
        )
        .filter(
            _contains(
                term,
                'description',
                'estimate__title',
                'estimate__client__name',
                'estimate__client__email',
            )
        )
        .order_by('-estimate__created_at', 'sort_order')[:SEARCH_LIMIT]
    )
    return [
        _result(
            line_item.description,
            f'Estimate #{line_item.estimate.number} · Line item',
            _dashboard_url('estimates', estimate=line_item.estimate.pk),
            'format_list_bulleted',
        )
        for line_item in line_items
    ]


def _search_documents(term: str) -> list[SearchResult]:
    documents = ProjectDocument.objects.select_related('project').filter(
        _contains(term, 'title', 'category', 'description', 'file', 'project__title')
    ).order_by('-created_at')[:SEARCH_LIMIT]
    return [
        _result(document.title, f'Document \u00b7 {document.project.title}', _dashboard_url('documents', project=document.project.pk), 'description')
        for document in documents
    ]


def _search_projects(term: str) -> list[SearchResult]:
    projects = (
        Project.objects.select_related('client', 'lead')
        .filter(
            _contains(
                term,
                'title',
                'location',
                'project_type',
                'next_step',
                'summary',
                'client__name',
                'client__email',
                'client__company',
                'lead__name',
                'lead__email',
            )
        )
        .order_by('-updated_at', 'title')[:SEARCH_LIMIT]
    )
    return [
        _result(
            project.title,
            f'Project · {_project_location(project)}',
            _dashboard_url('projects', project=project.pk),
            'engineering',
        )
        for project in projects
    ]


def _search_milestones(term: str) -> list[SearchResult]:
    milestones = (
        Milestone.objects.select_related('project')
        .filter(_contains(term, 'title', 'project__title', 'project__location'))
        .order_by('-project__updated_at', 'sort_order')[:SEARCH_LIMIT]
    )
    return [
        _result(
            milestone.title,
            f'Milestone · {milestone.project.title}',
            _dashboard_url('projects', project=milestone.project.pk),
            'task_alt',
        )
        for milestone in milestones
    ]


def _search_project_updates(term: str) -> list[SearchResult]:
    updates = (
        ProjectUpdate.objects.select_related('project')
        .filter(_contains(term, 'title', 'body', 'project__title', 'project__location'))
        .order_by('-created_at')[:SEARCH_LIMIT]
    )
    return [
        _result(
            update.title,
            f'Project update · {update.project.title}',
            _dashboard_url('projects', project=update.project.pk),
            'update',
        )
        for update in updates
    ]


def _search_messages(term: str) -> list[SearchResult]:
    client_messages = ClientMessage.objects.select_related('client', 'project').filter(
        _contains(term, 'body', 'client__name', 'client__email', 'project__title')
    ).order_by('-created_at')[:SEARCH_LIMIT]
    return [
        _result(message.client.name, f'Message \u00b7 {message.project.title if message.project else "Client conversation"}', _dashboard_url('clients', client=message.client.pk), 'chat')
        for message in client_messages
    ]


def _search_media(term: str) -> list[SearchResult]:
    media_assets = (
        MediaAsset.objects.select_related('project')
        .filter(
            _contains(
                term,
                'title',
                'caption',
                'file',
                'project__title',
                'project__location',
            )
        )
        .order_by('-created_at')[:SEARCH_LIMIT]
    )
    return [
        _result(
            media.title,
            f'Media · {_media_project(media)}',
            _dashboard_url('media', media_project=media.project.pk if media.project else 'all'),
            'perm_media',
        )
        for media in media_assets
    ]


def _search_services(term: str) -> list[SearchResult]:
    services = (
        Service.objects.filter(_contains(term, 'title', 'slug', 'description'))
        .order_by('sort_order', 'title')[:SEARCH_LIMIT]
    )
    return [
        _result(
            service.title,
            'Website content · Service',
            _dashboard_url('content'),
            'design_services',
        )
        for service in services
    ]


def _search_process_steps(term: str) -> list[SearchResult]:
    steps = (
        ProcessStep.objects.filter(_contains(term, 'key', 'title', 'description'))
        .order_by('sort_order')[:SEARCH_LIMIT]
    )
    return [
        _result(
            step.title,
            'Website content · Process step',
            _dashboard_url('content'),
            'format_list_numbered',
        )
        for step in steps
    ]


def _search_site_settings(term: str) -> list[SearchResult]:
    settings_records = (
        SiteSettings.objects.select_related('featured_project')
        .filter(
            _contains(
                term,
                'headline',
                'subheadline',
                'featured_title',
                'featured_body',
                'google_review_url',
                'featured_project__title',
            )
        )
        .order_by('id')[:SEARCH_LIMIT]
    )
    return [
        _result(
            str(site_settings),
            'Website content · Homepage',
            _dashboard_url('content'),
            'settings',
        )
        for site_settings in settings_records
    ]


def admin_search(request: HttpRequest, search_term: str) -> list[SearchResult]:
    if not (
        request.user.is_authenticated
        and request.user.is_active
        and request.user.is_staff
        and request.user.is_superuser
    ):
        return []

    term = (search_term or '').strip()
    if not term:
        return []

    results: list[SearchResult] = []
    for searcher in (
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
        results.extend(searcher(term))
    return results
