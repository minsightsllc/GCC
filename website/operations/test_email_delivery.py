from django.core import mail
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client as TestClient
from django.test import TestCase, override_settings
from django.urls import reverse

from .email_delivery import (
    deliver_email_outbox,
    queue_client_invite_email,
    queue_employee_invite_email,
)
from .models import Client, ClientInvite, EmployeeInvite, EmailOutbox
from .services import create_client_invite, create_employee_invite


class ClientInviteEmailDeliveryTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username='invite-owner',
            password='invite-owner-pass',
            email='owner@example.test',
            is_staff=True,
            is_superuser=True,
        )
        self.client_record = Client.objects.create(
            name='Jordan Hart',
            email='jordan@example.test',
        )

    def _queue(self):
        invite, raw_token = create_client_invite(self.client_record, actor=self.staff)
        invite_url = 'http://testserver' + reverse(
            'operations:client-invite',
            kwargs={'token': raw_token},
        )
        return invite, raw_token, queue_client_invite_email(
            invite,
            invite_url,
            actor=self.staff,
        )

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.test',
    )
    def test_enabled_delivery_sends_once_and_is_idempotent(self):
        invite, raw_token, outbox = self._queue()
        delivered = deliver_email_outbox(outbox)
        self.assertEqual(delivered.status, EmailOutbox.Status.SENT)
        self.assertEqual(delivered.attempt_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['jordan@example.test'])
        self.assertIn(raw_token, mail.outbox[0].body)
        self.assertIn(str(invite.pk), delivered.idempotency_key)

        duplicate = queue_client_invite_email(
            invite,
            'http://testserver' + reverse(
                'operations:client-invite',
                kwargs={'token': raw_token},
            ),
            actor=self.staff,
        )
        self.assertEqual(duplicate.pk, outbox.pk)
        self.assertEqual(deliver_email_outbox(duplicate).status, EmailOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=False,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.test',
    )
    def test_disabled_delivery_keeps_copy_fallback_pending(self):
        _invite, _raw_token, outbox = self._queue()
        delivered = deliver_email_outbox(outbox)
        self.assertEqual(delivered.status, EmailOutbox.Status.PENDING)
        self.assertEqual(delivered.attempt_count, 0)
        self.assertEqual(mail.outbox, [])

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='',
    )
    def test_missing_sender_marks_failed_without_rolling_back_invite(self):
        invite, _raw_token, outbox = self._queue()
        delivered = deliver_email_outbox(outbox)
        self.assertEqual(delivered.status, EmailOutbox.Status.FAILED)
        self.assertEqual(delivered.attempt_count, 1)
        self.assertTrue(ClientInvite.objects.filter(pk=invite.pk).exists())
        self.assertEqual(mail.outbox, [])

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.test',
    )
    def test_client_invite_button_sends_email_and_keeps_link_in_session(self):
        browser = TestClient()
        browser.force_login(self.staff)
        response = browser.post(
            reverse(
                'operations:client-invite-create',
                kwargs={'pk': self.client_record.pk},
            ),
            follow=True,
        )
        expected = reverse('operations:dashboard-section', kwargs={'section': 'clients'})
        self.assertEqual(response.redirect_chain[-1][0], f'{expected}?client={self.client_record.pk}')
        outbox = EmailOutbox.objects.get(client=self.client_record)
        self.assertEqual(outbox.status, EmailOutbox.Status.SENT)
        self.assertEqual(mail.outbox[0].to, ['jordan@example.test'])
        self.assertContains(response, 'Client invite emailed.')
        self.assertContains(response, 'Sent to jordan@example.test.')
        self.assertContains(response, 'one-time link')


class EmployeeInviteEmailDeliveryTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username='employee-invite-owner',
            password='employee-invite-owner-pass',
            email='owner@example.test',
            is_staff=True,
            is_superuser=True,
        )
        self.group, _created = Group.objects.get_or_create(name='Field')

    def _queue(self):
        invite, raw_token = create_employee_invite(
            email='casey@example.test',
            first_name='Casey',
            last_name='Lee',
            group=self.group,
            actor=self.owner,
        )
        invite_url = 'http://testserver' + reverse(
            'operations:employee-invite',
            kwargs={'token': raw_token},
        )
        return invite, raw_token, queue_employee_invite_email(
            invite,
            invite_url,
            actor=self.owner,
        )

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.test',
    )
    def test_enabled_delivery_sends_employee_onboarding_link_once(self):
        invite, raw_token, outbox = self._queue()
        delivered = deliver_email_outbox(outbox)
        self.assertEqual(delivered.status, EmailOutbox.Status.SENT)
        self.assertEqual(mail.outbox[0].to, ['casey@example.test'])
        self.assertIn(raw_token, mail.outbox[0].body)
        self.assertIn('expires in seven days', mail.outbox[0].body)
        self.assertEqual(delivered.idempotency_key, f'employee-onboarding-invite:{invite.pk}')

        duplicate = queue_employee_invite_email(
            invite,
            'https://example.test/duplicate',
            actor=self.owner,
        )
        self.assertEqual(duplicate.pk, outbox.pk)
        self.assertEqual(deliver_email_outbox(duplicate).status, EmailOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=False,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.test',
    )
    def test_disabled_delivery_keeps_employee_invite_and_copy_link_available(self):
        browser = TestClient()
        browser.force_login(self.owner)
        response = browser.post(
            reverse('operations:employee-invite-create'),
            {
                'email': 'casey@example.test',
                'first_name': 'Casey',
                'last_name': 'Lee',
                'group': self.group.pk,
            },
            follow=True,
        )
        invite = EmployeeInvite.objects.get(email='casey@example.test')
        outbox = EmailOutbox.objects.get(idempotency_key=f'employee-onboarding-invite:{invite.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(outbox.status, EmailOutbox.Status.PENDING)
        self.assertContains(response, 'Email is pending or disabled.')
        self.assertContains(response, '/accounts/employee-invite/')
        self.assertEqual(mail.outbox, [])

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.test',
    )
    def test_employee_invite_create_emails_recipient_and_displays_backup_link(self):
        browser = TestClient()
        browser.force_login(self.owner)
        response = browser.post(
            reverse('operations:employee-invite-create'),
            {
                'email': 'casey@example.test',
                'first_name': 'Casey',
                'last_name': 'Lee',
                'group': self.group.pk,
            },
            follow=True,
        )
        invite = EmployeeInvite.objects.get(email='casey@example.test')
        outbox = EmailOutbox.objects.get(idempotency_key=f'employee-onboarding-invite:{invite.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(outbox.status, EmailOutbox.Status.SENT)
        self.assertEqual(mail.outbox[0].to, ['casey@example.test'])
        invite_link = next(
            line for line in mail.outbox[0].body.splitlines()
            if '/accounts/employee-invite/' in line
        )
        self.assertContains(response, 'Employee invite emailed.')
        self.assertContains(response, 'Sent to casey@example.test.')
        self.assertContains(response, 'one-time link as a backup')
        self.assertContains(response, invite_link)

    @override_settings(
        GCC_EMAIL_DELIVERY_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='',
    )
    def test_failed_delivery_keeps_invite_and_displays_copy_link(self):
        browser = TestClient()
        browser.force_login(self.owner)
        response = browser.post(
            reverse('operations:employee-invite-create'),
            {
                'email': 'casey@example.test',
                'first_name': 'Casey',
                'last_name': 'Lee',
                'group': self.group.pk,
            },
            follow=True,
        )
        invite = EmployeeInvite.objects.get(email='casey@example.test')
        outbox = EmailOutbox.objects.get(idempotency_key=f'employee-onboarding-invite:{invite.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(outbox.status, EmailOutbox.Status.FAILED)
        self.assertTrue(invite.is_usable)
        self.assertContains(response, 'Employee invite email failed.')
        self.assertContains(response, '/accounts/employee-invite/')
        self.assertEqual(mail.outbox, [])
