from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import management
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client as DjangoClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import (
    LeadAssignmentForm,
    LeadForm,
    ProjectCreateForm,
    ProjectDocumentForm,
    ProjectForm,
    QuickTaskForm,
    CalendarDayOverrideForm,
    ScheduleEventForm,
    TaskForm,
)
from .models import (
    AdminRecoveryToken,
    AdminSecurityProfile,
    CALENDAR_TIME_ZONE,
    Activity,
    CalendarDayOverride,
    ClientNotification,
    Client as ClientRecord,
    ClientMessage,
    EmployeeInvite,
    EmployeeNotification,
    EmployeeProfile,
    Estimate,
    EstimateLineItem,
    Lead,
    MediaAsset,
    Milestone,
    MobilePushDevice,
    Project,
    ProjectDocument,
    ProjectUpdate,
    ScheduleEvent,
    SiteSettings,
    Task,
    TimeEntry,
)
from .services import (
    create_client_invite,
    create_employee_invite,
    ensure_role_groups,
    find_employee_invite,
)


TEST_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix='gcc-platform-tests-'))
TEST_STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
    'contact_submissions': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
}


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    STORAGES=TEST_STORAGES,
    GCC_TURNSTILE_ENABLED=False,
    CLOUDFLARE_TURNSTILE_SITE_KEY="",
    CLOUDFLARE_TURNSTILE_SECRET_KEY="",
)
class PlatformWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user(
            username='platform-owner',
            password='owner-pass-123',
            first_name='Platform',
            last_name='Owner',
            email='owner@gcc.example.com',
            is_staff=True,
            is_superuser=True,
        )
        cls.manager = user_model.objects.create_user(
            username='platform-manager',
            password='manager-pass-123',
            first_name='Project',
            last_name='Manager',
            email='manager@gcc.example.com',
            is_staff=True,
        )
        cls.office = user_model.objects.create_user(
            username='platform-office',
            password='office-pass-123',
            first_name='Office',
            last_name='Coordinator',
            email='office@gcc.example.com',
            is_staff=True,
        )
        cls.field = user_model.objects.create_user(
            username='platform-field',
            password='field-pass-123',
            first_name='Field',
            last_name='Lead',
            email='field@gcc.example.com',
            is_staff=True,
        )
        roles = ensure_role_groups()
        cls.owner.groups.add(roles['Owner'])
        cls.manager.groups.add(roles['Manager'])
        cls.office.groups.add(roles['Office'])
        cls.field.groups.add(roles['Field'])
        for user, title in (
            (cls.owner, 'Owner'),
            (cls.manager, 'Manager'),
            (cls.office, 'Office coordinator'),
            (cls.field, 'Field lead'),
        ):
            EmployeeProfile.objects.create(user=user, job_title=title, phone='805-555-0100')

        cls.client_user = user_model.objects.create_user(
            username='platform-client',
            password='client-pass-123',
            first_name='Maya',
            last_name='Thompson',
            email='maya.platform@example.com',
        )
        cls.other_client_user = user_model.objects.create_user(
            username='platform-other-client',
            password='client-pass-123',
            first_name='Jordan',
            last_name='Lee',
            email='jordan.platform@example.com',
        )
        cls.client_record = ClientRecord.objects.create(
            name='Maya Thompson',
            email=cls.client_user.email,
            user=cls.client_user,
        )
        cls.other_client_record = ClientRecord.objects.create(
            name='Jordan Lee',
            email=cls.other_client_user.email,
            user=cls.other_client_user,
        )
        cls.lead = Lead.objects.create(
            client=cls.client_record,
            name='Maya Thompson',
            email=cls.client_user.email,
            service='Renovations',
            location='Ventura, CA',
            status=Lead.Status.QUALIFIED,
            assigned_to=cls.manager,
            created_by=cls.owner,
        )
        cls.estimate = Estimate.objects.create(
            number=9801,
            lead=cls.lead,
            client=cls.client_record,
            title='Platform kitchen scope',
            status=Estimate.Status.ACCEPTED,
            deposit_amount=Decimal('125.00'),
            accepted_at=timezone.now() - timedelta(days=1),
            external_url='https://estimates.example.com/platform-kitchen',
            external_status=Estimate.ExternalEstimateStatus.APPROVED,
            external_status_at=timezone.now() - timedelta(days=1),
            external_status_by=cls.owner,
            external_client_visible=True,
            created_by=cls.owner,
        )
        EstimateLineItem.objects.create(
            estimate=cls.estimate,
            description='Cabinetry',
            quantity=Decimal('2.00'),
            unit_price=Decimal('100.00'),
        )
        cls.project = Project.objects.create(
            estimate=cls.estimate,
            lead=cls.lead,
            client=cls.client_record,
            title='Platform kitchen project',
            location='Ventura, CA',
            project_type='renovation',
            status=Project.Status.CONSTRUCTION,
            next_step='Complete rough-in',
            summary='A private test project.',
            fallback_image='operations/images/progress-kitchen.png',
            created_by=cls.owner,
        )
        cls.project.assigned_staff.add(cls.manager, cls.field)
        cls.milestone = Milestone.objects.create(
            project=cls.project,
            title='Rough-in',
            sort_order=1,
            is_complete=False,
        )
        cls.task = Task.objects.create(
            title='Field task for kitchen',
            description='Complete the field report.',
            project=cls.project,
            lead=cls.lead,
            milestone=cls.milestone,
            assigned_to=cls.field,
            priority=Task.Priority.HIGH,
            due_date=timezone.localdate(),
            created_by=cls.manager,
        )
        cls.other_project = Project.objects.create(
            client=cls.other_client_record,
            title='Other private project',
            location='Santa Barbara, CA',
            project_type='residential',
            status=Project.Status.PLANNING,
            fallback_image='operations/images/project-adu.png',
            created_by=cls.owner,
        )
        cls.other_task = Task.objects.create(
            title='Other client task',
            project=cls.other_project,
            assigned_to=cls.manager,
            created_by=cls.manager,
        )
        cls.field_event = ScheduleEvent.objects.create(
            title='Kitchen field visit',
            project=cls.project,
            task=cls.task,
            start_at=timezone.now() + timedelta(days=1),
            end_at=timezone.now() + timedelta(days=1, hours=2),
            location='Ventura site',
            created_by=cls.manager,
        )
        cls.field_event.assignees.add(cls.field)
        cls.manager_event = ScheduleEvent.objects.create(
            title='Manager planning review',
            project=cls.other_project,
            start_at=timezone.now() + timedelta(days=2),
            end_at=timezone.now() + timedelta(days=2, hours=1),
            created_by=cls.manager,
        )
        cls.manager_event.assignees.add(cls.manager)
        cls.public_media = MediaAsset.objects.create(
            project=cls.project,
            title='Public kitchen image',
            fallback_image='operations/images/hero-kitchen.png',
            visibility=MediaAsset.Visibility.PUBLIC,
            uploaded_by=cls.owner,
        )
        cls.internal_media = MediaAsset.objects.create(
            project=cls.project,
            title='Internal field reference',
            fallback_image='operations/images/project-bathroom.png',
            visibility=MediaAsset.Visibility.INTERNAL,
            uploaded_by=cls.owner,
        )
        SiteSettings.objects.create(
            headline='Build with confidence.',
            subheadline='A clear path.',
            featured_title='Platform work',
            featured_body='Thoughtful construction.',
            featured_project=cls.project,
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.browser = DjangoClient()

    def login(self, user):
        self.browser.logout()
        self.browser.force_login(user)
        if user.is_superuser:
            self.browser.post(
                reverse('admin:access'),
                {'identifier': user.username},
            )

    @staticmethod
    def pacific_datetime(day, hour, minute=0):
        return timezone.make_aware(
            datetime.combine(day, time(hour, minute)),
            timezone.get_current_timezone(),
        )

    def assert_operations_badges(self, response, role, keys):
        self.assertEqual(response.context['operations_nav_role'], role)
        counts = response.context['operations_nav_counts']
        markup = response.content.decode()
        self.assertIn(f'data-workspace-kind="{role}"', markup)
        nav_start = markup.index('<nav class="admin-nav grouped-admin-nav"')
        nav_markup = markup[nav_start:markup.index('</nav>', nav_start)]
        actual_badge_values = re.findall(
            r'<span class="(?:nav-count|notification-nav-count)"[^>]*>(\d+)</span>',
            nav_markup,
        )
        expected_badge_values = [str(counts[key]) for key in keys if counts[key] > 0]
        self.assertEqual(actual_badge_values, expected_badge_values)
        for key in keys:
            self.assertIsInstance(counts[key], int)
            self.assertIn(f'data-count-{key}="{counts[key]}"', markup)

    def assert_sidebar_sequence(self, response, tokens):
        markup = response.content.decode()
        start = markup.index('<nav class="admin-nav grouped-admin-nav"')
        sidebar = markup[start:markup.index('</nav>', start)]
        positions = [sidebar.index(token) for token in tokens]
        self.assertEqual(positions, sorted(positions))

    def test_admin_operations_sidebar_hides_zero_counts_and_keeps_positive_counts(self):
        self.login(self.owner)

        dashboard_response = self.browser.get(reverse('operations:dashboard'))
        admin_keys = (
            'overview', 'leads', 'clients', 'estimates', 'projects', 'tasks',
            'calendar', 'time', 'documents', 'media', 'team', 'messages',
            'notifications', 'content',
        )
        self.assert_operations_badges(dashboard_response, 'admin', admin_keys)
        self.assert_sidebar_sequence(
            dashboard_response,
            (
                '>Overview<', 'Client &amp; Job Operations', '>Leads<',
                '>Clients<', '>Estimates<', '>Projects<', '>Tasks<',
                '>Calendar<', '>Time<', '>Documents<', '>Media<',
                'Miscellaneous', '>Team<', '>Messages<', '>Notifications<', '>Content<',
            ),
        )

        workspace_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'tasks'})
        )
        self.assert_operations_badges(workspace_response, 'admin', admin_keys)
        self.assert_sidebar_sequence(
            workspace_response,
            ('>Overview<', 'Client &amp; Job Operations', '>Leads<', '>Clients<', '>Estimates<', '>Projects<', '>Tasks<', '>Calendar<', '>Time<', '>Documents<', '>Media<', 'Miscellaneous', '>Team<', '>Messages<', '>Notifications<', '>Content<'),
        )

    def test_employee_operations_sidebar_hides_zero_counts_and_keeps_positive_counts(self):
        self.login(self.field)

        response = self.browser.get(reverse('operations:team'))
        employee_keys = ('overview', 'projects', 'tasks', 'calendar', 'time', 'media', 'notifications', 'profile')
        self.assert_operations_badges(response, 'employee', employee_keys)
        self.assert_sidebar_sequence(
            response,
            ('>Overview<', 'Client &amp; Job Operations', '>Projects<', '>Tasks<', '>Calendar<', '>Time<', '>Media<', 'Miscellaneous', '>Notifications<', '>Profile<'),
        )
        self.assertEqual(
            response.context['operations_nav_counts']['projects'],
            Project.objects.filter(assigned_staff=self.field)
            .exclude(status=Project.Status.COMPLETE)
            .distinct()
            .count(),
        )
        self.assertEqual(response.context['operations_nav_counts']['leads'], 0)

    def test_client_portal_navigation_hides_zero_and_shows_positive_unread_count(self):
        self.login(self.client_user)
        overview_url = reverse('operations:portal-section', kwargs={'section': 'overview'})

        response = self.browser.get(overview_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Notifications')
        self.assertNotContains(response, 'Notifications (0)')
        self.assertContains(response, 'data-count-estimates="0"')
        self.assertContains(response, 'data-count-messages="0"')
        self.assertContains(response, 'data-count-notifications="0"')
        counts_url = reverse('operations:client-mobile-navigation-counts')
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 0, 'messages': 0, 'notifications': 0})

        pending_estimate = Estimate.objects.create(
            number=9901,
            client=self.client_record,
            title='Portal estimate ready for review',
            status=Estimate.Status.SENT,
            external_url='https://estimates.example.com/portal-review',
            external_status=Estimate.ExternalEstimateStatus.SENT,
            external_client_visible=True,
            created_by=self.owner,
        )

        ClientNotification.objects.create(
            client=self.client_record,
            kind='test',
            title='Estimate update',
            body='An estimate is ready to review.',
            destination_url='/portal/notifications/',
            created_by=self.owner,
            estimate=pending_estimate,
        )
        response = self.browser.get(overview_url)
        self.assertContains(response, 'Notifications (1)')
        self.assertContains(response, 'data-count-estimates="1"')
        self.assertContains(response, 'data-count-messages="0"')
        self.assertContains(response, 'data-count-notifications="1"')
        Estimate.objects.create(
            number=9902,
            client=self.other_client_record,
            title='Another client portal estimate',
            status=Estimate.Status.SENT,
            external_url='https://estimates.example.com/other-client-review',
            external_status=Estimate.ExternalEstimateStatus.SENT,
            external_client_visible=True,
            created_by=self.owner,
        )
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 1, 'messages': 0, 'notifications': 1})
        self.assertEqual(
            self.browser.get(counts_url, {'client': self.other_client_record.pk}).json(),
            {'estimates': 1, 'messages': 0, 'notifications': 1},
        )

        ClientNotification.objects.filter(client=self.client_record).update(read_at=timezone.now())
        response = self.browser.get(overview_url)
        self.assertContains(response, 'data-count-estimates="1"')
        self.assertContains(response, 'data-count-messages="0"')
        self.assertContains(response, 'data-count-notifications="0"')
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 1, 'messages': 0, 'notifications': 0})

        pending_estimate.external_status = Estimate.ExternalEstimateStatus.APPROVED
        pending_estimate.save(update_fields=['external_status', 'updated_at'])
        response = self.browser.get(overview_url)
        self.assertContains(response, 'data-count-estimates="0"')
        self.assertContains(response, 'data-count-messages="0"')
        self.assertContains(response, 'data-count-notifications="0"')
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 0, 'messages': 0, 'notifications': 0})

    def test_client_team_message_count_clears_only_after_messages_tab_opens(self):
        message = ClientMessage.objects.create(
            client=self.client_record,
            project=self.project,
            body='The team has a schedule update.',
            sent_by=self.owner,
        )
        notification = ClientNotification.objects.create(
            client=self.client_record,
            project=self.project,
            message=message,
            kind='employee-reply',
            title='Grand Coast replied to your message',
            body='A member of the Grand Coast team replied in your client portal.',
            destination_url=f"{reverse('operations:portal')}?client={self.client_record.pk}",
            created_by=self.owner,
        )
        self.login(self.client_user)
        notifications_url = reverse('operations:portal-section', kwargs={'section': 'notifications'})
        messages_url = reverse('operations:portal-section', kwargs={'section': 'messages'})
        counts_url = reverse('operations:client-mobile-navigation-counts')
        expected_message_url = f'{messages_url}?client={self.client_record.pk}&project={self.project.pk}'

        # Visiting other portal pages and the notification inbox does not mark the message read.
        self.assertEqual(self.browser.get(reverse('operations:portal')).status_code, 200)
        inbox_response = self.browser.get(notifications_url)
        self.assertContains(inbox_response, 'class="notification-card is-unread"')
        self.assertContains(inbox_response, 'data-count-messages="1"')
        self.assertContains(inbox_response, 'Open related work')
        self.assertNotContains(inbox_response, '>Mark read<')
        self.assertEqual(
            inbox_response.context['client_notifications'][0].destination_url,
            expected_message_url,
        )
        message.refresh_from_db()
        notification.refresh_from_db()
        self.assertFalse(message.is_read)
        self.assertIsNone(notification.read_at)
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 0, 'messages': 1, 'notifications': 1})

        # Bulk notification reading must leave the underlying message unread.
        self.browser.post(reverse('operations:client-notifications-mark-all-read'))
        message.refresh_from_db()
        notification.refresh_from_db()
        self.assertFalse(message.is_read)
        self.assertIsNone(notification.read_at)
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 0, 'messages': 1, 'notifications': 1})

        # The message notification's explicit read action leads to the actual conversation.
        mark_read_response = self.browser.post(
            reverse('operations:client-notification-mark-read', kwargs={'pk': notification.pk}),
        )
        self.assertEqual(mark_read_response.status_code, 302)
        self.assertEqual(mark_read_response.url, expected_message_url)
        messages_response = self.browser.get(mark_read_response.url)
        self.assertEqual(messages_response.status_code, 200)
        self.assertContains(messages_response, 'data-count-messages="0"')
        message.refresh_from_db()
        notification.refresh_from_db()
        self.assertTrue(message.is_read)
        self.assertIsNotNone(notification.read_at)
        self.assertEqual(self.browser.get(counts_url).json(), {'estimates': 0, 'messages': 0, 'notifications': 0})

    def test_owner_can_clear_one_notification_without_deleting_history(self):
        first = EmployeeNotification.objects.create(
            employee=self.owner,
            kind='test',
            title='First test alert',
            body='Dismiss this one.',
            created_by=self.owner,
        )
        second = EmployeeNotification.objects.create(
            employee=self.owner,
            kind='test',
            title='Second test alert',
            body='Keep this one visible.',
            created_by=self.owner,
        )
        self.login(self.owner)
        inbox_url = reverse('operations:dashboard-section', kwargs={'section': 'notifications'})
        response = self.browser.get(inbox_url)
        self.assertContains(response, first.title)
        self.assertContains(response, second.title)

        response = self.browser.post(
            reverse('operations:dashboard-notification-clear', kwargs={'pk': first.pk}),
        )
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertIsNotNone(first.dismissed_at)
        self.assertIsNotNone(first.read_at)
        self.assertTrue(EmployeeNotification.objects.filter(pk=first.pk).exists())

        response = self.browser.get(inbox_url)
        self.assertNotContains(response, first.title)
        self.assertContains(response, second.title)

    def test_owner_can_clear_all_notifications_for_their_inbox(self):
        notifications = [
            EmployeeNotification.objects.create(
                employee=self.owner,
                kind='test',
                title=f'Bulk test alert {index}',
                body='Dismiss this alert.',
                created_by=self.owner,
            )
            for index in (1, 2)
        ]
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:dashboard-notifications-clear-all'),
        )
        self.assertEqual(response.status_code, 302)
        for notification in notifications:
            notification.refresh_from_db()
            self.assertIsNotNone(notification.dismissed_at)
            self.assertIsNotNone(notification.read_at)
        response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'notifications'}),
        )
        self.assertContains(response, "all caught up.", html=False)
        for notification in notifications:
            self.assertNotContains(response, notification.title)

    def test_owner_cannot_clear_another_users_notification(self):
        notification = EmployeeNotification.objects.create(
            employee=self.manager,
            kind='test',
            title='Manager-only test alert',
            body='This must remain private.',
            created_by=self.manager,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:dashboard-notification-clear', kwargs={'pk': notification.pk}),
        )
        self.assertEqual(response.status_code, 404)
        notification.refresh_from_db()
        self.assertIsNone(notification.dismissed_at)
        self.assertIsNone(notification.read_at)

    def test_assignment_selectors_show_staff_full_names(self):
        staff_queryset = get_user_model().objects.filter(pk=self.manager.pk)
        forms_and_fields = (
            (LeadForm(staff_queryset=staff_queryset), ('assigned_to',)),
            (QuickTaskForm(staff_queryset=staff_queryset), ('assigned_to',)),
            (TaskForm(staff_queryset=staff_queryset), ('assigned_to', 'watchers')),
            (LeadAssignmentForm(staff_queryset=staff_queryset), ('assigned_to',)),
            (ProjectForm(staff_queryset=staff_queryset), ('assigned_staff',)),
            (ScheduleEventForm(staff_queryset=staff_queryset), ('assignees',)),
        )

        for form, field_names in forms_and_fields:
            for field_name in field_names:
                choices = {
                    str(value): label
                    for value, label in form.fields[field_name].choices
                }
                self.assertEqual(choices[str(self.manager.pk)], 'Project Manager')
                self.assertNotIn('platform-manager', choices.values())

    def test_roles_are_idempotent_and_field_isolated(self):
        ensure_role_groups()
        ensure_role_groups()
        self.assertEqual(Group.objects.filter(name__in=['Owner', 'Manager', 'Office', 'Field']).count(), 4)

        self.login(self.field)
        self.assertEqual(self.browser.get(reverse('operations:dashboard')).status_code, 403)
        team_response = self.browser.get(reverse('operations:team'))
        self.assertEqual(team_response.status_code, 200)
        self.assertContains(team_response, self.project.title)
        self.assertNotContains(team_response, self.other_project.title)
        task_response = self.browser.get(reverse('operations:team-section', kwargs={'section': 'tasks'}))
        self.assertContains(task_response, self.task.title)
        self.assertNotContains(task_response, self.other_task.title)
        calendar_response = self.browser.get(reverse('operations:team-section', kwargs={'section': 'calendar'}))
        self.assertContains(calendar_response, self.field_event.title)
        self.assertNotContains(calendar_response, self.manager_event.title)
        self.assertEqual(
            self.browser.get(reverse('admin:index')).status_code,
            403,
        )

    def test_only_active_superusers_can_enter_operations_or_unfold(self):
        self.login(self.owner)
        self.assertEqual(self.browser.get(reverse('operations:dashboard')).status_code, 200)
        self.assertEqual(self.browser.get(reverse('admin:index')).status_code, 200)
        self.assertEqual(self.browser.get(reverse('operations:team')).status_code, 403)
        self.assertEqual(
            self.browser.get(reverse('operations:team-section', kwargs={'section': 'projects'})).status_code,
            403,
        )
        owner_team_management = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'team'})
        )
        self.assertEqual(owner_team_management.status_code, 200)
        self.assertNotContains(owner_team_management, 'My team workspace')

        for employee in (self.manager, self.office, self.field):
            self.login(employee)
            self.assertEqual(self.browser.get(reverse('operations:dashboard')).status_code, 403)
            self.assertEqual(self.browser.get(reverse('admin:index')).status_code, 403)

        user_model = get_user_model()
        owner_group_account = user_model.objects.create_user(
            username='platform-owner-group-only',
            password='owner-group-pass-123',
            is_staff=True,
        )
        owner_group_account.groups.add(Group.objects.get(name='Owner'))
        EmployeeProfile.objects.create(user=owner_group_account, job_title='Owner group only')
        self.login(owner_group_account)
        self.assertEqual(self.browser.get(reverse('operations:dashboard')).status_code, 403)
        self.assertEqual(self.browser.get(reverse('admin:index')).status_code, 403)

        ungrouped = user_model.objects.create_user(
            username='platform-ungrouped',
            password='ungrouped-pass-123',
            is_staff=True,
        )
        EmployeeProfile.objects.create(user=ungrouped, job_title='Unassigned')
        inactive = user_model.objects.create_user(
            username='platform-inactive',
            password='inactive-pass-123',
            is_staff=True,
        )
        inactive.groups.add(Group.objects.get(name='Field'))
        EmployeeProfile.objects.create(user=inactive, job_title='Inactive', is_active=False)

        for employee in (ungrouped, inactive):
            self.login(employee)
            self.assertEqual(self.browser.get(reverse('operations:dashboard')).status_code, 403)
            self.assertEqual(self.browser.get(reverse('operations:team')).status_code, 403)
            self.assertEqual(self.browser.get(reverse('admin:index')).status_code, 403)

    def test_login_routes_owner_employee_and_client_to_their_workspace(self):
        for username, password, destination in (
            (self.manager.username, 'manager-pass-123', reverse('operations:team')),
            (self.office.username, 'office-pass-123', reverse('operations:team')),
            (self.field.username, 'field-pass-123', reverse('operations:team')),
            (self.client_user.username, 'client-pass-123', reverse('operations:portal')),
        ):
            browser = DjangoClient()
            response = browser.post(
                reverse('operations:login'),
                {'username': username, 'password': password},
            )
            self.assertRedirects(response, destination)

        owner_response = DjangoClient().post(
            reverse('operations:login'),
            {'username': self.owner.username, 'password': 'owner-pass-123'},
        )
        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, 'Please enter a correct username and password.')
        self.assertNotContains(owner_response, '%(username)s')

    @override_settings(
        GCC_TURNSTILE_ENABLED=True,
        CLOUDFLARE_TURNSTILE_SITE_KEY="test-site-key",
        CLOUDFLARE_TURNSTILE_SECRET_KEY="test-secret-key",
    )
    def test_website_turnstile_protects_login_and_contact_but_not_mobile_webview(self):
        contact_data = {
            "first_name": "Test",
            "last_name": "Lead",
            "email": "turnstile@example.com",
            "phone": "805-555-0199",
            "project_type": "Remodel",
            "location": "Ventura, CA",
            "message": "Please contact me about a project.",
        }
        lead_count = Lead.objects.count()
        blocked_contact = self.browser.post(
            reverse("operations:contact"),
            contact_data,
        )
        self.assertEqual(blocked_contact.status_code, 200)
        self.assertContains(blocked_contact, "Complete the security check and try again.")
        self.assertEqual(Lead.objects.count(), lead_count)

        contact_page = self.browser.get(reverse("operations:contact"))
        self.assertContains(
            contact_page,
            "https://challenges.cloudflare.com/turnstile/v0/api.js",
        )
        self.assertContains(contact_page, 'data-sitekey="test-site-key"')
        self.assertContains(contact_page, 'data-action="contact"')

        login_browser = DjangoClient()
        login_page = login_browser.get(reverse("operations:login"))
        self.assertContains(login_page, 'data-sitekey="test-site-key"')
        self.assertContains(login_page, 'data-action="login"')
        blocked_login = login_browser.post(
            reverse("operations:login"),
            {"username": self.field.username, "password": "field-pass-123"},
        )
        self.assertEqual(blocked_login.status_code, 200)
        self.assertContains(blocked_login, "Complete the security check and try again.")

        with patch("operations.forms.verify_turnstile_request", return_value=True):
            allowed_login = login_browser.post(
                reverse("operations:login"),
                {
                    "username": self.field.username,
                    "password": "field-pass-123",
                    "cf-turnstile-response": "valid-login-token",
                },
            )
        self.assertRedirects(allowed_login, reverse("operations:team"))

        with patch("operations.forms.verify_turnstile_request", return_value=True):
            allowed_contact = self.browser.post(
                reverse("operations:contact"),
                {
                    **contact_data,
                    "cf-turnstile-response": "valid-contact-token",
                },
            )
        self.assertRedirects(allowed_contact, reverse("operations:contact"))
        self.assertEqual(Lead.objects.count(), lead_count + 1)

        mobile_browser = DjangoClient(HTTP_USER_AGENT="GrandCoastMobile/1.0")
        mobile_login_page = mobile_browser.get(reverse("operations:login"))
        self.assertNotContains(mobile_login_page, "cf-turnstile")
        mobile_login = mobile_browser.post(
            reverse("operations:login"),
            {"username": self.field.username, "password": "field-pass-123"},
        )
        self.assertRedirects(mobile_login, reverse("operations:team"))
        mobile_contact_page = mobile_browser.get(reverse("operations:contact"))
        self.assertNotContains(mobile_contact_page, "cf-turnstile")

        native_header_browser = DjangoClient(HTTP_X_GRAND_COAST_MOBILE="1")
        native_header_login_page = native_header_browser.get(reverse("operations:login"))
        self.assertNotContains(native_header_login_page, "cf-turnstile")
        native_header_login = native_header_browser.post(
            reverse("operations:login"),
            {"username": self.field.username, "password": "field-pass-123"},
        )
        self.assertRedirects(native_header_login, reverse("operations:team"))
        native_header_contact = native_header_browser.post(
            reverse("operations:contact"),
            {**contact_data, "email": "native-header@example.com"},
        )
        self.assertRedirects(native_header_contact, reverse("operations:contact"))
        self.assertEqual(Lead.objects.count(), lead_count + 2)

    @override_settings(
        GCC_TURNSTILE_ENABLED=True,
        CLOUDFLARE_TURNSTILE_SITE_KEY="test-site-key",
        CLOUDFLARE_TURNSTILE_SECRET_KEY="test-secret-key",
    )
    def test_website_password_reset_renders_turnstile_and_requires_it(self):
        reset_page = self.browser.get(reverse("operations:password-reset"))
        self.assertContains(
            reset_page,
            "https://challenges.cloudflare.com/turnstile/v0/api.js",
        )
        self.assertContains(reset_page, 'data-sitekey="test-site-key"')
        self.assertContains(reset_page, 'data-action="password-reset"')

        blocked = self.browser.post(
            reverse("operations:password-reset"),
            {"email": self.client_user.email},
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "Complete the security check and try again.")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        CLOUDFLARE_TURNSTILE_SITE_KEY="test-site-key",
        CLOUDFLARE_TURNSTILE_SECRET_KEY="test-secret-key",
    )
    def test_mobile_app_can_complete_password_reset_without_turnstile(self):
        mobile_browser = DjangoClient(HTTP_USER_AGENT="GrandCoastMobile/1.0")
        reset_page = mobile_browser.get(reverse("operations:password-reset"))
        self.assertNotContains(reset_page, "cf-turnstile")
        self.assertNotContains(reset_page, "https://challenges.cloudflare.com/turnstile/v0/api.js")

        request = mobile_browser.post(
            reverse("operations:password-reset"),
            {"email": self.client_user.email},
        )
        self.assertRedirects(request, reverse("operations:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)

        reset_url = next(
            line for line in mail.outbox[0].body.splitlines()
            if "/accounts/password/reset/confirm/" in line
        )
        confirm = mobile_browser.get(reset_url, follow=True)
        self.assertContains(confirm, "Choose a new password.")
        confirm_url = reset_url.replace(reset_url.rstrip("/").rsplit("/", 1)[-1], "set-password")
        complete = mobile_browser.post(
            confirm_url,
            {
                "new_password1": "mobile-new-password-123",
                "new_password2": "mobile-new-password-123",
            },
        )
        self.assertRedirects(complete, reverse("operations:password-reset-complete"))
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.check_password("mobile-new-password-123"))

    @override_settings(
        GCC_TURNSTILE_ENABLED=True,
        CLOUDFLARE_TURNSTILE_SITE_KEY="test-site-key",
        CLOUDFLARE_TURNSTILE_SECRET_KEY="test-secret-key",
    )
    def test_turnstile_verifier_posts_token_to_cloudflare(self):
        cloudflare_response = MagicMock()
        cloudflare_response.__enter__.return_value = cloudflare_response
        cloudflare_response.__exit__.return_value = None
        cloudflare_response.read.return_value = b'{"success": true, "action": "login"}'

        browser = DjangoClient()
        with patch("operations.turnstile.urlopen", return_value=cloudflare_response) as open_url:
            response = browser.post(
                reverse("operations:login"),
                {
                    "username": self.field.username,
                    "password": "field-pass-123",
                    "cf-turnstile-response": "server-token",
                },
            )

        self.assertRedirects(response, reverse("operations:team"))
        verification_request = open_url.call_args.args[0]
        self.assertEqual(
            verification_request.full_url,
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        )
        self.assertEqual(verification_request.method, "POST")
        self.assertIn(b"response=server-token", verification_request.data)

    def test_public_navigation_logs_out_owner_and_routes_staff_or_clients_to_dashboard(self):
        public_response = self.browser.get(reverse('operations:home'))
        self.assertContains(public_response, 'Sign in')
        self.assertNotContains(public_response, reverse('operations:dashboard'))
        self.assertNotContains(public_response, reverse('operations:portal'))
        self.assertNotContains(public_response, 'Operations')

        self.login(self.owner)
        owner_response = self.browser.get(reverse('operations:home'))
        self.assertContains(owner_response, 'Sign in')
        self.assertNotContains(owner_response, 'Dashboard')
        self.assertIsNone(self.browser.session.get('_auth_user_id'))

        self.login(self.owner)
        owner_project_response = self.browser.get(
            reverse('operations:project-detail', kwargs={'pk': self.project.pk}),
        )
        self.assertContains(owner_project_response, 'Sign in')
        self.assertIsNone(self.browser.session.get('_auth_user_id'))

        self.login(self.manager)
        employee_response = self.browser.get(reverse('operations:home'))
        self.assertContains(employee_response, 'Dashboard')
        self.assertContains(employee_response, reverse('operations:team'))
        self.assertNotContains(employee_response, 'Sign in')

        self.login(self.client_user)
        client_response = self.browser.get(reverse('operations:home'))
        self.assertContains(client_response, 'Dashboard')
        self.assertContains(client_response, reverse('operations:portal'))
        self.assertNotContains(client_response, 'Sign in')

    def test_portal_requires_a_client_session(self):
        anonymous_response = self.browser.get(reverse('operations:portal'))
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn(reverse('operations:login'), anonymous_response['Location'])
        self.assertIn('next=/portal/', anonymous_response['Location'])

        self.login(self.manager)
        self.assertEqual(self.browser.get(reverse('operations:portal')).status_code, 403)

        self.client_user.is_active = False
        self.client_user.save(update_fields=['is_active'])
        self.login(self.client_user)
        inactive_client_response = self.browser.get(reverse('operations:portal'))
        self.assertIn(inactive_client_response.status_code, (302, 403))

        self.login(self.owner)
        for path in (
            reverse('operations:portal'),
            reverse('operations:portal-section', kwargs={'section': 'overview'}),
            reverse('operations:portal-section', kwargs={'section': 'messages'}),
            reverse('operations:client-mobile-navigation-counts'),
        ):
            self.assertEqual(self.browser.get(path, {'client': self.other_client_record.pk}).status_code, 403)

        self.assertEqual(
            self.browser.get(
                reverse('operations:external-estimate-link', kwargs={'pk': self.estimate.pk}),
            ).status_code,
            404,
        )

    def test_authenticated_roles_cannot_use_other_workspace_invites(self):
        _invite, raw_token = create_client_invite(self.other_client_record, actor=self.owner)
        client_invite_url = reverse('operations:client-invite', kwargs={'token': raw_token})

        employee_invite, employee_token = create_employee_invite(
            email='new.invitee@gcc.example.com',
            first_name='New',
            last_name='Invitee',
            group=Group.objects.get(name='Field'),
            actor=self.owner,
        )
        employee_invite_url = reverse('operations:employee-invite', kwargs={'token': employee_token})

        for user in (self.owner, self.manager, self.client_user):
            with self.subTest(user=user.username, invite='client'):
                self.login(user)
                self.assertEqual(self.browser.get(client_invite_url).status_code, 403)

            with self.subTest(user=user.username, invite='employee'):
                self.login(user)
                self.assertEqual(self.browser.get(employee_invite_url).status_code, 403)

    def test_private_dashboards_do_not_link_into_other_roles_workspaces(self):
        self.login(self.owner)
        owner_dashboard = self.browser.get(reverse('operations:dashboard'))
        self.assertEqual(owner_dashboard.status_code, 200)
        self.assertNotContains(owner_dashboard, 'href="/portal/"')
        self.assertNotContains(owner_dashboard, 'href="/team/"')

        self.login(self.manager)
        employee_dashboard = self.browser.get(reverse('operations:team'))
        self.assertEqual(employee_dashboard.status_code, 200)
        self.assertNotContains(employee_dashboard, 'href="/dashboard/"')
        self.assertNotContains(employee_dashboard, 'href="/portal/"')

        self.login(self.client_user)
        client_dashboard = self.browser.get(reverse('operations:portal'))
        self.assertEqual(client_dashboard.status_code, 200)
        self.assertNotContains(client_dashboard, 'href="/dashboard/"')
        self.assertNotContains(client_dashboard, 'href="/team/"')

    def test_notification_links_stay_inside_each_private_workspace(self):
        EmployeeNotification.objects.create(
            employee=self.owner,
            kind='project-update',
            title='Employee route for owner',
            body='This destination belongs to the employee workspace.',
            destination_url='/team/projects/',
        )
        self.login(self.owner)
        owner_notifications = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'notifications'}),
        )
        self.assertEqual(owner_notifications.status_code, 200)
        self.assertNotContains(owner_notifications, 'href="/team/projects/"')

        EmployeeNotification.objects.create(
            employee=self.manager,
            kind='estimate-update',
            title='Admin route for employee',
            body='This destination belongs to the admin workspace.',
            destination_url='/dashboard/estimates/',
        )
        self.login(self.manager)
        employee_notifications = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'notifications'}),
        )
        self.assertEqual(employee_notifications.status_code, 200)
        self.assertNotContains(employee_notifications, 'href="/dashboard/estimates/"')

        ClientNotification.objects.create(
            client=self.client_user.client_record,
            kind='project-update',
            title='Admin route for client',
            body='This destination belongs to the admin workspace.',
            destination_url='/dashboard/projects/',
        )
        self.login(self.client_user)
        client_notifications = self.browser.get(
            reverse('operations:portal-section', kwargs={'section': 'notifications'}),
        )
        self.assertEqual(client_notifications.status_code, 200)
        self.assertNotContains(client_notifications, 'href="/dashboard/projects/"')

    def test_client_only_reaches_own_portal_records(self):
        self.login(self.client_user)
        self.assertEqual(self.browser.get(reverse('operations:dashboard')).status_code, 403)
        self.assertEqual(self.browser.get(reverse('operations:team')).status_code, 403)
        portal_response = self.browser.get(
            reverse('operations:portal'),
            {'project': self.other_project.pk, 'client': self.other_client_record.pk},
        )
        self.assertEqual(portal_response.status_code, 200)
        self.assertContains(portal_response, self.project.title)
        self.assertNotContains(portal_response, self.other_project.title)
        self.assertEqual(self.browser.get(reverse('admin:index')).status_code, 403)

    def test_client_portal_categories_render_on_separate_compatible_pages(self):
        self.login(self.client_user)
        pages = {
            'overview': 'Choose what you need next.',
            'updates': 'What&rsquo;s happening on your project.',
            'photos': 'Photos &amp; videos from the job.',
            'messages': 'class="conversation-chat portal-chat"',
            'estimate-files': 'Your estimate and shared project files.',
            'notifications': 'Important project alerts, decisions, and updates.',
        }
        for section, marker in pages.items():
            response = self.browser.get(
                reverse('operations:portal-section', kwargs={'section': section}),
            )
            self.assertEqual(response.status_code, 200, section)
            self.assertEqual(response.context['portal_section'], section)
            self.assertContains(response, f'id="portal-{section}"')
            self.assertContains(response, marker)
            self.assertNotContains(response, '#updates')
            self.assertNotContains(response, '#conversation')

        compatibility_response = self.browser.get(reverse('operations:portal'))
        self.assertEqual(compatibility_response.status_code, 200)
        self.assertEqual(compatibility_response.context['portal_section'], 'overview')
        self.assertContains(compatibility_response, 'Choose what you need next.')
        self.assertNotContains(compatibility_response, 'class="conversation-chat portal-chat"')

    def test_field_employees_cannot_open_client_portal_preview(self):
        self.login(self.field)
        self.assertEqual(self.browser.get(reverse('operations:portal')).status_code, 403)

    def test_field_employee_cannot_change_active_status_from_profile(self):
        self.login(self.field)
        response = self.browser.post(
            reverse('operations:team-profile-update', kwargs={'pk': self.field.employee_profile.pk}),
            {
                'job_title': 'Updated field lead',
                'phone': '805-555-0199',
                'is_active': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.field.employee_profile.refresh_from_db()
        self.assertTrue(self.field.employee_profile.is_active)

    def test_nonowners_cannot_change_team_access_or_schedule(self):
        self.login(self.manager)
        self.assertEqual(
            self.browser.post(reverse('operations:employee-invite-create'), {}).status_code,
            403,
        )
        self.assertEqual(
            self.browser.post(
                reverse('operations:team-profile-update', kwargs={'pk': self.field.employee_profile.pk}),
                {'job_title': 'Not allowed', 'phone': '',},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.browser.post(reverse('operations:schedule-create'), {}).status_code,
            403,
        )
        self.assertEqual(
            self.browser.post(
                reverse('operations:schedule-update', kwargs={'pk': self.field_event.pk}),
                {},
            ).status_code,
            403,
        )

    def test_manager_sees_team_calendar_read_only_and_other_roles_are_scoped(self):
        office_event = ScheduleEvent.objects.create(
            title='Office coordination',
            start_at=timezone.now() + timedelta(days=3),
            end_at=timezone.now() + timedelta(days=3, hours=1),
            created_by=self.owner,
        )
        office_event.assignees.add(self.office)

        self.login(self.manager)
        manager_calendar = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'calendar'}),
        )
        self.assertContains(manager_calendar, self.field_event.title)
        self.assertContains(manager_calendar, self.manager_event.title)
        self.assertContains(manager_calendar, office_event.title)
        self.assertContains(
            manager_calendar,
            timezone.localtime(self.field_event.start_at).strftime('%I:%M %p').lstrip('0'),
        )
        self.assertContains(
            manager_calendar,
            timezone.localtime(self.field_event.end_at).strftime('%I:%M %p').lstrip('0'),
        )
        self.assertNotContains(manager_calendar, 'Schedule event')
        self.assertEqual(
            self.browser.post(reverse('operations:schedule-create'), {}).status_code,
            403,
        )
        self.assertEqual(
            self.browser.post(
                reverse('operations:schedule-update', kwargs={'pk': self.field_event.pk}),
                {},
            ).status_code,
            403,
        )

        self.login(self.office)
        office_calendar = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'calendar'}),
        )
        self.assertContains(office_calendar, office_event.title)
        self.assertNotContains(office_calendar, self.field_event.title)
        self.assertNotContains(office_calendar, self.manager_event.title)
        self.assertContains(office_calendar, 'My schedule')

        self.login(self.field)
        field_calendar = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'calendar'}),
        )
        self.assertContains(field_calendar, self.field_event.title)
        self.assertNotContains(field_calendar, self.manager_event.title)
        self.assertNotContains(field_calendar, office_event.title)

    def test_team_reports_and_media_are_forced_internal(self):
        self.login(self.field)
        update_response = self.browser.post(
            reverse('operations:team-project-add-update', kwargs={'pk': self.project.pk}),
            {
                'title': 'Internal field report',
                'body': 'Crew notes stay private until the Owner publishes them.',
                'visibility': 'client',
            },
        )
        self.assertEqual(update_response.status_code, 302)
        update = ProjectUpdate.objects.get(title='Internal field report')
        self.assertEqual(update.visibility, ProjectUpdate.Visibility.INTERNAL)

        upload = SimpleUploadedFile(
            'field-report.jpg',
            b'fake image bytes',
            content_type='image/jpeg',
        )
        media_response = self.browser.post(
            reverse('operations:team-media-upload'),
            {
                'project': self.project.pk,
                'visibility': MediaAsset.Visibility.PUBLIC,
                'files': [upload],
            },
        )
        self.assertEqual(media_response.status_code, 302)
        media = MediaAsset.objects.get(title='field-report')
        self.assertEqual(media.visibility, MediaAsset.Visibility.INTERNAL)

    def test_employee_invite_is_one_time_and_expired_links_are_rejected(self):
        group = Group.objects.get(name='Field')
        invite, raw_token = create_employee_invite(
            email='new.field@gcc.example.com',
            first_name='New',
            last_name='Field',
            group=group,
            actor=self.owner,
        )
        invite_url = reverse('operations:employee-invite', kwargs={'token': raw_token})
        invite_response = self.browser.get(invite_url)
        self.assertEqual(invite_response.status_code, 200)
        self.assertContains(invite_response, "<label>Password")
        self.assertContains(invite_response, "<label>Confirm Password")
        self.assertNotContains(invite_response, "<label>Password1")
        self.assertNotContains(invite_response, "<label>Password2")
        response = self.browser.post(
            invite_url,
            {
                'username': 'new-field',
                'first_name': 'New',
                'last_name': 'Field',
                'password1': 'new-field-password-123',
                'password2': 'new-field-password-123',
            },
        )
        self.assertRedirects(response, reverse('operations:team'))
        invite.refresh_from_db()
        self.assertIsNotNone(invite.accepted_at)
        new_user = get_user_model().objects.get(username='new-field')
        self.assertTrue(new_user.is_staff)
        self.assertTrue(new_user.groups.filter(name='Field').exists())
        self.assertTrue(EmployeeProfile.objects.filter(user=new_user).exists())
        self.assertEqual(self.browser.get(invite_url).status_code, 410)

        expired_raw = 'expired-employee-token'
        expired = EmployeeInvite.objects.create(
            email='expired@gcc.example.com',
            group=group,
            token_hash=hashlib.sha256(expired_raw.encode()).hexdigest(),
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertFalse(expired.is_usable)
        self.assertEqual(
            self.browser.get(reverse('operations:employee-invite', kwargs={'token': expired_raw})).status_code,
            410,
        )

    def test_manager_password_reset_link_changes_password_once(self):
        invite, raw_token = create_employee_invite(
            email=self.manager.email,
            first_name=self.manager.first_name,
            last_name=self.manager.last_name,
            group=Group.objects.get(name='Manager'),
            actor=self.owner,
            employee=self.manager.employee_profile,
            purpose=EmployeeInvite.Purpose.PASSWORD_RESET,
        )
        reset_url = reverse('operations:employee-invite', kwargs={'token': raw_token})
        response = self.browser.post(
            reset_url,
            {
                'password1': 'manager-new-password-123',
                'password2': 'manager-new-password-123',
            },
        )
        self.assertRedirects(response, reverse('operations:team'))
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.check_password('manager-new-password-123'))
        invite.refresh_from_db()
        self.assertIsNotNone(invite.accepted_at)
        self.assertIsNone(find_employee_invite(raw_token))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_client_and_employee_password_recovery_is_scoped_one_time_and_expiring(self):
        login_page = self.browser.get(reverse("operations:login"))
        self.assertContains(login_page, "Forgot your password?")

        client_request = self.browser.post(
            reverse("operations:password-reset"),
            {"email": self.client_user.email},
        )
        self.assertRedirects(client_request, reverse("operations:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        client_reset_url = next(
            line for line in mail.outbox[0].body.splitlines()
            if "/accounts/password/reset/confirm/" in line
        )
        client_form = self.browser.get(client_reset_url, follow=True)
        self.assertContains(client_form, "Choose a new password.")
        client_confirm_url = client_reset_url.replace(client_reset_url.rstrip("/").rsplit("/", 1)[-1], "set-password")
        client_reset = self.browser.post(
            client_confirm_url,
            {
                "new_password1": "client-new-password-123",
                "new_password2": "client-new-password-123",
            },
        )
        self.assertRedirects(client_reset, reverse("operations:password-reset-complete"))
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.check_password("client-new-password-123"))
        self.assertEqual(self.browser.get(client_reset_url).status_code, 200)
        self.assertContains(self.browser.get(client_reset_url), "That link has expired.")

        mail.outbox.clear()
        employee_request = self.browser.post(
            reverse("operations:password-reset"),
            {"email": self.manager.email},
        )
        self.assertRedirects(employee_request, reverse("operations:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        employee_reset_url = next(
            line for line in mail.outbox[0].body.splitlines()
            if "/accounts/password/reset/confirm/" in line
        )
        employee_form = self.browser.get(employee_reset_url, follow=True)
        self.assertContains(employee_form, "Choose a new password.")
        employee_confirm_url = employee_reset_url.replace(employee_reset_url.rstrip("/").rsplit("/", 1)[-1], "set-password")
        employee_reset = self.browser.post(
            employee_confirm_url,
            {
                "new_password1": "employee-new-password-123",
                "new_password2": "employee-new-password-123",
            },
        )
        self.assertRedirects(employee_reset, reverse("operations:password-reset-complete"))
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.check_password("employee-new-password-123"))

        mail.outbox.clear()
        admin_request = self.browser.post(
            reverse("operations:password-reset"),
            {"email": self.owner.email},
        )
        self.assertRedirects(admin_request, reverse("operations:password-reset-done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_public_password_recovery_locks_after_five_unknown_emails(self):
        reset_url = reverse("operations:password-reset")
        done_url = reverse("operations:password-reset-done")

        for attempt in range(4):
            response = self.browser.post(
                reset_url,
                {"email": f"unknown-public-{attempt}@example.com"},
            )
            self.assertRedirects(response, done_url)

        locked = self.browser.post(
            reset_url,
            {"email": "unknown-public-final@example.com"},
        )
        self.assertEqual(locked.status_code, 429)
        self.assertContains(locked, "Too many password reset attempts.", status_code=429)
        self.assertEqual(self.browser.get(reset_url).status_code, 429)

    def test_lead_assignment_conversion_and_estimate_requires_client(self):
        unconverted = Lead.objects.create(
            name='Unconverted Lead',
            email='unconverted@example.com',
            service='Restoration',
            location='Ojai, CA',
        )
        self.login(self.owner)
        before = Estimate.objects.count()
        blocked = self.browser.post(
            reverse('operations:estimate-create'),
            {
                'external_url': 'https://estimates.example.com/blocked',
            },
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, 'Choose a client and provide a secure external estimate link.')
        self.assertEqual(Estimate.objects.count(), before)

        assignment = self.browser.post(
            reverse('operations:lead-assign', kwargs={'pk': unconverted.pk}),
            {'assigned_to': self.field.pk},
        )
        self.assertEqual(assignment.status_code, 302)
        unconverted.refresh_from_db()
        self.assertEqual(unconverted.assigned_to_id, self.field.id)
        conversion = self.browser.post(
            reverse('operations:lead-convert-client', kwargs={'pk': unconverted.pk}),
        )
        self.assertEqual(conversion.status_code, 302)
        unconverted.refresh_from_db()
        self.assertIsNotNone(unconverted.client_id)

        created = self.browser.post(
            reverse('operations:estimate-create'),
            {
                'client': unconverted.client.pk,
                'external_url': 'https://estimates.example.com/allowed',
            },
        )
        self.assertEqual(created.status_code, 302)
        self.assertTrue(
            Estimate.objects.filter(
                client=unconverted.client,
                external_url='https://estimates.example.com/allowed',
                status=Estimate.Status.DRAFT,
            ).exists()
        )

    def test_retrying_lead_conversion_restores_a_deleted_client_record(self):
        lead = Lead.objects.create(
            name="Retry Conversion Lead",
            email="retry-conversion@example.com",
            service="Renovation",
            location="Ventura, CA",
        )
        self.login(self.owner)
        convert_url = reverse('operations:lead-convert-client', kwargs={'pk': lead.pk})

        first_response = self.browser.post(convert_url)
        self.assertEqual(first_response.status_code, 302)
        lead.refresh_from_db()
        first_client_id = lead.client_id
        self.assertIsNotNone(first_client_id)

        ClientRecord.objects.get(pk=first_client_id).delete()
        lead.refresh_from_db()
        self.assertIsNone(lead.client_id)

        retry_response = self.browser.post(convert_url)
        self.assertEqual(retry_response.status_code, 302)
        lead.refresh_from_db()
        self.assertIsNotNone(lead.client_id)
        self.assertEqual(lead.client.email, lead.email)
        self.assertNotEqual(lead.client_id, first_client_id)

    def test_lead_derived_project_requires_accepted_external_estimate(self):
        lead = Lead.objects.create(
            name='Needs approval',
            email='needs-approval@example.com',
            service='Renovation',
            location='Ventura, CA',
            client=self.client_record,
        )
        form = ProjectForm(
            {
                'title': 'Blocked lead project',
                'client': self.client_record.pk,
                'lead': lead.pk,
                'assigned_staff': [self.field.pk],
                'location': 'Ventura, CA',
                'project_type': 'renovation',
                'status': Project.Status.PLANNING,
                'next_step': 'Review',
                'summary': '',
                'is_published': 'on',
                'start_date': '',
                'target_date': '',
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn('externally approved estimate', str(form.errors).lower())

        lead.estimates.create(
            number=9901,
            client=self.client_record,
            title='Approved scope',
            status=Estimate.Status.ACCEPTED,
            external_url='https://estimates.example.com/approved-scope',
            external_status=Estimate.ExternalEstimateStatus.APPROVED,
        )
        form = ProjectForm(form.data)
        self.assertTrue(form.is_valid())

    def test_project_creation_uses_client_as_authoritative_record(self):
        create_form = ProjectCreateForm(staff_queryset=get_user_model().objects.filter(pk=self.field.pk))
        self.assertNotIn('lead', create_form.fields)

        self.login(self.owner)
        response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'projects'}) + '?new=project',
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('lead', response.context['project_form'].fields)
        rendered = response.content.decode()
        create_form_start = rendered.index(
            f'action="{reverse("operations:project-create")}"',
        )
        create_form_end = rendered.index('</form>', create_form_start)
        self.assertNotIn('id="id_lead"', rendered[create_form_start:create_form_end])

        accepted_estimate = Estimate.objects.create(
            number=9910,
            lead=self.lead,
            client=self.client_record,
            title='Client-approved project scope',
            status=Estimate.Status.ACCEPTED,
            external_url='https://estimates.example.com/client-approved',
            external_status=Estimate.ExternalEstimateStatus.APPROVED,
            accepted_at=timezone.now(),
            created_by=self.owner,
        )
        response = self.browser.post(
            reverse('operations:project-create'),
            {
                'title': 'Client-authoritative project',
                'client': str(self.client_record.pk),
                'assigned_staff': [str(self.manager.pk)],
                'location': 'Ventura, CA',
                'project_type': 'renovation',
                'status': Project.Status.PLANNING,
                'next_step': 'Start readiness',
                'summary': '',
                'is_published': 'on',
                'start_date': '',
                'target_date': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title='Client-authoritative project')
        self.assertEqual(project.client_id, self.client_record.pk)
        self.assertEqual(project.estimate_id, accepted_estimate.pk)
        self.assertEqual(project.lead_id, self.lead.pk)

    def test_task_assignment_status_filters_and_activity(self):
        self.login(self.owner)
        create_response = self.browser.post(
            reverse('operations:task-create'),
            {
                'title': 'Assign concrete inspection',
                'description': 'Check the slab before the next phase.',
                'lead': self.lead.pk,
                'project': self.project.pk,
                'milestone': self.milestone.pk,
                'assigned_to': self.field.pk,
                'watchers': [self.office.pk],
                'status': Task.Status.OPEN,
                'priority': Task.Priority.URGENT,
                'due_date': timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(create_response.status_code, 302)
        new_task = Task.objects.get(title='Assign concrete inspection')
        self.assertTrue(new_task.watchers.filter(pk=self.office.pk).exists())
        self.assertTrue(Activity.objects.filter(message='Task created', detail=new_task.title).exists())

        self.login(self.field)
        status_response = self.browser.post(
            reverse('operations:team-task-status', kwargs={'pk': self.task.pk}),
            {'status': Task.Status.COMPLETE},
        )
        self.assertEqual(status_response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.COMPLETE)
        self.assertIsNotNone(self.task.completed_at)
        denied = self.browser.post(
            reverse('operations:team-task-status', kwargs={'pk': self.other_task.pk}),
            {'status': Task.Status.COMPLETE},
        )
        self.assertEqual(denied.status_code, 403)

        filtered = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'tasks'}),
            {
                'priority': Task.Priority.HIGH,
                'project_filter': self.project.pk,
                'due': 'today',
            },
        )
        self.assertContains(filtered, self.task.title)
        self.assertNotContains(filtered, self.other_task.title)

    def test_lead_add_task_action_persists_the_lead_relationship(self):
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:lead-followup', kwargs={'pk': self.lead.pk}),
            {
                'followup-title': 'Call about selections',
                'followup-due_date': timezone.localdate().isoformat(),
                'followup-assigned_to': self.manager.pk,
                'followup-priority': Task.Priority.HIGH,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("lead_tab=notes", response.url)
        task = Task.objects.get(title='Call about selections')
        self.assertEqual(task.lead_id, self.lead.pk)
        self.assertEqual(task.assigned_to_id, self.manager.pk)
        self.assertEqual(task.priority, Task.Priority.HIGH)

    def test_lead_note_save_returns_to_notes_and_tasks(self):
        self.login(self.owner)
        self.lead.note = 'Original client inquiry; keep this unchanged.'
        self.lead.save(update_fields=['note', 'updated_at'])
        response = self.browser.post(
            reverse('operations:lead-note', kwargs={'pk': self.lead.pk}),
            {'admin-note-internal_note': 'Confirm the site walkthrough date.'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'lead={self.lead.pk}', response.url)
        self.assertIn('lead_tab=notes', response.url)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.note, 'Original client inquiry; keep this unchanged.')
        self.assertEqual(self.lead.internal_note, 'Confirm the site walkthrough date.')

    def test_invalid_lead_task_keeps_notes_and_tasks_selected(self):
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:lead-followup', kwargs={'pk': self.lead.pk}),
            {'followup-title': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-lead-detail-tab="notes"')

    def test_admin_can_soft_delete_and_restore_lead_without_losing_tasks(self):
        self.login(self.owner)
        task = Task.objects.create(
            title='Keep deleted lead context',
            lead=self.lead,
            created_by=self.owner,
        )

        delete_response = self.browser.post(
            reverse('operations:lead-delete', kwargs={'pk': self.lead.pk}),
        )
        self.assertRedirects(
            delete_response,
            reverse('operations:dashboard-section', kwargs={'section': 'leads'}),
        )
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.deleted_at)
        self.assertEqual(self.lead.deleted_by_id, self.owner.pk)
        self.assertTrue(Task.objects.filter(pk=task.pk, lead=self.lead).exists())

        active = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'leads'}),
        )
        self.assertNotIn(self.lead.pk, {lead.pk for lead in active.context['leads']})
        self.assertEqual(active.context['operations_nav_counts']['leads'], 0)

        trash = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'leads'}),
            {'trash': '1'},
        )
        self.assertContains(trash, 'Deleted leads')
        self.assertContains(trash, self.lead.name)
        self.assertContains(trash, self.lead.email)
        self.assertContains(trash, self.lead.service)
        self.assertContains(trash, 'Original stage')
        self.assertContains(trash, 'Deleted by')
        self.assertContains(
            trash,
            reverse('operations:lead-restore', kwargs={'pk': self.lead.pk}),
        )

        restore_response = self.browser.post(
            reverse('operations:lead-restore', kwargs={'pk': self.lead.pk}),
        )
        self.assertRedirects(
            restore_response,
            f"{reverse('operations:dashboard-section', kwargs={'section': 'leads'})}?trash=1",
        )
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.deleted_at)
        self.assertIsNone(self.lead.deleted_by_id)
        self.assertTrue(Task.objects.filter(pk=task.pk, lead=self.lead).exists())

    def test_admin_can_move_all_active_leads_to_trash(self):
        second_lead = Lead.objects.create(
            name='Second recoverable lead',
            email='second-recoverable@example.com',
            service='New construction',
            location='Oxnard, CA',
        )
        self.login(self.owner)
        response = self.browser.post(reverse('operations:lead-delete-all'))
        self.assertRedirects(
            response,
            reverse('operations:dashboard-section', kwargs={'section': 'leads'}),
        )
        self.assertEqual(Lead.objects.filter(deleted_at__isnull=True).count(), 0)
        self.assertEqual(
            Lead.objects.filter(deleted_by=self.owner, deleted_at__isnull=False).count(),
            2,
        )

        trash = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'leads'}),
            {'trash': '1'},
        )
        self.assertContains(trash, self.lead.name)
        self.assertContains(trash, second_lead.name)

    def test_only_admin_can_delete_or_restore_leads(self):
        self.lead.deleted_at = timezone.now()
        self.lead.deleted_by = self.owner
        self.lead.save(update_fields=['deleted_at', 'deleted_by', 'updated_at'])
        self.login(self.manager)

        delete_response = self.browser.post(
            reverse('operations:lead-delete', kwargs={'pk': self.lead.pk}),
        )
        restore_response = self.browser.post(
            reverse('operations:lead-restore', kwargs={'pk': self.lead.pk}),
        )
        self.assertEqual(delete_response.status_code, 403)
        self.assertEqual(restore_response.status_code, 403)

    def test_schedule_creation_and_employee_visibility(self):
        start = timezone.now() + timedelta(days=5)
        end = start + timedelta(hours=2)
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:schedule-create'),
            {
                'title': 'Material delivery',
                'assignees': [self.field.pk],
                'project': self.project.pk,
                'task': self.task.pk,
                'start_at': start.strftime('%Y-%m-%dT%H:%M'),
                'end_at': end.strftime('%Y-%m-%dT%H:%M'),
                'location': 'Ventura site',
                'notes': 'Confirm delivery window.',
            },
        )
        self.assertEqual(response.status_code, 302)
        event = ScheduleEvent.objects.get(title='Material delivery')
        self.assertTrue(event.assignees.filter(pk=self.field.pk).exists())
        self.assertContains(
            self.browser.get(reverse('operations:dashboard-section', kwargs={'section': 'calendar'})),
            event.title,
        )
        self.assertContains(
            self.browser.get(reverse('operations:team-section', kwargs={'section': 'calendar'})),
            event.title,
        )

    def test_calendar_limits_day_previews_and_opens_full_day_list(self):
        calendar_day = timezone.localdate() + timedelta(days=10)
        for index in range(4):
            ScheduleEvent.objects.create(
                title=f'Overflow event {index + 1}',
                start_at=self.pacific_datetime(calendar_day, 8 + index),
                end_at=self.pacific_datetime(calendar_day, 9 + index),
                created_by=self.owner,
            )

        self.login(self.owner)
        month = calendar_day.strftime('%Y-%m')
        preview_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'month': month},
        )
        self.assertEqual(preview_response.status_code, 200)
        day_context = next(
            day
            for week in preview_response.context['calendar_weeks']
            for day in week
            if day['date'] == calendar_day
        )
        self.assertEqual(day_context['event_count'], 4)
        self.assertEqual(len(day_context['preview_events']), 3)
        self.assertEqual(day_context['remaining_event_count'], 1)
        self.assertContains(preview_response, 'Overflow event 1')
        self.assertContains(preview_response, 'Overflow event 2')
        self.assertContains(preview_response, 'Overflow event 3')
        self.assertContains(preview_response, '+1 more')
        self.assertContains(preview_response, 'View all 4 events')
        self.assertNotContains(preview_response, 'Overflow event 4')
        self.assertContains(
            preview_response,
            f'?month={month}&amp;day={calendar_day.isoformat()}',
        )

        detail_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'month': month, 'day': calendar_day.isoformat()},
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(len(detail_response.context['selected_day_events']), 4)
        for index in range(4):
            self.assertContains(detail_response, f'Overflow event {index + 1}')

    def test_calendar_groups_events_by_pacific_date_and_excludes_midnight_boundary(self):
        calendar_day = timezone.localdate() + timedelta(days=18)
        local_start = datetime.combine(calendar_day, time(8), tzinfo=CALENDAR_TIME_ZONE)
        local_late_start = datetime.combine(calendar_day, time(23), tzinfo=CALENDAR_TIME_ZONE)
        local_midnight_end = datetime.combine(calendar_day + timedelta(days=1), time.min, tzinfo=CALENDAR_TIME_ZONE)
        ScheduleEvent.objects.create(
            title='Pacific morning event',
            start_at=local_start.astimezone(datetime_timezone.utc),
            end_at=(local_start + timedelta(hours=1)).astimezone(datetime_timezone.utc),
            created_by=self.owner,
        )
        ScheduleEvent.objects.create(
            title='Pacific overnight event',
            start_at=local_late_start.astimezone(datetime_timezone.utc),
            end_at=local_midnight_end.astimezone(datetime_timezone.utc),
            created_by=self.owner,
        )

        self.login(self.owner)
        with timezone.override('UTC'):
            response = self.browser.get(
                reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
                {'month': calendar_day.strftime('%Y-%m'), 'day': calendar_day.isoformat()},
            )
        selected_day = response.context['selected_day']
        next_day = next(
            day
            for week in response.context['calendar_weeks']
            for day in week
            if day['date'] == calendar_day + timedelta(days=1)
        )
        self.assertEqual(
            [event.title for event in selected_day['events']],
            ['Pacific morning event', 'Pacific overnight event'],
        )
        self.assertNotIn('Pacific overnight event', [event.title for event in next_day['events']])

    def test_admin_calendar_includes_project_and_task_timestamps(self):
        calendar_day = timezone.localdate() + timedelta(days=25)
        project = self.project
        project.start_date = calendar_day
        project.target_date = calendar_day
        project.construction_ready_at = self.pacific_datetime(calendar_day, 7, 30)
        project.save(update_fields=["start_date", "target_date", "construction_ready_at", "updated_at"])

        completed_task = Task.objects.create(
            title="Admin calendar completed task",
            description="A completed task timestamp should remain visible to the Owner.",
            project=project,
            lead=self.lead,
            status=Task.Status.COMPLETE,
            due_date=calendar_day,
            completed_at=self.pacific_datetime(calendar_day, 13),
            created_by=self.owner,
        )
        Milestone.objects.create(
            project=project,
            title="Admin calendar milestone",
            sort_order=2,
            is_complete=True,
            completed_at=self.pacific_datetime(calendar_day, 14),
        )

        self.login(self.owner)
        month = calendar_day.strftime("%Y-%m")
        owner_response = self.browser.get(
            reverse("operations:dashboard-section", kwargs={"section": "calendar"}),
            {"month": month, "day": calendar_day.isoformat()},
        )
        self.assertEqual(owner_response.status_code, 200)
        owner_event_titles = [
            event.title
            for event in owner_response.context["selected_day_events"]
        ]
        self.assertIn(f"Project start: {project.title}", owner_event_titles)
        self.assertIn(f"Project deadline: {project.title}", owner_event_titles)
        self.assertIn(f"Construction ready: {project.title}", owner_event_titles)
        self.assertIn(f"Task: {completed_task.title}", owner_event_titles)
        self.assertIn(f"Task completed: {completed_task.title}", owner_event_titles)
        self.assertIn("Milestone completed: Admin calendar milestone", owner_event_titles)
        self.assertGreaterEqual(owner_response.context["calendar_job_event_count"], 6)
        self.assertContains(owner_response, "Owner job timeline:")
        self.assertContains(owner_response, "Task due")
        self.assertContains(owner_response, "Project target")

        self.login(self.field)
        employee_response = self.browser.get(
            reverse("operations:team-section", kwargs={"section": "calendar"}),
            {"month": month, "day": calendar_day.isoformat()},
        )
        self.assertEqual(employee_response.status_code, 200)
        self.assertNotContains(employee_response, f"Project start: {project.title}")
        self.assertNotContains(employee_response, f"Construction ready: {project.title}")
        self.assertNotContains(employee_response, f"Task completed: {completed_task.title}")
        self.assertNotContains(employee_response, "Milestone completed: Admin calendar milestone")

    def test_owner_can_set_clear_calendar_day_and_employee_sees_it(self):
        calendar_day = timezone.localdate() + timedelta(days=12)
        month = calendar_day.strftime('%Y-%m')
        payload = {
            'date': calendar_day.isoformat(),
            'status': 'closed',
            'short_start': '',
            'short_end': '',
            'reason': 'Company holiday',
            'month': month,
        }

        self.login(self.manager)
        self.assertEqual(
            self.browser.post(reverse('operations:calendar-day-update'), payload).status_code,
            403,
        )

        self.login(self.owner)
        response = self.browser.post(reverse('operations:calendar-day-update'), payload)
        self.assertRedirects(
            response,
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'})
            + f'?month={month}&day={calendar_day.isoformat()}',
        )
        override = CalendarDayOverride.objects.get(date=calendar_day)
        self.assertEqual(override.status, CalendarDayOverride.Status.CLOSED)
        self.assertEqual(override.reason, 'Company holiday')
        self.assertTrue(
            Activity.objects.filter(
                message='Calendar day updated',
                detail=f'{calendar_day.isoformat()} · Closed',
                actor=self.owner,
            ).exists()
        )
        owner_calendar_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'month': month, 'day': calendar_day.isoformat()},
        )
        self.assertContains(owner_calendar_response, "data-calendar-day-form")
        self.assertContains(owner_calendar_response, "data-calendar-day-field='short_start'")
        self.assertContains(owner_calendar_response, "data-calendar-day-status='closed'")

        self.login(self.field)
        employee_response = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'calendar'}),
            {'month': month, 'day': calendar_day.isoformat()},
        )
        self.assertContains(employee_response, 'Closed')
        self.assertContains(employee_response, 'Company holiday')
        self.assertNotContains(employee_response, 'Owner controls')

        self.login(self.owner)
        update_response = self.browser.post(
            reverse('operations:calendar-day-update'),
            {
                **payload,
                'status': 'short',
                'short_start': '08:00',
                'short_end': '12:00',
                'reason': 'Short holiday hours',
            },
        )
        self.assertEqual(update_response.status_code, 302)
        override.refresh_from_db()
        self.assertEqual(override.status, CalendarDayOverride.Status.SHORT)
        self.assertEqual(override.short_start, time(8))
        self.assertEqual(override.short_end, time(12))

        short_owner_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'month': month, 'day': calendar_day.isoformat()},
        )
        self.assertContains(short_owner_response, "data-calendar-day-status='short'")

        self.login(self.field)
        short_employee_response = self.browser.get(
            reverse('operations:team-section', kwargs={'section': 'calendar'}),
            {'month': month, 'day': calendar_day.isoformat()},
        )
        self.assertContains(short_employee_response, 'Short day')
        self.assertContains(short_employee_response, 'Hours: 8:00 AM')
        self.assertContains(short_employee_response, '12:00 PM')

        self.login(self.owner)
        reset_response = self.browser.post(
            reverse('operations:calendar-day-update'),
            {
                **payload,
                'status': 'normal',
                'reason': '',
            },
        )
        self.assertEqual(reset_response.status_code, 302)
        self.assertFalse(CalendarDayOverride.objects.filter(date=calendar_day).exists())
        self.assertTrue(
            Activity.objects.filter(
                message='Calendar day reset',
                detail=calendar_day.isoformat(),
                actor=self.owner,
            ).exists()
        )

    def test_short_and_closed_days_enforce_schedule_conflicts(self):
        short_day = timezone.localdate() + timedelta(days=14)
        month = short_day.strftime('%Y-%m')
        self.login(self.owner)
        short_response = self.browser.post(
            reverse('operations:calendar-day-update'),
            {
                'date': short_day.isoformat(),
                'status': 'short',
                'short_start': '08:00',
                'short_end': '12:00',
                'reason': 'Early close',
                'month': month,
            },
        )
        self.assertEqual(short_response.status_code, 302)

        outside_event = ScheduleEvent(
            title='Outside short-day hours',
            start_at=self.pacific_datetime(short_day, 13),
            end_at=self.pacific_datetime(short_day, 14),
            created_by=self.owner,
        )
        with self.assertRaises(ValidationError):
            outside_event.full_clean()

        overnight_short_event = ScheduleEvent(
            title='Overnight short-day conflict',
            start_at=self.pacific_datetime(short_day - timedelta(days=1), 23),
            end_at=self.pacific_datetime(short_day, 9),
            created_by=self.owner,
        )
        with self.assertRaises(ValidationError):
            overnight_short_event.full_clean()

        allowed_event = ScheduleEvent(
            title='Inside short-day hours',
            start_at=self.pacific_datetime(short_day, 9),
            end_at=self.pacific_datetime(short_day, 10),
            created_by=self.owner,
        )
        allowed_event.full_clean()

        closed_day = short_day + timedelta(days=1)
        closed_month = closed_day.strftime('%Y-%m')
        closed_response = self.browser.post(
            reverse('operations:calendar-day-update'),
            {
                'date': closed_day.isoformat(),
                'status': 'closed',
                'short_start': '',
                'short_end': '',
                'reason': 'Holiday closure',
                'month': closed_month,
            },
        )
        self.assertEqual(closed_response.status_code, 302)

        overnight_event = ScheduleEvent(
            title='Overnight closure conflict',
            start_at=self.pacific_datetime(closed_day - timedelta(days=1), 23),
            end_at=self.pacific_datetime(closed_day, 1),
            created_by=self.owner,
        )
        with self.assertRaises(ValidationError):
            overnight_event.full_clean()

        conflict_day = closed_day + timedelta(days=1)
        ScheduleEvent.objects.create(
            title='Existing conflict event',
            start_at=self.pacific_datetime(conflict_day, 9),
            end_at=self.pacific_datetime(conflict_day, 10),
            created_by=self.owner,
        )
        conflict_response = self.browser.post(
            reverse('operations:calendar-day-update'),
            {
                'date': conflict_day.isoformat(),
                'status': 'closed',
                'short_start': '',
                'short_end': '',
                'reason': 'Cannot close yet',
                'month': conflict_day.strftime('%Y-%m'),
            },
        )
        self.assertEqual(conflict_response.status_code, 200)
        self.assertContains(conflict_response, 'Existing conflict event')
        self.assertContains(conflict_response, 'Resolve these scheduled events')
        self.assertFalse(CalendarDayOverride.objects.filter(date=conflict_day).exists())

    def test_calendar_day_form_rejects_invalid_short_hours(self):
        calendar_day = timezone.localdate() + timedelta(days=16)
        form = CalendarDayOverrideForm(
            {
                'date': calendar_day.isoformat(),
                'status': 'short',
                'short_start': '12:00',
                'short_end': '12:00',
                'reason': 'Invalid hours',
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('short_end', form.errors)

    def test_calendar_handles_invalid_day_empty_day_and_month_navigation(self):
        self.login(self.owner)
        invalid_response = self.browser.post(
            reverse('operations:calendar-day-update'),
            {
                'date': 'not-a-date',
                'status': 'closed',
                'short_start': '',
                'short_end': '',
                'reason': 'Invalid date',
                'month': timezone.localdate().strftime('%Y-%m'),
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, 'Enter a valid date')
        self.assertFalse(CalendarDayOverride.objects.filter(reason='Invalid date').exists())

        empty_day = timezone.localdate() + timedelta(days=60)
        empty_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'month': empty_day.strftime('%Y-%m'), 'day': empty_day.isoformat()},
        )
        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.context['selected_day_events'], [])
        self.assertContains(empty_response, 'No schedule events are visible for this day.')
        self.assertContains(empty_response, "data-calendar-day-status='normal'")
        self.assertContains(empty_response, "data-calendar-day-field='reason'")
        previous_month = empty_day.replace(day=1)
        previous_month = (previous_month - timedelta(days=1)).strftime('%Y-%m')
        next_month = (empty_day.replace(day=28) + timedelta(days=7)).replace(day=1).strftime('%Y-%m')
        self.assertContains(empty_response, f'?month={previous_month}')
        self.assertContains(empty_response, f'?month={next_month}')

    def test_schedule_event_edit_opens_editor_and_persists_changes(self):
        self.login(self.owner)
        edit_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'event': self.field_event.pk, 'edit': 'event'},
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, 'Calendar editor')
        self.assertContains(edit_response, f'Update {self.field_event.title}')

        start = timezone.now() + timedelta(days=4)
        end = start + timedelta(hours=3)
        update_response = self.browser.post(
            reverse('operations:schedule-update', kwargs={'pk': self.field_event.pk}),
            {
                'title': 'Updated kitchen field visit',
                'assignees': [self.manager.pk],
                'project': self.other_project.pk,
                'task': '',
                'start_at': start.strftime('%Y-%m-%dT%H:%M'),
                'end_at': end.strftime('%Y-%m-%dT%H:%M'),
                'location': 'Updated Ventura site',
                'notes': 'Updated event notes.',
            },
        )
        self.assertRedirects(
            update_response,
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'})
            + f'?event={self.field_event.pk}',
        )
        self.field_event.refresh_from_db()
        self.assertEqual(self.field_event.title, 'Updated kitchen field visit')
        self.assertEqual(self.field_event.project_id, self.other_project.pk)
        self.assertEqual(self.field_event.location, 'Updated Ventura site')
        self.assertTrue(self.field_event.assignees.filter(pk=self.manager.pk).exists())
        self.assertFalse(self.field_event.assignees.filter(pk=self.field.pk).exists())

    def test_schedule_event_delete_is_owner_only_and_records_activity(self):
        self.login(self.owner)
        edit_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {'event': self.field_event.pk, 'edit': 'event'},
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, 'Delete event')
        self.assertContains(
            edit_response,
            reverse('operations:schedule-delete', kwargs={'pk': self.field_event.pk}),
        )

        delete_response = self.browser.post(
            reverse('operations:schedule-delete', kwargs={'pk': self.field_event.pk}),
        )
        self.assertRedirects(
            delete_response,
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
        )
        self.assertFalse(ScheduleEvent.objects.filter(pk=self.field_event.pk).exists())
        self.assertTrue(
            Activity.objects.filter(
                message='Calendar event deleted',
                detail='Kitchen field visit',
                actor=self.owner,
            ).exists()
        )

        self.login(self.manager)
        self.assertEqual(
            self.browser.post(
                reverse('operations:schedule-delete', kwargs={'pk': self.manager_event.pk}),
            ).status_code,
            403,
        )

    def test_clock_in_out_duplicate_prevention_and_pacific_display(self):
        fixed_clock_in = datetime(2026, 8, 28, 16, 15, tzinfo=datetime_timezone.utc)
        fixed_entry = TimeEntry.objects.create(
            employee=self.field,
            project=self.project,
            task=self.task,
            clock_in=fixed_clock_in,
            clock_out=fixed_clock_in + timedelta(hours=1),
        )
        self.login(self.field)
        response = self.browser.get(reverse('operations:team-section', kwargs={'section': 'time'}))
        self.assertContains(response, '9:15 AM')
        self.assertContains(response, 'America/Los_Angeles')

        clock_in = self.browser.post(
            reverse('operations:team-time-clock'),
            {'project': self.project.pk, 'task': self.task.pk, 'note': 'Site arrival'},
        )
        self.assertEqual(clock_in.status_code, 302)
        active = TimeEntry.objects.get(employee=self.field, clock_out__isnull=True)
        self.assertEqual(active.project_id, self.project.id)
        clock_out = self.browser.post(reverse('operations:team-time-clock'), {'note': 'Leaving site'})
        self.assertEqual(clock_out.status_code, 302)
        active.refresh_from_db()
        self.assertIsNotNone(active.clock_out)
        fixed_entry.delete()
        open_entry = TimeEntry.objects.create(employee=self.field, clock_in=timezone.now())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TimeEntry.objects.create(employee=self.field, clock_in=timezone.now())
        open_entry.delete()

    def test_document_validation_and_protected_client_delivery(self):
        invalid = SimpleUploadedFile('plans.exe', b'bad', content_type='application/octet-stream')
        form = ProjectDocumentForm(
            {
                'project': self.project.pk,
                'title': 'Bad file',
                'category': 'Plans',
                'visibility': ProjectDocument.Visibility.CLIENT,
            },
            {'file': invalid},
        )
        self.assertFalse(form.is_valid())
        self.assertIn('Upload a PDF', str(form.errors))

        client_document = ProjectDocument.objects.create(
            project=self.project,
            title='Client plans',
            category='Plans',
            visibility=ProjectDocument.Visibility.CLIENT,
            uploaded_by=self.owner,
        )
        client_document.file.save('client-plans.pdf', ContentFile(b'pdf bytes'), save=True)
        internal_document = ProjectDocument.objects.create(
            project=self.project,
            title='Internal notes',
            category='Notes',
            visibility=ProjectDocument.Visibility.INTERNAL,
            uploaded_by=self.owner,
        )
        internal_document.file.save('internal-notes.pdf', ContentFile(b'internal bytes'), save=True)

        client_url = reverse('operations:document-file', kwargs={'pk': client_document.pk})
        internal_url = reverse('operations:document-file', kwargs={'pk': internal_document.pk})
        self.login(self.client_user)
        self.assertEqual(self.browser.get(client_url).status_code, 200)
        self.assertEqual(self.browser.get(internal_url).status_code, 403)
        self.browser.force_login(self.other_client_user)
        self.assertEqual(self.browser.get(client_url).status_code, 403)
        self.login(self.field)
        self.assertEqual(self.browser.get(internal_url).status_code, 200)

    def test_two_way_client_messages_and_public_visibility(self):
        self.login(self.client_user)
        client_message_response = self.browser.post(
            reverse('operations:portal-message'),
            {'project': self.project.pk, 'body': 'Can we review the finish schedule?'},
        )
        self.assertEqual(client_message_response.status_code, 302)
        client_message = ClientMessage.objects.get(body='Can we review the finish schedule?')
        self.assertEqual(client_message.sent_by_id, self.client_user.id)
        self.assertFalse(client_message.is_read)

        self.login(self.owner)
        staff_response = self.browser.post(
            reverse('operations:staff-message-reply', kwargs={'client_pk': self.client_record.pk}),
            {'project': self.project.pk, 'body': 'Yes, the team will share it today.'},
        )
        self.assertEqual(staff_response.status_code, 302)
        staff_message = ClientMessage.objects.get(body='Yes, the team will share it today.')
        self.assertTrue(staff_message.sent_by.is_staff)
        staff_notification = ClientNotification.objects.get(kind='employee-reply', message=staff_message)
        self.assertIn(
            reverse('operations:portal-section', kwargs={'section': 'messages'}),
            staff_notification.destination_url,
        )
        self.login(self.client_user)
        self.browser.get(reverse('operations:portal-section', kwargs={'section': 'overview'}))
        staff_message.refresh_from_db()
        self.assertFalse(staff_message.is_read)
        self.browser.get(reverse('operations:portal-section', kwargs={'section': 'messages'}))
        staff_message.refresh_from_db()
        self.assertTrue(staff_message.is_read)

        settings = SiteSettings.objects.get(pk=1)
        settings.google_review_url = 'https://g.page/r/grandcoast/review'
        settings.save(update_fields=['google_review_url', 'updated_at'])
        public_response = self.browser.get(reverse('operations:home'))
        self.assertContains(public_response, 'Review us on Google')
        detail_response = self.browser.get(reverse('operations:project-detail', kwargs={'pk': self.project.pk}))
        self.assertContains(detail_response, self.public_media.title)
        self.assertNotContains(detail_response, self.internal_media.title)

    def test_conversation_surfaces_render_as_one_chat_thread(self):
        ClientMessage.objects.create(
            client=self.client_record,
            project=self.project,
            body='First client question',
            sent_by=self.client_user,
        )
        ClientMessage.objects.create(
            client=self.client_record,
            project=self.project,
            body='Grand Coast response',
            sent_by=self.owner,
        )

        self.login(self.owner)
        admin_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'clients'})
            + f'?client={self.client_record.pk}'
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, 'class="conversation-chat workspace-chat"')
        self.assertContains(admin_response, 'Messages with')
        self.assertContains(admin_response, 'Write a message to the client...')
        self.assertContains(admin_response, '<textarea')
        self.assertContains(admin_response, 'Send message')
        self.assertNotContains(admin_response, 'workspace-messages')
        self.assertNotContains(admin_response, 'Reply to the client...')
        admin_markup = admin_response.content.decode()
        self.assertLess(admin_markup.index('First client question'), admin_markup.index('Grand Coast response'))

        self.login(self.client_user)
        portal_response = self.browser.get(
            reverse('operations:portal-section', kwargs={'section': 'messages'}),
        )
        self.assertEqual(portal_response.status_code, 200)
        self.assertContains(portal_response, 'class="conversation-chat portal-chat"')
        self.assertContains(portal_response, 'Messages with Grand Coast')
        self.assertContains(portal_response, 'Write a message to Grand Coast...')
        self.assertContains(portal_response, '<textarea')
        self.assertContains(portal_response, 'Send message')
        self.assertNotContains(portal_response, 'Conversation history')
        self.assertNotContains(portal_response, 'portal-message-history')
        portal_markup = portal_response.content.decode()
        self.assertLess(portal_markup.index('First client question'), portal_markup.index('Grand Coast response'))

    def test_client_cannot_mutate_portal_acceptance_as_staff_preview(self):
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:portal-accept-estimate', kwargs={'pk': self.estimate.pk}),
        )
        self.assertEqual(response.status_code, 403)

    def test_direct_client_external_estimate_is_published_in_preproject_portal(self):
        user_model = get_user_model()
        preproject_user = user_model.objects.create_user(
            username='platform-preproject-client',
            password='client-pass-123',
            first_name='Preproject',
            last_name='Client',
            email='preproject.platform@example.com',
        )
        preproject_client = ClientRecord.objects.create(
            name='Preproject Client',
            email=preproject_user.email,
            user=preproject_user,
        )
        self.login(self.owner)
        create_response = self.browser.post(
            reverse('operations:estimate-create'),
            {
                'client': preproject_client.pk,
                'external_url': 'https://estimates.example.com/preproject-bathroom',
            },
        )
        self.assertEqual(create_response.status_code, 302)
        self.assertIn('delivery_prompt=1', create_response.url)
        prompt_response = self.browser.get(create_response.url)
        self.assertContains(prompt_response, 'data-auto-open="delivery"')
        self.assertContains(prompt_response, 'All three')
        self.assertContains(prompt_response, 'type="checkbox" name="delivery_mode" value="sms"')
        self.assertContains(prompt_response, 'type="checkbox" name="delivery_mode" value="email"')
        self.assertContains(prompt_response, 'type="checkbox" name="delivery_mode" value="portal"')
        self.assertContains(prompt_response, 'data-estimate-delivery-all')
        self.assertContains(prompt_response, 'Not yet')
        estimate = Estimate.objects.get(client=preproject_client)
        self.assertEqual(estimate.client_id, preproject_client.pk)
        self.assertEqual(estimate.external_url, 'https://estimates.example.com/preproject-bathroom')

        ready_response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': 'email_portal'},
        )
        self.assertEqual(ready_response.status_code, 302)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertTrue(estimate.external_client_visible)

        self.login(preproject_user)
        portal_response = self.browser.get(
            reverse('operations:portal-section', kwargs={'section': 'estimate-files'}),
            {'estimate': estimate.pk},
        )
        self.assertEqual(portal_response.status_code, 200)
        self.assertContains(portal_response, estimate.title)
        self.assertContains(portal_response, 'Sent')
        self.assertContains(portal_response, 'Open estimate link')
        self.assertContains(portal_response, 'Review and respond in the external estimate service.')
        self.assertNotContains(portal_response, 'Accept estimate')

    @override_settings(GCC_EMAIL_DELIVERY_ENABLED=False)
    def test_email_only_estimate_delivery_does_not_publish_to_portal(self):
        estimate = Estimate.objects.create(
            client=self.client_record,
            title='Email-only delivery',
            external_url='https://estimates.example.com/email-only',
            created_by=self.owner,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': 'email', 'idempotency_key': 'email-only-delivery'},
        )
        self.assertEqual(response.status_code, 302)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertFalse(estimate.external_client_visible)

    def test_portal_only_estimate_delivery_publishes_without_email(self):
        from .models import EmailOutbox

        estimate = Estimate.objects.create(
            client=self.client_record,
            title='Portal-only delivery',
            external_url='https://estimates.example.com/portal-only',
            created_by=self.owner,
        )
        idempotency_key = 'portal-only-delivery'
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': 'portal', 'idempotency_key': idempotency_key},
        )
        self.assertEqual(response.status_code, 302)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertTrue(estimate.external_client_visible)
        self.assertFalse(EmailOutbox.objects.filter(idempotency_key=idempotency_key).exists())

    @override_settings(GCC_EMAIL_DELIVERY_ENABLED=False)
    def test_email_and_portal_can_be_selected_together(self):
        from .models import EmailOutbox

        estimate = Estimate.objects.create(
            client=self.client_record,
            title='Email and portal delivery',
            external_url='https://estimates.example.com/email-portal',
            created_by=self.owner,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': ['email', 'portal'], 'idempotency_key': 'email-portal-delivery'},
        )
        self.assertEqual(response.status_code, 302)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertTrue(estimate.external_client_visible)
        self.assertTrue(
            EmailOutbox.objects.filter(
                client=self.client_record,
                idempotency_key='email-portal-delivery',
            ).exists()
        )

    @override_settings(GCC_EMAIL_DELIVERY_ENABLED=False)
    def test_email_and_text_can_be_selected_together(self):
        from .models import EmailOutbox

        self.client_record.phone = '805-555-0142'
        self.client_record.save(update_fields=['phone'])
        estimate = Estimate.objects.create(
            client=self.client_record,
            title='Email and text delivery',
            external_url='https://estimates.example.com/email-text',
            created_by=self.owner,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': ['email', 'sms'], 'idempotency_key': 'email-text-delivery'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('sms_followup=1', response.url)
        self.assertIn('sms_after_other_channels=1', response.url)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertFalse(estimate.external_client_visible)
        self.assertTrue(
            EmailOutbox.objects.filter(
                client=self.client_record,
                idempotency_key='email-text-delivery',
            ).exists()
        )

    def test_portal_and_text_can_be_selected_together(self):
        from .models import EmailOutbox

        self.client_record.phone = '805-555-0142'
        self.client_record.save(update_fields=['phone'])
        estimate = Estimate.objects.create(
            client=self.client_record,
            title='Portal and text delivery',
            external_url='https://estimates.example.com/portal-text',
            created_by=self.owner,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': ['portal', 'sms'], 'idempotency_key': 'portal-text-delivery'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('sms_followup=1', response.url)
        self.assertIn('sms_after_other_channels=1', response.url)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertTrue(estimate.external_client_visible)
        self.assertFalse(EmailOutbox.objects.filter(idempotency_key='portal-text-delivery').exists())

    def test_text_delivery_does_not_claim_sent_until_admin_records_it(self):
        estimate = Estimate.objects.create(
            client=self.client_record,
            title='Text-only delivery',
            external_url='https://estimates.example.com/text-only',
            created_by=self.owner,
        )
        self.client_record.phone = '805-555-0142'
        self.client_record.save(update_fields=['phone'])
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {'delivery_mode': 'sms'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('sms_followup=1', response.url)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.NOT_STARTED)
        self.assertFalse(estimate.external_client_visible)

    def test_estimate_delivery_requires_at_least_one_selected_channel(self):
        estimate = Estimate.objects.create(
            client=self.client_record,
            title='No delivery selected',
            external_url='https://estimates.example.com/no-channel',
            created_by=self.owner,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('delivery_prompt=1', response.url)
        prompt_response = self.browser.get(response.url)
        self.assertContains(prompt_response, 'Select at least one estimate delivery method.')
        self.assertContains(prompt_response, 'data-auto-open="delivery"')
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.NOT_STARTED)
        self.assertFalse(estimate.external_client_visible)

    @override_settings(GCC_EMAIL_DELIVERY_ENABLED=False)
    def test_all_estimate_delivery_publishes_emails_and_offers_sms_followup(self):
        from .models import EmailOutbox

        self.client_record.phone = '805-555-0142'
        self.client_record.save(update_fields=['phone'])
        estimate = Estimate.objects.create(
            client=self.client_record,
            title='All-channel delivery',
            external_url='https://estimates.example.com/all-channels',
            created_by=self.owner,
        )
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:external-estimate-send', kwargs={'pk': estimate.pk}),
            {
                'delivery_mode': ['sms', 'email', 'portal'],
                'idempotency_key': 'all-channel-delivery',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('sms_followup=1', response.url)
        self.assertIn('sms_after_other_channels=1', response.url)
        estimate.refresh_from_db()
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertTrue(estimate.external_client_visible)
        self.assertTrue(
            EmailOutbox.objects.filter(
                client=self.client_record,
                idempotency_key='all-channel-delivery',
            ).exists()
        )
        followup_response = self.browser.get(response.url)
        self.assertContains(followup_response, 'data-auto-open="sms"')
        self.assertContains(followup_response, 'Finish text delivery')

    def test_legacy_internal_estimate_remains_read_only_history(self):
        draft = Estimate.objects.create(
            lead=self.lead,
            client=self.client_record,
            number=9802,
            title='Draft scope before send',
            status=Estimate.Status.SENT,
            created_by=self.owner,
        )
        line = EstimateLineItem.objects.create(
            estimate=draft,
            description='Original scope',
            quantity=Decimal('1.00'),
            unit_price=Decimal('100.00'),
        )
        self.login(self.owner)
        update_response = self.browser.post(
            reverse('operations:estimate-update', kwargs={'pk': draft.pk}),
            {
                'title': 'Final scope before send',
                'status': Estimate.Status.ACCEPTED,
                'deposit_amount': '75.00',
                'notes': 'Visible client scope',
            },
        )
        self.assertEqual(update_response.status_code, 302)
        send_response = self.browser.post(
            reverse('operations:estimate-send', kwargs={'pk': draft.pk}),
            {'title': 'Another attempted change', 'status': Estimate.Status.ACCEPTED},
        )
        self.assertEqual(send_response.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.title, 'Draft scope before send')
        self.assertEqual(draft.status, Estimate.Status.SENT)
        self.assertEqual(draft.total, Decimal('100.00'))
        self.assertEqual(draft.line_items.count(), 1)
        self.assertEqual(draft.line_items.get().description, 'Original scope')

        admin_response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'estimates'}),
            {'estimate': draft.pk},
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, 'read-only history')
        self.assertContains(admin_response, 'Original scope')
        self.assertNotContains(admin_response, 'Revised cabinetry')

        self.login(self.client_user)
        portal_response = self.browser.get(
            reverse('operations:portal-section', kwargs={'section': 'estimate-files'}),
            {'estimate': draft.pk},
        )
        self.assertContains(portal_response, 'Draft scope before send')
        self.assertContains(portal_response, 'Historical estimate records are read-only in GCC.')
        self.assertNotContains(portal_response, 'Accept estimate')
        self.assertNotContains(portal_response, 'Original scope')

    def test_project_portal_view_keeps_primary_estimate_in_project_context(self):
        unrelated = Estimate.objects.create(
            lead=self.lead,
            client=self.client_record,
            number=9803,
            title='Later pre-project scope',
            status=Estimate.Status.SENT,
            created_by=self.owner,
        )
        EstimateLineItem.objects.create(
            estimate=unrelated,
            description='Additional scope',
            quantity=Decimal('1.00'),
            unit_price=Decimal('300.00'),
        )

        self.login(self.client_user)
        response = self.browser.get(
            reverse('operations:portal-section', kwargs={'section': 'estimate-files'}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['portal_project'].pk, self.project.pk)
        self.assertEqual(response.context['portal_estimate'].pk, self.estimate.pk)
        self.assertContains(response, 'Your estimates')
        self.assertContains(response, unrelated.title)

    def test_project_publication_and_staff_assignments_can_be_cleared(self):
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:project-update', kwargs={'pk': self.project.pk}),
            {
                'title': self.project.title,
                'client': self.project.client.pk,
                'lead': self.project.lead.pk,
                'location': self.project.location,
                'project_type': self.project.project_type,
                'status': self.project.status,
                'next_step': self.project.next_step,
                'summary': self.project.summary,
                'start_date': '',
                'target_date': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_published)
        self.assertEqual(self.project.assigned_staff.count(), 0)
        self.assertEqual(
            self.browser.get(reverse('operations:project-detail', kwargs={'pk': self.project.pk})).status_code,
            404,
        )

    def test_related_task_schedule_and_time_records_reject_mismatches(self):
        with self.assertRaises(ValidationError):
            Task(
                title='Mismatched milestone task',
                project=self.other_project,
                milestone=self.milestone,
                created_by=self.owner,
            ).full_clean()

        with self.assertRaises(ValidationError):
            ScheduleEvent(
                title='Mismatched schedule event',
                project=self.other_project,
                task=self.task,
                start_at=timezone.now(),
                end_at=timezone.now() + timedelta(hours=1),
            ).full_clean()

        with self.assertRaises(ValidationError):
            TimeEntry(
                employee=self.field,
                project=self.other_project,
                task=self.task,
                clock_in=timezone.now(),
            ).full_clean()

    def test_invalid_owner_forms_stay_on_the_current_workspace(self):
        self.login(self.owner)
        response = self.browser.post(
            reverse('operations:task-create'),
            {'title': '', 'description': '', 'lead': '', 'project': '', 'status': Task.Status.OPEN},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please correct the task details and try again.')
        self.assertContains(response, 'Save task')

    def test_calendar_create_and_update_write_distinct_activity_entries(self):
        self.login(self.owner)
        start = timezone.now() + timedelta(days=8)
        end = start + timedelta(hours=1)
        response = self.browser.post(
            reverse('operations:schedule-create'),
            {
                'title': 'Distinct activity event',
                'assignees': [self.field.pk],
                'project': self.project.pk,
                'task': self.task.pk,
                'start_at': start.strftime('%Y-%m-%dT%H:%M'),
                'end_at': end.strftime('%Y-%m-%dT%H:%M'),
                'location': 'Ventura site',
                'notes': 'Created once.',
            },
        )
        self.assertEqual(response.status_code, 302)
        event = ScheduleEvent.objects.get(title='Distinct activity event')
        self.assertTrue(Activity.objects.filter(message='Calendar event created', detail=event.title).exists())
        self.assertFalse(Activity.objects.filter(message='Calendar event updated', detail=event.title).exists())

        update_start = start + timedelta(days=1)
        update_response = self.browser.post(
            reverse('operations:schedule-update', kwargs={'pk': event.pk}),
            {
                'title': 'Distinct activity event updated',
                'assignees': [self.field.pk],
                'project': self.project.pk,
                'task': self.task.pk,
                'start_at': update_start.strftime('%Y-%m-%dT%H:%M'),
                'end_at': (update_start + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
                'location': 'Updated Ventura site',
                'notes': 'Updated once.',
            },
        )
        self.assertEqual(update_response.status_code, 302)
        self.assertTrue(
            Activity.objects.filter(
                message='Calendar event updated',
                detail='Distinct activity event updated',
            ).exists()
        )

    def test_owner_team_client_and_public_sections_render_with_seeded_records(self):
        self.login(self.owner)
        for section in (
            'overview', 'clients', 'leads', 'estimates', 'projects', 'tasks',
            'calendar', 'time', 'media', 'documents', 'team', 'content',
        ):
            response = self.browser.get(
                reverse('operations:dashboard-section', kwargs={'section': section}),
            )
            self.assertEqual(response.status_code, 200, section)

        self.login(self.field)
        for section in ('overview', 'projects', 'tasks', 'calendar', 'time', 'media', 'profile'):
            response = self.browser.get(
                reverse('operations:team-section', kwargs={'section': section}),
            )
            self.assertEqual(response.status_code, 200, section)
        employee_profile = self.browser.get(reverse('operations:team-section', kwargs={'section': 'profile'}))
        self.assertContains(employee_profile, 'Log out')
        self.assertContains(employee_profile, 'Delete account')

        self.login(self.client_user)
        self.assertEqual(self.browser.get(reverse('operations:portal')).status_code, 200)
        client_profile = self.browser.get(reverse('operations:portal-section', kwargs={'section': 'profile'}))
        self.assertEqual(client_profile.status_code, 200)
        self.assertContains(client_profile, 'Log out')
        self.assertContains(client_profile, 'Delete account')
        for page in ('home', 'services', 'projects', 'process', 'contact'):
            response = self.browser.get(reverse(f'operations:{page}'))
            self.assertEqual(response.status_code, 200, page)


    def test_employee_account_controls_and_legal_pages_are_available(self):
        self.login(self.field)
        overview = self.browser.get(reverse('operations:team'))
        self.assertContains(overview, 'Manage your access')
        self.assertContains(overview, reverse('operations:account-delete'))
        self.assertContains(overview, 'Log out')

        delete_page = self.browser.get(reverse('operations:account-delete'))
        self.assertEqual(delete_page.status_code, 200)
        self.assertContains(delete_page, 'Current password')
        self.assertContains(delete_page, 'Type DELETE to confirm')

        for page in ('privacy', 'terms'):
            response = self.browser.get(reverse(f'operations:{page}'))
            self.assertEqual(response.status_code, 200, page)

        self.login(self.client_user)
        client_delete_page = self.browser.get(reverse('operations:account-delete'))
        self.assertEqual(client_delete_page.status_code, 200)
        self.assertContains(client_delete_page, 'client portal access')
        self.login(self.owner)
        self.assertEqual(self.browser.get(reverse('operations:account-delete')).status_code, 403)
        self.browser.logout()
        anonymous = self.browser.get(reverse('operations:account-delete'))
        self.assertEqual(anonymous.status_code, 302)

    def test_client_account_delete_anonymizes_client_and_preserves_project_history(self):
        self.login(self.client_user)
        client_user_id = self.client_user.pk
        client_record_id = self.client_record.pk
        project_id = self.project.pk
        device = MobilePushDevice.objects.create(
            client=self.client_record,
            token='ExponentPushToken[client-delete-device]',
            platform='ios',
        )

        response = self.browser.post(
            reverse('operations:account-delete'),
            {'password': 'client-pass-123', 'confirmation': 'DELETE'},
        )
        self.assertRedirects(response, reverse('operations:login'))
        self.assertIsNone(self.browser.session.get('_auth_user_id'))

        self.client_user.refresh_from_db()
        self.client_record.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(self.client_user.pk, client_user_id)
        self.assertTrue(self.client_user.username.startswith('deleted-account-'))
        self.assertFalse(self.client_user.is_active)
        self.assertFalse(self.client_user.is_staff)
        self.assertEqual(self.client_user.email, '')
        self.assertEqual(self.client_record.pk, client_record_id)
        self.assertIsNone(self.client_record.user)
        self.assertEqual(self.client_record.name, 'Deleted client')
        self.assertEqual(self.client_record.email, f'deleted-client-{client_user_id}@invalid.local')
        self.assertEqual(self.project.pk, project_id)
        self.assertEqual(self.project.client_id, client_record_id)
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_employee_account_delete_requires_exact_confirmation_and_password(self):
        self.login(self.field)
        original = {
            'username': self.field.username,
            'email': self.field.email,
            'first_name': self.field.first_name,
        }

        for password, confirmation in (
            ('wrong-password', 'DELETE'),
            ('field-pass-123', 'delete'),
        ):
            response = self.browser.post(
                reverse('operations:account-delete'),
                {'password': password, 'confirmation': confirmation},
            )
            self.assertEqual(response.status_code, 200)
            self.field.refresh_from_db()
            self.assertTrue(self.field.is_active)
            self.assertTrue(self.field.is_staff)
            self.assertEqual(self.field.username, original['username'])
            self.assertEqual(self.field.email, original['email'])
            self.assertEqual(self.field.first_name, original['first_name'])

    def test_employee_account_delete_anonymizes_user_and_preserves_history(self):
        self.login(self.field)
        profile = self.field.employee_profile
        field_group = Group.objects.get(name='Field')
        AdminSecurityProfile.objects.create(user=self.field, pin_enabled=True, pin_hash='hashed-pin')
        AdminRecoveryToken.objects.create(
            user=self.field,
            token_hash='f' * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        EmployeeInvite.objects.create(
            employee=profile,
            email=self.field.email,
            first_name=self.field.first_name,
            last_name=self.field.last_name,
            group=field_group,
            token_hash='e' * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        time_entry = TimeEntry.objects.create(
            employee=self.field,
            project=self.project,
            task=self.task,
            clock_in=timezone.now() - timedelta(hours=2),
            clock_out=timezone.now() - timedelta(hours=1),
        )
        activity = Activity.objects.create(
            message='Field report retained',
            detail='History survives account deletion',
            actor=self.field,
            project=self.project,
        )
        user_id = self.field.pk

        response = self.browser.post(
            reverse('operations:account-delete'),
            {'password': 'field-pass-123', 'confirmation': 'DELETE'},
        )
        self.assertRedirects(response, reverse('operations:login'))
        self.assertIsNone(self.browser.session.get('_auth_user_id'))

        self.field.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(self.field.pk, user_id)
        self.assertTrue(self.field.username.startswith('deleted-account-'))
        self.assertFalse(self.field.is_active)
        self.assertFalse(self.field.is_staff)
        self.assertFalse(self.field.is_superuser)
        self.assertEqual(self.field.email, '')
        self.assertEqual(self.field.first_name, '')
        self.assertEqual(self.field.last_name, '')
        self.assertFalse(self.field.has_usable_password())
        self.assertFalse(self.field.groups.exists())
        self.assertFalse(self.field.user_permissions.exists())
        self.assertFalse(profile.is_active)
        self.assertEqual(profile.job_title, '')
        self.assertEqual(profile.phone, '')
        self.assertFalse(EmployeeInvite.objects.filter(employee=profile).exists())
        self.assertFalse(AdminRecoveryToken.objects.filter(user=self.field).exists())
        security_profile = AdminSecurityProfile.objects.get(user=self.field)
        self.assertFalse(security_profile.pin_enabled)
        self.assertEqual(security_profile.pin_hash, '')

        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertEqual(Task.objects.get(pk=self.task.pk).assigned_to_id, user_id)
        self.assertTrue(self.project.assigned_staff.filter(pk=user_id).exists())
        self.assertTrue(TimeEntry.objects.filter(pk=time_entry.pk, employee_id=user_id).exists())
        self.assertEqual(Activity.objects.get(pk=activity.pk).actor_id, user_id)

        rejected_login = self.browser.post(
            reverse('operations:login'),
            {'username': 'platform-field', 'password': 'field-pass-123'},
        )
        self.assertEqual(rejected_login.status_code, 200)
        self.assertContains(rejected_login, 'Please enter a correct username and password.')

    def test_post_logout_redirects_to_sign_in(self):
        self.login(self.field)
        response = self.browser.post(reverse('operations:logout'))
        self.assertRedirects(response, reverse('operations:login'))


class SeedOperationsTests(TestCase):
    def test_seed_operations_is_idempotent_and_creates_roles(self):
        management.call_command('seed_operations', verbosity=0)
        counts = {
            'services': __import__('operations.models', fromlist=['Service']).Service.objects.count(),
            'process_steps': __import__('operations.models', fromlist=['ProcessStep']).ProcessStep.objects.count(),
            'clients': ClientRecord.objects.count(),
            'leads': Lead.objects.count(),
            'estimates': Estimate.objects.count(),
            'projects': Project.objects.count(),
            'tasks': Task.objects.count(),
        }
        management.call_command('seed_operations', verbosity=0)
        self.assertEqual(Group.objects.filter(name__in=['Owner', 'Manager', 'Office', 'Field']).count(), 4)
        self.assertEqual(
            {key: model_count for key, model_count in counts.items()},
            {
                'services': __import__('operations.models', fromlist=['Service']).Service.objects.count(),
                'process_steps': __import__('operations.models', fromlist=['ProcessStep']).ProcessStep.objects.count(),
                'clients': ClientRecord.objects.count(),
                'leads': Lead.objects.count(),
                'estimates': Estimate.objects.count(),
                'projects': Project.objects.count(),
                'tasks': Task.objects.count(),
            },
        )
