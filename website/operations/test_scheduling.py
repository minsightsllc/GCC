import json
from datetime import date, time, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    CalendarDayOverride,
    Client as ClientRecord,
    ClientNotification,
    EmployeeNotification,
    EmployeeProfile,
    EmployeeScheduleOverride,
    EmployeeWeeklySchedule,
    MobilePushDevice,
    PushDelivery,
    effective_employee_schedule,
)
from .notifications import (
    dispatch_client_notification_ids,
    deliver_push_delivery,
    queue_client_notifications,
)
from .services import ensure_role_groups


class EmployeeSchedulingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user(
            username='schedule-owner',
            password='owner-pass',
            is_staff=True,
            is_superuser=True,
        )
        cls.employee = user_model.objects.create_user(
            username='schedule-employee',
            password='employee-pass',
            first_name='Schedule',
            last_name='Employee',
            is_staff=True,
        )
        cls.manager = user_model.objects.create_user(
            username='schedule-manager',
            password='manager-pass',
            is_staff=True,
        )
        roles = ensure_role_groups()
        cls.owner.groups.add(roles['Owner'])
        cls.employee.groups.add(roles['Field'])
        cls.manager.groups.add(roles['Manager'])
        EmployeeProfile.objects.create(user=cls.owner)
        EmployeeProfile.objects.create(user=cls.employee)
        EmployeeProfile.objects.create(user=cls.manager)

    def pacific_day(self, offset=14):
        return timezone.localtime(timezone.now()).date() + timedelta(days=offset)

    def test_schedule_precedence_and_company_hours(self):
        day = self.pacific_day()
        day -= timedelta(days=day.weekday())
        EmployeeWeeklySchedule.objects.create(
            employee=self.employee,
            weekday=day.weekday(),
            is_working=True,
            start_time=time(8),
            end_time=time(16),
        )
        self.assertEqual(effective_employee_schedule(self.employee, day)['start_time'], time(8))

        EmployeeScheduleOverride.objects.create(
            employee=self.employee,
            date=day,
            status=EmployeeScheduleOverride.Status.WORKING,
            start_time=time(10),
            end_time=time(12),
            reason='Appointment',
            created_by=self.owner,
        )
        effective = effective_employee_schedule(self.employee, day)
        self.assertEqual((effective['start_time'], effective['end_time']), (time(10), time(12)))

        EmployeeScheduleOverride.objects.filter(employee=self.employee, date=day).update(
            status=EmployeeScheduleOverride.Status.OFF,
            start_time=None,
            end_time=None,
        )
        self.assertFalse(effective_employee_schedule(self.employee, day)['is_working'])
        EmployeeScheduleOverride.objects.filter(employee=self.employee, date=day).delete()

        CalendarDayOverride.objects.create(
            date=day,
            status=CalendarDayOverride.Status.SHORT,
            short_start=time(9),
            short_end=time(13),
            created_by=self.owner,
        )
        effective = effective_employee_schedule(self.employee, day)
        self.assertEqual((effective['start_time'], effective['end_time']), (time(9), time(13)))
        CalendarDayOverride.objects.filter(date=day).update(status=CalendarDayOverride.Status.CLOSED, short_start=None, short_end=None)
        self.assertFalse(effective_employee_schedule(self.employee, day)['is_working'])

    def test_owner_weekly_and_date_routes_notify_only_on_change(self):
        day = self.pacific_day()
        week = day - timedelta(days=day.weekday())
        weekly_payload = {
            'employee': self.employee.pk,
            'weekday': day.weekday(),
            'week': week.isoformat(),
            f'weekday-{day.weekday()}-is_working': 'on',
            f'weekday-{day.weekday()}-start_time': '08:00',
            f'weekday-{day.weekday()}-end_time': '16:00',
        }
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(reverse('operations:weekly-schedule-update'), weekly_payload).status_code, 403)

        self.client.force_login(self.owner)
        response = self.client.post(reverse('operations:weekly-schedule-update'), weekly_payload)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EmployeeWeeklySchedule.objects.filter(employee=self.employee, weekday=day.weekday(), is_working=True).exists())
        self.assertEqual(EmployeeNotification.objects.filter(employee=self.employee, kind='employee-schedule').count(), 1)
        self.client.post(reverse('operations:weekly-schedule-update'), weekly_payload)
        self.assertEqual(EmployeeNotification.objects.filter(employee=self.employee, kind='employee-schedule').count(), 1)

        override_payload = {
            'employee': self.employee.pk,
            'date': day.isoformat(),
            'status': 'off',
            'start_time': '',
            'end_time': '',
            'reason': 'Personal day',
        }
        self.assertEqual(self.client.post(reverse('operations:employee-day-schedule-update'), override_payload).status_code, 302)
        self.assertTrue(EmployeeScheduleOverride.objects.filter(employee=self.employee, date=day, status='off').exists())
        self.assertEqual(EmployeeNotification.objects.filter(employee=self.employee, kind='employee-schedule').count(), 2)
        self.client.post(reverse('operations:employee-day-schedule-update'), {**override_payload, 'status': 'clear'})
        self.assertFalse(EmployeeScheduleOverride.objects.filter(employee=self.employee, date=day).exists())
        self.assertEqual(EmployeeNotification.objects.filter(employee=self.employee, kind='employee-schedule').count(), 3)

    def test_calendar_day_links_preserve_employee_schedule_in_day_details(self):
        day = self.pacific_day()
        EmployeeWeeklySchedule.objects.create(
            employee=self.employee,
            weekday=day.weekday(),
            is_working=True,
            start_time=time(9),
            end_time=time(17),
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse('operations:dashboard-section', kwargs={'section': 'calendar'}),
            {
                'month': day.strftime('%Y-%m'),
                'day': day.isoformat(),
                'schedule_employee': self.employee.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_schedule_employee'], self.employee)
        self.assertTrue(response.context['selected_day']['effective_shift']['is_working'])
        self.assertEqual(response.context['selected_day']['effective_shift']['start_time'], time(9))
        self.assertContains(response, 'Shift for Schedule Employee')
        self.assertContains(response, f'&amp;schedule_employee={self.employee.pk}')

    def test_device_registration_and_deactivation_are_scoped_to_employee(self):
        self.client.force_login(self.employee)
        token = 'ExponentPushToken[test-device]'
        response = self.client.post(
            reverse('operations:notification-device-register'),
            {'token': token, 'platform': 'android'},
        )
        self.assertEqual(response.status_code, 200)
        device = MobilePushDevice.objects.get(token=token)
        self.assertEqual(device.employee_id, self.employee.pk)
        self.assertTrue(device.is_active)
        response = self.client.post(
            reverse('operations:notification-device-deactivate'),
            {'token': token},
        )
        self.assertEqual(response.status_code, 200)
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_owner_device_registration_is_mobile_flag_controlled(self):
        self.client.force_login(self.owner)
        token = 'ExponentPushToken[owner-mobile-device]'
        mobile_headers = {
            'HTTP_USER_AGENT': 'GrandCoastMobile/1.0',
            'HTTP_X_GRAND_COAST_MOBILE': '1',
        }
        with self.settings(GCC_MOBILE_OWNER_PUSH_ENABLED=False):
            disabled = self.client.post(
                reverse('operations:notification-device-register'),
                {'token': token, 'platform': 'ios'},
                **mobile_headers,
            )
        self.assertEqual(disabled.status_code, 403)
        self.assertFalse(MobilePushDevice.objects.filter(token=token).exists())

        with self.settings(GCC_MOBILE_OWNER_PUSH_ENABLED=True):
            browser_request = self.client.post(
                reverse('operations:notification-device-register'),
                {'token': token, 'platform': 'ios'},
            )
            self.assertEqual(browser_request.status_code, 403)
            enabled = self.client.post(
                reverse('operations:notification-device-register'),
                {'token': token, 'platform': 'ios'},
                **mobile_headers,
            )
        self.assertEqual(enabled.status_code, 200)
        device = MobilePushDevice.objects.get(token=token)
        self.assertEqual(device.employee_id, self.owner.pk)
        self.assertTrue(device.is_active)

    def test_client_device_registration_is_scoped_to_client(self):
        user_model = get_user_model()
        client_user = user_model.objects.create_user(
            username='schedule-client',
            password='client-pass',
            email='schedule-client@example.com',
        )
        client = ClientRecord.objects.create(
            name='Schedule Client',
            email=client_user.email,
            user=client_user,
        )
        other_client_user = user_model.objects.create_user(
            username='schedule-other-client',
            password='client-pass',
            email='schedule-other-client@example.com',
        )
        other_client = ClientRecord.objects.create(
            name='Other Schedule Client',
            email=other_client_user.email,
            user=other_client_user,
        )
        token = 'ExponentPushToken[client-device]'

        self.client.force_login(client_user)
        response = self.client.post(
            reverse('operations:client-notification-device-register'),
            {'token': token, 'platform': 'ios'},
        )
        self.assertEqual(response.status_code, 200)
        device = MobilePushDevice.objects.get(token=token)
        self.assertEqual(device.client_id, client.pk)
        self.assertIsNone(device.employee_id)
        self.assertTrue(device.is_active)

        self.client.force_login(other_client_user)
        response = self.client.post(
            reverse('operations:client-notification-device-deactivate'),
            {'token': token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deactivated'], 0)
        device.refresh_from_db()
        self.assertEqual(device.client_id, client.pk)
        self.assertTrue(device.is_active)

        self.client.force_login(self.employee)
        staff_response = self.client.post(
            reverse('operations:client-notification-device-register'),
            {'token': 'ExponentPushToken[staff-cannot-register-client]'},
        )
        self.assertEqual(staff_response.status_code, 403)

        self.client.force_login(client_user)
        response = self.client.post(
            reverse('operations:client-notification-device-register'),
            {'token': 'not-an-expo-token'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(other_client.user_id, other_client_user.pk)

        response = self.client.post(
            reverse('operations:client-notification-device-deactivate'),
            {'token': token},
        )
        self.assertEqual(response.status_code, 200)
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    @override_settings(EXPO_PUSH_ENABLED=True)
    def test_client_notifications_dispatch_after_commit_and_are_deduplicated(self):
        user_model = get_user_model()
        client_user = user_model.objects.create_user(
            username='schedule-push-client',
            password='client-pass',
            email='schedule-push-client@example.com',
        )
        client = ClientRecord.objects.create(
            name='Push Client',
            email=client_user.email,
            user=client_user,
        )
        device = MobilePushDevice.objects.create(
            client=client,
            token='ExponentPushToken[client-push-device]',
            platform='ios',
            last_seen_at=timezone.now(),
        )

        with patch('operations.notifications.deliver_push_delivery') as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                notifications = queue_client_notifications(
                    [client, client],
                    kind='project-update',
                    title='Project update',
                    body='A new update is ready.',
                    destination_url='/portal/updates/',
                )
            deliver.assert_called_once()
            self.assertEqual(
                PushDelivery.objects.filter(
                    client_notification_id=notifications[0].pk,
                    device=device,
                ).count(),
                1,
            )

        with self.settings(EXPO_PUSH_ENABLED=False):
            dispatch_client_notification_ids([notifications[0].pk])
            dispatch_client_notification_ids([notifications[0].pk])
        self.assertEqual(
            PushDelivery.objects.filter(
                client_notification_id=notifications[0].pk,
                device=device,
            ).count(),
            1,
        )

    @override_settings(EXPO_PUSH_ENABLED=True)
    def test_client_push_payload_is_portal_only_and_redacts_metadata(self):
        user_model = get_user_model()
        client_user = user_model.objects.create_user(
            username='schedule-payload-client',
            password='client-pass',
            email='schedule-payload-client@example.com',
        )
        client = ClientRecord.objects.create(
            name='Payload Client',
            email=client_user.email,
            user=client_user,
        )
        device = MobilePushDevice.objects.create(
            client=client,
            token='ExponentPushToken[client-payload-device]',
            platform='android',
            last_seen_at=timezone.now(),
        )
        notification = ClientNotification.objects.create(
            client=client,
            kind='project-update',
            title='Project update',
            body='The project has a new update.',
            destination_url='/dashboard/projects/private/',
            metadata={'internal_cost': '999999', 'vendor': 'Private Vendor'},
        )
        delivery = PushDelivery.objects.create(
            client_notification=notification,
            device=device,
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'data': {'status': 'ok', 'id': 'client-ticket-123'}}
        ).encode('utf-8')
        with patch('operations.notifications.urlopen', return_value=response) as urlopen:
            deliver_push_delivery(delivery)

        payload = json.loads(urlopen.call_args.args[0].data.decode('utf-8'))
        self.assertEqual(payload['data']['url'], '/portal/notifications/')
        self.assertEqual(
            set(payload['data']),
            {'url', 'notification_id', 'kind'},
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, PushDelivery.Status.SENT)

    @override_settings(EXPO_PUSH_ENABLED=True)
    def test_invalid_client_push_token_is_deactivated(self):
        user_model = get_user_model()
        client_user = user_model.objects.create_user(
            username='schedule-invalid-client',
            password='client-pass',
            email='schedule-invalid-client@example.com',
        )
        client = ClientRecord.objects.create(
            name='Invalid Token Client',
            email=client_user.email,
            user=client_user,
        )
        device = MobilePushDevice.objects.create(
            client=client,
            token='not-an-expo-token',
            platform='ios',
        )
        notification = ClientNotification.objects.create(
            client=client,
            kind='project-update',
            title='Project update',
            body='A new update is ready.',
        )
        delivery = PushDelivery.objects.create(
            client_notification=notification,
            device=device,
        )

        deliver_push_delivery(delivery)
        device.refresh_from_db()
        delivery.refresh_from_db()
        self.assertFalse(device.is_active)
        self.assertEqual(delivery.status, PushDelivery.Status.INVALID)

    def test_blank_weekly_days_do_not_create_noop_notifications(self):
        day = self.pacific_day()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('operations:weekly-schedule-update'),
            {
                'employee': self.employee.pk,
                'weekday': day.weekday(),
                'week': (day - timedelta(days=day.weekday())).isoformat(),
                f'weekday-{day.weekday()}-is_working': '',
                f'weekday-{day.weekday()}-start_time': '',
                f'weekday-{day.weekday()}-end_time': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            EmployeeWeeklySchedule.objects.filter(
                employee=self.employee,
                weekday=day.weekday(),
            ).exists()
        )
        self.assertFalse(EmployeeNotification.objects.filter(employee=self.employee).exists())

    def test_notification_inbox_uses_uuid_read_route_and_is_employee_scoped(self):
        notification = EmployeeNotification.objects.create(
            employee=self.employee,
            kind='employee-schedule',
            title='Schedule update',
            body='Your hours changed.',
            destination_url='/team/calendar/',
        )
        self.assertEqual(len(str(notification.pk)), 36)

        self.client.force_login(self.employee)
        inbox = self.client.get(reverse('operations:team-notifications'))
        self.assertEqual(inbox.status_code, 200)
        self.assertContains(inbox, 'Schedule update')
        self.assertContains(
            inbox,
            reverse('operations:notification-mark-read', kwargs={'pk': notification.pk}),
        )

        response = self.client.post(
            reverse('operations:notification-mark-read', kwargs={'pk': notification.pk}),
        )
        self.assertEqual(response.status_code, 200)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

        other_notification = EmployeeNotification.objects.create(
            employee=self.owner,
            kind='employee-schedule',
            title='Private schedule update',
            body='This is not yours.',
        )
        forbidden = self.client.post(
            reverse('operations:notification-mark-read', kwargs={'pk': other_notification.pk}),
        )
        self.assertEqual(forbidden.status_code, 404)

    def test_employee_schedule_models_reject_overnight_shifts(self):
        with self.assertRaises(ValidationError):
            EmployeeWeeklySchedule(
                employee=self.employee,
                weekday=0,
                is_working=True,
                start_time=time(22),
                end_time=time(2),
            ).full_clean()
        with self.assertRaises(ValidationError):
            EmployeeScheduleOverride(
                employee=self.employee,
                date=self.pacific_day(),
                status=EmployeeScheduleOverride.Status.WORKING,
                start_time=time(22),
                end_time=time(2),
                created_by=self.owner,
            ).full_clean()

    @override_settings(EXPO_PUSH_ENABLED=True)
    def test_push_payload_uses_default_sound_and_android_channel(self):
        device = MobilePushDevice.objects.create(
            employee=self.employee,
            token='ExponentPushToken[payload-device]',
            platform='android',
            last_seen_at=timezone.now(),
        )
        notification = EmployeeNotification.objects.create(
            employee=self.employee,
            kind='employee-schedule',
            title='Schedule update',
            body='Your hours changed.',
            destination_url='/team/calendar/',
            metadata={'date': self.pacific_day().isoformat()},
        )
        delivery = PushDelivery.objects.create(notification=notification, device=device)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'data': {'status': 'ok', 'id': 'ticket-123'}}
        ).encode('utf-8')
        with patch('operations.notifications.urlopen', return_value=response) as urlopen:
            deliver_push_delivery(delivery)
        payload = json.loads(urlopen.call_args.args[0].data.decode('utf-8'))
        self.assertEqual(payload['sound'], 'default')
        self.assertEqual(payload['channelId'], 'schedule-updates')
        self.assertEqual(payload['data']['url'], '/team/calendar/')
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, PushDelivery.Status.SENT)
