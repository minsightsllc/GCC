# Grand Coast Construction Operations Platform

This README is my walkthrough for the Grand Coast Construction website and internal operations platform. I wrote it in the order that I would set up, run, use, and test the application myself.

The project is a Django application for:

- Grand Coast Construction's public marketing website
- The private staff Operations workspace
- The employee Team workspace
- The secure client portal
- The lower-level Django administration interface, styled with Unfold

The visual system stays consistent everywhere: Grand Coast navy, blue, white, and gold; the supplied Grand Coast logo; Montserrat for interface text; and Merriweather for major headings.

## What I have built

I converted the original frontend prototype into a database-backed Django application. The browser is no longer the source of truth for leads, estimates, projects, tasks, users, documents, messages, or media.

The application now includes:

- Secure Django session authentication
- Owner, Manager, Office, Sales, and Field employee roles
- Employee profiles and one-time employee invitation links
- Client records and one-time client portal invitations
- Lead capture from the public contact form
- Lead assignment, statuses, priorities, notes, and tasks
- Lead-to-client conversion
- Server-calculated estimates with editable line items
- Client estimate review and acceptance
- Project creation from accepted estimates
- Project assignments, milestones, updates, documents, and media
- Unified internal tasks
- Internal schedule events and employee calendars
- Employee clock-in, clock-out, and time entries
- Two-way client and staff messaging
- Protected client-only documents and media
- Public project detail pages and gallery readiness
- Content Studio for public website content
- Optional Google Review link
- Unfold for the private /gccad/ interface
- Operations search from the Unfold admin home

I intentionally did not add Stripe, payment processing, payment webhooks, card collection, contact-form email notifications, external calendar synchronization, payroll functionality, or geolocation tracking.

## Project layout

I keep the Django project inside the website directory:

~~~text
GCC/
├── README.md
└── website/
    ├── manage.py
    ├── backend/
    │   ├── settings.py
    │   ├── urls.py
    │   ├── asgi.py
    │   └── wsgi.py
    ├── operations/
    │   ├── models.py
    │   ├── forms.py
    │   ├── views.py
    │   ├── urls.py
    │   ├── services.py
    │   ├── search.py
    │   ├── admin.py
    │   ├── migrations/
    │   ├── management/commands/seed_operations.py
    │   ├── templates/operations/
    │   └── static/operations/
    ├── templates/
    ├── db.sqlite3
    ├── media/
    └── venv/
~~~

The old demo app has been retired. Old /demo/... links only redirect to the current routes for bookmark compatibility; demo is not used in the visible navigation or page copy.

## 1. I prepare my local environment

I open PowerShell and move into the Django project:

~~~powershell
cd C:\dev\GCC\website
~~~

If I need to create the virtual environment from scratch, I run:

~~~powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
~~~

If PowerShell does not allow activation, I can use the virtual-environment Python executable directly for every command:

~~~powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

The main dependencies are:

- Django 6.1
- django-unfold 0.104.1
- django-storages with boto3 support for optional production object storage

For local development, the application uses SQLite and stores uploaded files under website/media/.

## 2. I apply the database migrations

I apply Django's migrations:

~~~powershell
.\venv\Scripts\python.exe manage.py migrate
~~~

The Operations migrations create and preserve the application data for:

- Clients and client invites
- Leads and staff assignment
- Estimates and estimate line items
- Projects and staff assignments
- Milestones and project updates
- Unified tasks
- Media assets and lead attachments
- Client messages
- Employee profiles and employee invites
- Schedule events
- Time entries
- Project documents
- Services, process steps, and site settings
- Activity history

The previous follow-up concept was migrated into the new Task model while preserving completion state. I do not delete or reset existing application data when I run migrations.

## 3. I create or confirm my owner account

I create a superuser if I do not already have one:

~~~powershell
.\venv\Scripts\python.exe manage.py createsuperuser
~~~

I use that account for:

- The separate Django/Unfold administration area at /gccad/
- The established Operations workspace at /dashboard/, with Projects as the canonical project record
- Employee access, assignments, schedules, and visibility controls
- Local setup and data inspection


Only an active superuser can enter /gccad/ or /dashboard/. The seed command automatically places an existing superuser in the Owner group, creates an EmployeeProfile for staff accounts, and creates a disabled AdminSecurityProfile for each superuser. The Owner group is reserved for this account; employee invitations offer Manager, Office, Sales, and Field.

## 4. I seed the local presentation data

I run the idempotent seed command:

~~~powershell
.\venv\Scripts\python.exe manage.py seed_operations
~~~

I can safely run this more than once. It uses stable identifying fields and does not duplicate the seeded records.

The seed command creates or preserves sample:

- Services and process steps
- Clients and leads
- Estimates and line items
- Projects and milestones
- Project updates
- Media records using the existing branded sample artwork
- Tasks and activity entries
- Site content

For a local client-portal walkthrough, I can create the seeded Maya client login by passing a password explicitly:

~~~powershell
.\venv\Scripts\python.exe manage.py seed_operations --client-password "ChangeThisLocalOnly123!"
~~~

That password is only for my local presentation database. I do not put credentials in source control, and the command has no hardcoded default password.

The command also creates these groups idempotently:

- Owner
- Manager
- Office
- Sales
- Field

I can verify the database state with:

~~~powershell
.\venv\Scripts\python.exe manage.py showmigrations
~~~

## 5. I start the website

I run the local server:

~~~powershell
.\venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
~~~

I then open:

- Public website: http://127.0.0.1:8000/
- Owner Operations workspace: http://127.0.0.1:8000/dashboard/
- Preserved workspace overview: http://127.0.0.1:8000/dashboard/overview/
- Employee Team workspace: http://127.0.0.1:8000/team/
- Authenticated client portal: http://127.0.0.1:8000/portal/
- Owner-only administration launchpad: http://127.0.0.1:8000/gccad/
- Sign-in page: http://127.0.0.1:8000/accounts/login/

## 6. I understand the account permissions

All staff accounts use Django's existing User model, Group model, and session authentication.

### Owner

I use an active superuser as the Owner. The Owner can use the full Operations workspace, manage team access, manage every operational record, control assignments and visibility, and use the native Django/Unfold user and group administration. The Owner group is a label for this account, not an employee invitation option.

### Manager

A Manager uses /team/ and cannot enter /dashboard/ or /gccad/. A Manager can view the complete Team calendar, including assigned employee names, and work with their own permitted assigned projects, tasks, internal reports, personal time entries, and clock-in/out. The calendar is read-only for Managers; they cannot create or edit schedules, change assignments, change employee access, or publish client-visible or public material.

### Office

An Office user uses /team/ and cannot enter /dashboard/ or /gccad/. Office visibility is assignment-scoped: assigned projects, personally relevant tasks, assigned schedule events, relevant client context, internal reports, personal time entries, and clock-in/out. Office users cannot view another employee's schedule or change anyone's access, assignments, or publication visibility.

### Sales

A Sales user uses /team/ for assigned leads, site visits, estimates, and follow-ups. Sales visibility is assignment-scoped and excludes project financials, internal costs, margins, and construction controls.

### Field

A Field user uses the dedicated Team workspace. A Field user can see only assigned projects, assigned tasks, relevant client context, personal schedule entries, personal time entries, internal updates, and permitted media reporting. Field uploads and reports are forced to Internal until the Owner publishes them. Field users cannot enter the staff dashboard, native Unfold admin, or another client's portal.

### Client

A client only sees the projects, estimates, milestones, client-visible updates, client-visible documents, permitted media, and messages connected to that client's own record.

I enforce these restrictions in both group permissions and view/queryset logic. I do not rely on hiding a link in the interface as a security boundary.

## 7. I walk through the public website

I start at / and check the public experience:

- The Grand Coast logo appears in the header and footer.
- The navy, blue, white, and gold palette is consistent.
- The home page includes the current headline, supporting copy, services, process, featured work, and contact call-to-action.
- The public navigation links to Services, Projects, Process, and Contact.
- The public layout works on desktop and mobile.

The public routes are:

- /
- /services/
- /projects/
- /process/
- /contact/

The public contact form uses normal Django form handling and CSRF protection. I can submit a test inquiry from /contact/, and it creates a real Lead record in the database.

## 8. I manage a new lead

I sign in as the Owner and open:

~~~text
/dashboard/leads/
~~~

I use the lead workspace to:

1. Search by name, service, location, or related client details.
2. Filter by lead status.
3. Select a lead to open its detail panel.
4. Assign the lead to a staff member.
5. Move the lead through New, Contacted, Qualified, Quoted, Won, or Lost.
6. Mark the lead as a priority.
7. Add or edit internal notes.
8. Add a task with a due date, priority, and assignee.
9. Review activity history.
10. Convert the lead into a Client when I am ready.

The lead conversion is explicit. A lead does not silently become a client just because it exists.

The intended business sequence is:

~~~text
Lead captured
    → Lead converted into Client
    → Estimate created
    → Estimate accepted by Client
    → Project created
    → Staff, tasks, milestones, documents, updates, and media assigned
~~~

Lead status changes, assignments, notes, priority changes, tasks, and conversion activity persist in the database.

## 9. I create client portal access

From the client workspace at:

~~~text
/dashboard/clients/
~~~

I can:

- Create or edit client records.
- See linked leads, estimates, and projects.
- Create a one-time client invite.
- Copy the invite link for the client.
- Revoke access.
- Review client messages and unread state.

When email delivery is enabled, creating the client invite also queues and sends the one-time link to the client email address. The copy link remains available in the Operations alert as a secure fallback. Local development keeps delivery disabled by default, so it is safe to test without contacting a real inbox.

For a real or development SMTP/provider delivery test, configure `GCC_EMAIL_DELIVERY_ENABLED=true`, `DEFAULT_FROM_EMAIL`, and the existing Django `EMAIL_*` settings. A local console or locmem backend is useful for confirming message contents but does not deliver to an external inbox. Pending and failed messages can be retried with:

~~~text
python manage.py dispatch_email_outbox --limit 100
~~~

The invite email is idempotent per generated invite, never logs the raw token, and delivery failure does not roll back the client invite.

The client invite:

- Is stored as a hash rather than a raw token.
- Expires after seven days.
- Can only be used once.
- Lets the client create a username and password.

The client completes the invite at:

~~~text
/accounts/invite/<token>/
~~~

After logging in, the client is redirected to /portal/.

## 10. I create and manage an estimate

I open:

~~~text
/dashboard/estimates/
~~~

I can create an estimate from a lead that already has a client association. I add line items with:

- Description
- Quantity
- Unit price
- Sort order

The estimate editor recalculates the subtotal and total in the browser for convenience, but the server recalculates the final Decimal total before saving. The browser is not trusted for business calculations.

I can:

- Save an estimate as Draft.
- Edit line items.
- Remove line items.
- Set an informational deposit amount.
- Send the estimate.
- Preview the customer-facing estimate.
- Decline or accept through the appropriate workflow.

The supported estimate statuses are:

- Draft
- Sent
- Accepted
- Declined

Once an estimate is accepted, it becomes read-only. If I need changes, I create a new estimate revision rather than silently changing the accepted record.

The deposit amount is informational only. No payment status, card collection, Stripe request, Checkout session, PaymentIntent, or webhook exists in this application.

## 11. I test client estimate acceptance

I sign in with the seeded client account or a client account created through an invite and open:

~~~text
/portal/
~~~

I confirm that the client can see only their own:

- Project information
- Estimate
- Deposit information
- Milestones
- Client-visible updates
- Client-visible documents
- Public/client media
- Project conversation

I click the estimate acceptance action. The application records:

- Accepted status
- Acceptance timestamp
- Accepting client user

The interface clearly explains that acceptance does not collect payment.

## 12. I convert an accepted estimate into a project

From the estimate workspace, I select the action to create a project from an accepted estimate.

The project form can include:

- Project title
- Client
- Lead
- Accepted estimate
- Assigned staff
- Location
- Project type
- Status
- Next step
- Summary
- Start date
- Target date
- Published state
- Cover image or fallback artwork

Lead-derived projects require the accepted-estimate workflow. I can still create standalone projects for work that did not originate from a lead.

I manage projects at:

~~~text
/dashboard/projects/
~~~

Inside a project I can:

- Change the project status.
- Assign employees.
- Toggle milestones complete or incomplete.
- Add project updates.
- Mark updates as client-visible or internal.
- Add documents.
- Add or edit media.
- Review activity history.
- Preview the client-facing project view.

## 13. I use unified tasks

I manage internal tasks at:

~~~text
/dashboard/tasks/
~~~

Tasks can be connected to:

- A lead
- A project
- A milestone

Each task supports:

- Title
- Description
- One assigned employee
- Watcher employees
- Status
- Priority
- Due date
- Completion timestamp
- Created/updated metadata

The task statuses are:

- Open
- In progress
- Blocked
- Complete

The priorities are:

- Normal
- High
- Urgent

I can filter tasks by employee, project, status, priority, due date, or search text. Task creation, reassignment, and completion add activity entries.

Employees work with their assigned tasks at:

~~~text
/team/tasks/
~~~

They can update permitted task fields and status, but they cannot see unrelated staff tasks.

## 14. I use the internal calendar

Only the Owner creates and edits schedule events at:

~~~text
/dashboard/calendar/
~~~

A schedule event supports:

- Title
- Assigned employees
- Optional project
- Optional task
- Start time
- End time
- Location
- Notes

The calendar is internal and does not synchronize with Google Calendar or Outlook. The Owner can also manage blank-by-default recurring employee templates at the bottom of the calendar page:

- one continuous shift per employee and weekday
- Pacific-time date overrides for adjusted hours or days off
- Clear / use weekly schedule to remove an exception without changing the template
- company-wide short and closed days that constrain or suppress effective employee hours

Closed and short days never delete schedule events or employee templates. Existing event validation prevents a new event from conflicting with a company closure or short-day window.

Managers view the complete read-only Team calendar at:

~~~text
/team/calendar/
~~~

Office and Field employees view only their own assigned schedule at:

~~~text
/team/calendar/
~~~

The Team workspace provides a monthly calendar with a mobile-friendly list fallback. Each event shows the complete range, such as Monday · 4:00 PM – 8:00 PM, in America/Los_Angeles. Clicking an employee event opens read-only details for non-Owners.

Schedule mutations also create persistent employee inbox notifications. When a native employee app is configured, the same notification is sent through Expo Push Service with the device default sound and a link back to the relevant calendar day. Client portal alerts use the same delivery path when a client has enabled notifications in the Expo app. Client payloads contain only the client-visible title, body, notification ID, type, and an authorized `/portal/...` destination; internal costs, margins, vendors, notes, and staff notifications are never included. Failed deliveries remain retryable with:

~~~powershell
python manage.py dispatch_push_notifications
~~~

The existing employee/owner device endpoints are `/team/notifications/devices/` and `/team/notifications/devices/deactivate/`. Client devices use `/portal/notifications/devices/` and `/portal/notifications/devices/deactivate/`; the mobile app selects the correct endpoint from the authenticated workspace. Device registration is scoped to the authenticated account, and logout or client account deletion deactivates that account's device. `EXPO_PUSH_ENABLED`, `EXPO_PUSH_URL`, and `EXPO_ACCESS_TOKEN` are shared by employee, owner, and client delivery. There is no separate client push flag.

## 15. I test employee clock-in and time tracking

I use the staff review page at:

~~~text
/dashboard/time/
~~~

Employees use:

~~~text
/team/time/
~~~

An employee can clock in and clock out. The application allows only one active time entry per employee.

A time entry can include:

- Employee
- Optional project
- Optional task
- Clock-in timestamp
- Clock-out timestamp
- Note
- Manager correction metadata

Only the Owner can correct time entries from /dashboard/time/. Employees can see their own recent entries and clock themselves in and out from /team/time/.

Database timestamps remain timezone-aware. The application uses America/Los_Angeles and displays times in 12-hour format, such as 1:05 PM.

This is operational time tracking only. I do not treat it as payroll, wage, overtime, geolocation, or compliance software.

## 16. I use the employee Team workspace

I open:

~~~text
/team/
~~~

The Team workspace includes:

- Personal overview
- Assigned projects
- Assigned tasks
- Today's schedule
- Time tracking
- Internal project reporting
- Permitted media reporting
- Employee profile
- Password change

Employees can update their own profile information at:

~~~text
/team/profile/<user-id>/
~~~

I use the staff team-management area at:

~~~text
/dashboard/team/
~~~

From there, the Owner can:

- Invite an employee.
- Choose the employee's group.
- View employee profiles.
- Update job title, phone, and active status.
- Control which projects, tasks, milestones, and schedule events employees can see.
- Create a one-time password reset link.

The employee onboarding link expires after seven days. Manager-issued password reset links expire after one day and can only be used once. Employees can also request a password-recovery email from the public sign-in page; those links expire after one hour and can only be used once. After five unmatched recovery-email submissions, that browser is paused for 15 minutes. Since onboarding and manager-issued reset email delivery is deferred, I copy those links manually.

Employees can update only their own profile details. They cannot change another employee's schedule, access, assignments, or visibility.

## 17. I prepare project documents

I upload and manage project documents at:

~~~text
/dashboard/documents/
~~~

Documents support:

- Project association
- Title
- Category
- Description
- File
- Internal visibility
- Client-visible visibility
- Uploader and timestamps

The upload uses a multipart Django form with file type and size validation. Common PDF and office document formats are supported.

Client-visible documents are delivered through an authenticated Django view. A client cannot access an internal document by guessing its URL, and a client cannot access a document belonging to another client's project.

## 18. I prepare media and future galleries

I manage media from:

~~~text
/dashboard/media/
~~~

The system supports:

- Images and videos
- Captions
- Project association
- Public visibility
- Client-only visibility
- Internal visibility
- Protected authenticated delivery for non-public files

The current media is presentation artwork and seeded sample content. I have not added new real construction photography.

When I have real photos and videos, I can upload them through the existing staff interface. The system is already prepared for:

- Public project galleries
- Client-only project media
- Internal staff media
- Image lightbox previews
- Video display
- Empty gallery states when a project has no real photography yet

Field employees cannot publish internal reporting media publicly.

## 19. I view public project pages

Published projects have public detail pages at:

~~~text
/projects/<project-uuid>/
~~~

A public project page can show:

- Project title and location
- Project summary and story
- Current public status
- Cover image or branded fallback artwork
- Public media gallery
- An empty gallery state when there is no public photography yet
- Google Review call-to-action when configured

Only published projects and public media are exposed on the public website.

## 20. I use Content Studio

I edit public website content at:

~~~text
/dashboard/content/
~~~

Content Studio controls:

- Homepage headline
- Homepage supporting copy
- Service names and descriptions
- Featured project information
- Process steps
- Google Review URL

After saving, the public pages read from the database and show the updated content. I do not need to edit templates for normal content changes.

The Google Review field is a normal external URL. If it is blank, the public review call-to-action does not render. There is no Google API, Places API, synchronization, or stored Google credential.

## 21. I use the native Unfold administration

I open:

~~~text
/gccad/
~~~

Unfold is the lower-level Django administration interface. It is separate from the branded Operations workspace at /dashboard/, and both are restricted to the active Owner superuser.

I use Unfold for:

- User and group administration
- Lower-level model inspection
- Permissions
- Database-backed record management
- History and change forms

The Unfold interface keeps the Grand Coast logo, navy/blue/white/gold palette, light theme, and Operations wording.

The Unfold admin search is extended to search Operations records, including:

- Clients
- Leads
- Employees
- Estimates
- Projects
- Tasks
- Schedule events
- Documents
- Messages
- Media
- Services
- Process steps
- Site settings
- Related milestones, updates, line items, and client details

When I select an Operations result, it opens the branded dashboard or Team workspace instead of sending me to an unrelated native edit page.

## 21A. I secure the private administration and install the PWA

I use the private administration entry at:

~~~text
/gccad/
~~~

The old /admin/ path intentionally returns 404. The public /accounts/login/ page is for employee and client accounts; it rejects superuser credentials with the same generic invalid-login response as any failed login. I do not reveal the private administration route through public navigation, the regular login page, or recovery pages.

When I open /gccad/, I first enter the administrator username or email. If that superuser has enabled PIN protection, I enter the six-digit PIN before the password form. If authenticator verification is enabled, I enter a fresh code from the authenticator app after the password is accepted. Direct requests to the login, model pages, or authenticator page cannot skip the gate.

After I sign in, I can open Security settings from the Unfold account area. I can enable or disable my own PIN and enroll or disable my own authenticator app without re-entering credentials. PIN setup uses two six-digit fields, and authenticator setup displays a QR code that I scan with my app before choosing the enable button. Existing superusers start with PIN and authenticator protection disabled until I turn them on.

If I forget the administrator password, I choose the password-recovery link on the private login or access screen and submit the verified administrator email. The form only proceeds for an active superuser email. An unmatched email stays on the form and shows the remaining attempts; after five unmatched submissions, that browser is paused for 15 minutes. A matching request uses the configured email backend to send a one-time link that expires after one hour. Password recovery changes only the password, does not disable PIN or authenticator protection, and never signs me in automatically.

If I forget the PIN or lose the authenticator, I choose the separate recovery link on the private access screen and submit the verified administrator email. The form only proceeds for an active superuser email. An unmatched email stays on the form and shows the remaining attempts; after five unmatched submissions, that browser is paused for 15 minutes. A matching request uses the configured email backend to send a one-time recovery link that expires after 30 minutes. Recovery lets me choose a new six-digit PIN, disables authenticator verification, revokes existing administrator sessions, and never signs me in automatically. Locally, email uses the console backend; production SMTP values come from environment variables.

The Security settings page also provides the active Owner superuser with an administration access watch. It records invalid administrator identifiers, passwords, PINs, authenticator codes, recovery verification failures, blocked attempts, and successful administrator sign-ins with the source IP, route, user agent, account when known, review state, and email-delivery state. It never records a password, PIN, authenticator code, session value, or recovery token. From that page I can search and review events, manually block an IP address for administration routes, lock an administrator's next sign-in, mark events reviewed, and reversibly unblock a target. Existing signed-in administrator sessions remain available so an accidental IP block can be removed. A signed-in administrator cannot lock their own account.

Failed administration authentication and security-verification events can send alerts to every active staff superuser with a non-empty, valid email address. Delivery is queued after the event is committed, so an SMTP failure never changes the login response. Configure `DEFAULT_FROM_EMAIL` and the existing Django email settings to enable delivery. The emergency `ADMIN_SECURITY_EMAIL_ALERTS_ENABLED=false` switch preserves event records while suppressing alert delivery. Because the alert policy intentionally emails every failed attempt, a brute-force attack can create substantial email volume; use a monitored security mailbox and review the dashboard as the primary audit trail. Failed or unavailable deliveries can be retried with:

~~~powershell
.\venv\Scripts\python.exe manage.py dispatch_admin_security_emails --limit 100
~~~

The administration security controls use `REMOTE_ADDR` by default. I leave `ADMIN_TRUSTED_PROXY_IPS` empty unless a known reverse proxy is terminating requests. Only explicitly listed proxy IPs are allowed to provide `X-Forwarded-For` or `Forwarded` client information; spoofed forwarding headers from other sources are ignored. For a trusted proxy deployment, I provide a comma-separated list of the proxy addresses, for example `203.0.113.10,2001:db8::10`. Blocks apply only to administration access and do not block employee, client, public, or other account routes.

The website is also installable as a Progressive Web App. Public pages register /service-worker.js and reference /manifest.webmanifest. Public static assets can be cached for faster repeat visits, while public navigation uses the network first and falls back to /offline/. The service worker never caches POST requests, credentials, CSRF tokens, administrator pages, Team pages, client portal pages, account pages, private documents, or private media. Private routes require a live connection.

I can install the PWA from the browser’s install prompt or address-bar install control. The application name is Grand Coast Construction, it opens at /, and it uses the existing Grand Coast logo and navy, blue, gold, and white color system.

## 22. I understand local file storage and production storage

During development, I use:

~~~text
MEDIA_ROOT = website/media/
MEDIA_URL = /media/
~~~

Django serves local media while DEBUG is enabled.

The production storage backend can be enabled only when I explicitly configure the environment:

~~~powershell
$env:USE_SUPABASE_STORAGE = "true"
$env:SUPABASE_S3_ENDPOINT = "https://<project>.storage.supabase.co/storage/v1/s3"
$env:SUPABASE_S3_ACCESS_KEY = "<access-key>"
$env:SUPABASE_S3_SECRET_KEY = "<secret-key>"
$env:SUPABASE_STORAGE_BUCKET = "<bucket-name>"
$env:SUPABASE_S3_REGION = "us-east-1"
~~~

I keep these values out of source control. If neither Supabase storage flag is enabled, the application makes no Supabase storage request and uses local filesystem storage.

For the current scoped rollout, I enable only contact-form attachments with USE_SUPABASE_CONTACT_STORAGE=true. This keeps the default storage for project, client, employee, and administrator uploads unchanged. USE_SUPABASE_STORAGE is the broader switch for moving every Django FileField to Supabase once those workflows are ready. The public URL base is not required; contact files use the protected storage path and signed access behavior.

Contact-form attachments are stored under the contact-form/YYYY/MM/ prefix inside the configured bucket. Other upload fields keep their existing projects/... prefixes until their client, employee, and administrator sharing workflows are finalized.

Before a real deployment, I also configure:

~~~powershell
$env:DJANGO_SECRET_KEY = "<long-random-production-secret>"
$env:DJANGO_DEBUG = "false"
$env:ALLOWED_HOSTS = "grandcoastconstruction.com,www.grandcoastconstruction.com"
$env:GCC_TURNSTILE_ENABLED = "true"
$env:GCC_TURNSTILE_ALLOW_MISSING = "false"
$env:GCC_MOBILE_TURNSTILE_BYPASS_ENABLED = "false"
~~~

I do not use the development secret key or DEBUG=true in production.

The Projects tab is the standard project workspace. Legacy links to the
construction command center, weekly action list, and expanded Project
Operations page return to the existing Overview or Projects tab. Generic
readiness and closeout checklists are not automatically created for new
projects; the Owner will define any additional construction workflow before
it is exposed in the admin UI.

Construction execution services remain behind a separate pilot switch for
backend and staging validation:

~~~powershell
$env:GCC_EXECUTION_LOOP_ENABLED = "true"
$env:GCC_EXECUTION_LOOP_PROJECT_IDS = "<pilot-project-uuid>"
$env:GCC_EXECUTION_LOOP_USER_IDS = "<owner-uuid>,<manager-uuid>,<field-uuid>"
$env:GCC_AI_ENABLED = "false"
~~~

When either allowlist is non-empty, a user or project must be explicitly
listed before execution-loop API actions and derived calendar signals are
available. This pilot flag does not expose the retired construction pages in
the admin UI.

For production security-alert delivery, I also set the existing Django SMTP settings and the alert controls:

~~~powershell
$env:DEFAULT_FROM_EMAIL = 'security@grandcoastconstruction.com'
$env:EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
$env:EMAIL_HOST = 'smtp.example.com'
$env:EMAIL_PORT = '587'
$env:EMAIL_HOST_USER = '<smtp-user>'
$env:EMAIL_HOST_PASSWORD = '<smtp-password>'
$env:EMAIL_USE_TLS = 'true'
$env:EMAIL_USE_SSL = 'false'
$env:ADMIN_SECURITY_EMAIL_ALERTS_ENABLED = 'true'
# Only when a known reverse proxy is in front of Django:
$env:ADMIN_TRUSTED_PROXY_IPS = '203.0.113.10'
~~~

`DEFAULT_FROM_EMAIL` and `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` must be configured for delivery. I keep SMTP credentials out of source control. I set `ADMIN_TRUSTED_PROXY_IPS` only to actual proxy addresses, never to a broad network range.

## 23. I run the automated checks

I run Django's system checks:

~~~powershell
.\venv\Scripts\python.exe manage.py check
~~~

I confirm that no model changes are waiting for a migration:

~~~powershell
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
~~~

I run the Operations test suite:

~~~powershell
.\venv\Scripts\python.exe manage.py test operations --verbosity 1
~~~

For a disposable full-lifecycle smoke test, I use the guarded launcher from
the website directory:

~~~powershell
.\run_operations_simulation.ps1
~~~

The launcher creates a fresh SQLite database and media directory beneath the
operating-system temporary folder, migrates it, creates dummy role accounts,
and exercises the inquiry-to-warranty workflow with retries, permissions,
uploads, schedule conflicts, financial calculations, notifications, and
weekly review data. It prints generated pilot IDs and temporary credentials,
writes a sanitized JSON report, and removes the temporary data unless I pass
`-Keep`. It refuses to use the normal development database or media folder and
requires `GCC_AI_ENABLED=false`.

The current suite covers:

- Migration and seed behavior
- Idempotent Owner, Manager, Office, Sales, and Field groups
- Employee invitation creation, expiry, and one-time use
- Password setup and manager-issued reset links
- Owner-only Operations and Unfold access
- Manager, Office, Field, client, inactive, ungrouped, and anonymous access restrictions
- Role-aware Team calendar visibility and read-only employee calendar behavior
- Lead creation, assignment, status, priority, and client conversion
- Estimate creation, line-item Decimal totals, status transitions, and acceptance
- Accepted estimate to project conversion
- Task assignment, watcher access, status, priority, due dates, and activity
- Schedule event visibility
- Clock-in/out, duplicate active-entry prevention, corrections, and Pacific display
- Document upload validation and protected delivery
- Client-visible versus internal updates, documents, media, and messages
- Two-way client/staff messaging and unread behavior
- Public project pages and public-media filtering
- Google Review link visibility
- Public content updates
- Client Expo push registration, portal-only notification destinations, post-commit delivery, redacted payloads, duplicate prevention, and invalid-token deactivation
- Payment exclusion
- No business state stored in browser local storage

I expect the checks to finish with no system-check errors, no migration changes, and all Operations tests passing.

## 24. I manually test the main workflows

After starting the server, I test the application in this order.

### Public website

1. I open /.
2. I click Services, Projects, Process, and Contact.
3. I submit a test contact form.
4. I verify that a new lead appears in /dashboard/leads/ as the Owner.
5. I resize the browser to a mobile width and confirm there is no horizontal overflow.

### Owner workflow

1. I log in at /accounts/login/.
2. I open /dashboard/ as the Owner.
3. I confirm the fixed navigation and staff account area remain visible.
4. I open Leads, Clients, Tasks, Estimates, Projects, Calendar, Time, Documents, Media, and Content.
5. I assign a lead and change its status.
6. I convert the lead into a client.
7. I create an estimate and edit its line items.
8. I send the estimate.
9. I create or use a client invite.
10. I create a project after the estimate is accepted.
11. I assign an employee and toggle a milestone.
12. I add a project update and choose whether it is client-visible.
13. I upload an internal document and a client-visible document.
14. I add a task and schedule event.
15. I review messages and reply to a client.

### Employee workflow

1. I create an employee invitation from /dashboard/team/.
2. I copy the generated link.
3. I open the link in a private browser window.
4. I create the employee username and password.
5. I sign in and verify that the employee lands in /team/.
6. I confirm the employee sees only assigned projects, tasks, schedules, and time entries.
7. I update a task.
8. I add an internal project update.
9. I clock in and clock out.
10. I confirm that the employee cannot open /dashboard/, /gccad/, or another employee's information, and cannot create or edit schedules.

### Client workflow

1. I create or use a client invite from /dashboard/clients/.
2. I copy the invite link.
3. I open it in a separate private browser window.
4. I create the client's login.
5. I verify that the client lands in /portal/.
6. I review the estimate and accept it.
7. I verify that no payment screen or payment request appears.
8. I check project status, milestones, client-visible updates, media, and documents.
9. I send a message to staff.
10. I verify that the client cannot view another client's project or internal records.

### Unfold workflow

1. I open /gccad/ as the Owner.
2. I confirm the Grand Coast logo and colors.
3. I use the admin search for a known lead, project, estimate, or client.
4. I click the result.
5. I verify that it opens the relevant Operations dashboard or Team page.
6. I open the native user and group pages when I need low-level account administration.

## 25. I test the responsive design

I check at least one desktop size and one narrow mobile size. I verify:

- The navy sidebar remains usable.
- The staff account section stays visible in the Operations navigation.
- Tables and split panes do not create accidental page-wide horizontal scrolling.
- The mobile navigation can collapse and reopen.
- Forms remain readable and submit buttons remain reachable.
- The client portal remains usable as a future mobile webview.
- Public galleries and project fallback artwork scale correctly.
- The Unfold login, home, list, and form pages do not clip.

## 26. I check the security boundaries

I verify that:

- Anonymous visitors can view public pages but not staff or client workspaces.
- Only the active Owner superuser can access /dashboard/ and /gccad/.
- Managers, Office, and Field employees can access only the permitted /team/ records.
- Clients cannot access /dashboard/, /team/, or /gccad/.
- Employees cannot access another employee's schedule, access controls, assignments, or private records.
- Public pages expose no Operations or Client Portal destination links.
- Managers can view the full Team calendar but cannot create or edit its schedule events.
- Office and Field employees see only their personally assigned calendar events.
- Internal project updates, media, and documents are protected.
- Client-only files require authentication and project ownership.
- Invite links are hashed, expire, and cannot be reused.
- Forms use CSRF protection.
- Administration security events are visible only to active staff superusers, and security block/review actions are CSRF-protected and audit-logged.
- Failed administration authentication attempts capture no credential secrets and alert the configured administrator email recipients when enabled.
- Administration IP blocks and administrator-login locks are reversible and do not affect employee, client, or public routes.
- Forwarded client-IP headers are ignored unless the connecting proxy address is listed in `ADMIN_TRUSTED_PROXY_IPS`.
- Estimate totals are recalculated server-side.
- Accepted estimates cannot be edited in place.
- No business data is stored in localStorage.
- No Stripe, payment, webhook, or card request is made.
- No Supabase request is made unless I explicitly enable production storage settings.

## 27. I prepare for a future deployment

I keep the current GoDaddy website untouched while I review this Django application with the owner.

Before deployment, I would:

1. Set a strong production DJANGO_SECRET_KEY.
2. Turn DJANGO_DEBUG off.
3. Set the correct ALLOWED_HOSTS.
4. Configure the production database.
5. Decide whether to enable Supabase S3-compatible storage.
6. Configure HTTPS and secure cookie settings.
7. Run collectstatic.
8. Create real staff accounts and remove any local presentation accounts.
9. Upload real project photography and documents.
10. Confirm the owner approves the content, roles, workflows, and client-facing language.
11. Deploy the Django application separately from the existing GoDaddy site.
12. Keep payment processing deferred until the business requirements are discussed and approved.

For static assets in a deployment environment, I run:

~~~powershell
.\venv\Scripts\python.exe manage.py collectstatic --noinput
~~~

## Current platform boundary

This is now a real backend-powered construction-management foundation, but it is still intentionally focused on the agreed first phase:

- Local SQLite for development
- Local filesystem media by default
- Optional Supabase S3-compatible storage configuration
- Manual copying of onboarding invites and manager-issued reset links
- Internal calendar, recurring employee scheduling, and employee notification inbox
- Informational estimate deposits
- No payment collection
- No contact-form email notifications
- No external calendar synchronization
- No real construction photography added yet
- No payroll or compliance timekeeping

That boundary lets me demonstrate the complete Grand Coast workflow now while keeping the application ready for the owner's decisions about production hosting, communications, photography, documents, payments, and future integrations.

## Main Testing Staging -> Production Phase

The non-AI code path is now covered by the full Operations suite and the
disposable simulation. Before production rollout, I still need staging
validation, backup review, a selected real-project pilot, and real iOS
simulator/device testing of camera, photo-library, document-picker,
interrupted-upload, and offline-retry behavior. Production also requires the
usual HTTPS, secrets, object-storage, database, backup, and deployment
safeguards.

The current Operations suite has 164 passing tests. AI remains disabled until
the real staging pilot completes successfully.

## 28. I run development certification before staging

The repository includes a disposable certification runner for the complete
non-AI Operations workflow. It is intentionally separate from the normal
development database and media directory. It creates temporary users and
records, runs the inquiry-to-warranty lifecycle, checks permissions and
financial redaction, exercises protected uploads, and writes a JSON report.

Certification must never use production credentials, production databases,
production storage, real client documents, or real client contact information.
The runner refuses to operate unless simulation mode, the execution loop, the
storage smoke gate, and GCC_AI_ENABLED=false are all set.

### Development certification prerequisites

I need:

- Python dependencies installed in website/venv.
- Django migrations available locally.
- PowerShell 5.1 or newer.
- Docker Desktop with a local S3-compatible emulator for the Emulator and
  Both modes.
- Provider credentials for a separate private development bucket for Provider
  and Both modes.
- Node.js/npm for browser smoke tests.
- A browser installed and available to Playwright CLI for the Browser option.

The application itself can run with local SQLite and local media, but the
release certification gate uses both storage targets. The local emulator is
deterministic and the real development bucket catches provider-specific
behavior.

### Emulator setup

The launcher defaults to MinIO at 127.0.0.1:9000 with the following
development-only values:

~~~powershell
docker run --name gcc-certification-minio -p 9000:9000 -p 9001:9001 -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data --console-address ":9001"
~~~

The launcher creates the gcc-certification bucket when it does not exist and
uses a unique certification/run-id/ object prefix. I stop and remove this
container only after confirming that no other local project uses it:

~~~powershell
docker stop gcc-certification-minio
docker rm gcc-certification-minio
~~~

Do not point the emulator variables at a shared or production endpoint.

### Private development bucket setup

The provider target must be a private, development-only S3-compatible bucket.
The access key must be limited to that bucket and must not be a production
service key. Set these values only in the current PowerShell process, a local
secret manager, or an ignored environment file:

~~~powershell
$env:GCC_STORAGE_SMOKE_ENABLED = "true"
$env:USE_SUPABASE_STORAGE = "true"
$env:SUPABASE_S3_ENDPOINT = "https://<development-s3-endpoint>"
$env:SUPABASE_S3_ACCESS_KEY = "<development-only-key>"
$env:SUPABASE_S3_SECRET_KEY = "<development-only-secret>"
$env:SUPABASE_STORAGE_BUCKET = "gcc-development-private"
$env:SUPABASE_S3_REGION = "<development-region>"
~~~

The bucket must reject anonymous reads. The certification suite checks that
an unsigned object URL cannot retrieve a file and that Django's protected
delivery view remains the authorization boundary.

The certification runner also sets:

~~~text
GCC_STORAGE_PREFIX=certification/<run-id>/<mode>
~~~

The prefix is validated against traversal and unsafe characters, and is
removed during cleanup unless -Keep is supplied. Do not manually substitute a
production bucket or a production prefix.

### Full certification commands

From the repository root:

~~~powershell
cd website
.\run_operations_certification.ps1 -StorageMode Emulator
~~~

The final local gate uses both storage targets:

~~~powershell
.\run_operations_certification.ps1 -StorageMode Both
~~~

To retain the disposable database, media, report, and browser artifacts for
inspection:

~~~powershell
.\run_operations_certification.ps1 -StorageMode Both -Browser -Keep
~~~

The command prints the retained temporary root and generated pilot IDs. It
does not print passwords, cookies, session values, storage secrets, or API
credentials. The report removes secret-shaped fields before it is written.

The runner creates these disposable identities:

~~~text
Owner, Office, Project Manager, Sales, Field, Subcontractor, Client,
Unauthorized user
~~~

The automated lifecycle simulation below is a backend regression harness. It
does not define the currently exposed admin workflow or a required sequence
of general-contracting steps:

~~~text
Inquiry -> Lead -> Site Visit -> Estimate -> Agreement -> Deposit ->
Readiness -> Construction -> Field Reports -> Media -> Inspection failure
and correction -> Selection -> Procurement -> Change Order -> Milestone ->
Payment -> Closeout -> Warranty
~~~

It also checks retry idempotency, stale updates, calendar conflicts,
notification failure isolation, protected files, direct URL/API boundaries,
financial redaction, and attention-feed and weekly-review service data.

### Browser smoke commands

The browser runner uses Playwright CLI rather than a test framework. It logs
in with the generated disposable accounts and checks the standard Overview
and Projects workspaces, legacy construction-route redirects, calendar, field
view, client portal, financial redaction, protected documents, and direct
unauthorized URLs.

Run it through the certification launcher:

~~~powershell
.\run_operations_certification.ps1 -StorageMode Emulator -Browser -Keep
~~~

If a browser is missing, install a browser supported by the local Playwright
CLI and rerun the command. The launcher does not silently download a browser
or weaken the test when none is installed.

The browser target is localhost by default. A remote target is rejected unless
the explicit staging guard is present:

~~~powershell
$env:GCC_CERTIFICATION_TARGET = "staging"
.\run_browser_smoke.ps1 -BaseUrl "https://<staging-domain>" -ProjectId "<generated-staging-project-uuid>" -ReportPath "$env:TEMP\gcc-browser-report.json" -ArtifactDirectory "$env:TEMP\gcc-browser-artifacts" -AllowRemote
~~~

The remote run must use staging-only accounts. Never pass a production URL,
production cookie, or production credential to this command.

### Role-by-role acceptance matrix

| Role | Must be able to use | Must not be able to use |
| --- | --- | --- |
| Owner | Overview, Projects tab, all authorized company operations and financials | Nothing outside the owners authorized company scope |
| Office | Leads, clients, messages, scheduling, documents, permitted payment records | Restricted internal financial details outside its policy |
| Project Manager | Assigned projects, tasks, schedules, and field activity | Unassigned projects and unrelated financial records |
| Sales | Leads, appointments, estimates, follow-ups | Project costs, margins, internal notes, vendor details |
| Field | Todays work, scope, tasks, plans, photos, daily reports, materials, problems | Financials, margins, internal vendor data, unrelated projects |
| Subcontractor | Assigned work package and explicitly shared project records | Other projects, client-private records, financials, internal notes |
| Client | Own project progress, decisions, selections, documents, change orders, payment schedule | Internal costs, margins, vendor details, internal notes, other projects |
| Unauthorized | Publicly permitted pages only | Operations, client portal, project APIs, documents, and media |

Each role is tested both through visible navigation and through direct URLs/API
requests. Hiding a link is not considered authorization.

### iOS WebView smoke checklist

Native media is a thin capability bridge around the Django WebView workflow;
there is no SwiftUI screen architecture. Use a retained local certification
run and a disposable Field account.

1. Start the retained server on the LAN address, not only 127.0.0.1:

~~~powershell
$env:GCC_SIMULATION_MODE = "true"
$env:GCC_AI_ENABLED = "false"
$env:GCC_NATIVE_MEDIA_ENABLED = "true"
$env:GCC_DATABASE_PATH = "<retained-certification-root>\db.sqlite3"
$env:GCC_MEDIA_ROOT = "<retained-certification-root>\media"
$env:DJANGO_DEBUG = "true"
.\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8001
~~~

2. Open the LAN URL in the existing Expo WebView build or use a temporary
   HTTPS tunnel that points only to this disposable server.
3. Log in as the generated Field user.
4. Verify camera photo capture, photo-library selection, video capture, and
   document picker.
5. Cancel each picker and confirm the page remains usable.
6. Try an invalid extension, oversized file, unsafe filename, and an upload
   to an unrelated project. Each must be rejected server-side.
7. Interrupt the network during upload, reconnect, and confirm the private
   retry queue submits exactly one authorized attachment.
8. Confirm progress, retry, and failure states are visible.
9. Confirm the app does not persist passwords, session secrets, recovery
   tokens, or API credentials in plaintext storage.
10. Confirm the client, subcontractor, and unrelated Field accounts cannot
    retrieve the uploaded file.

The physical iPhone is authoritative for camera and video behavior. Desktop
and mobile-web tests prove server authorization and responsive behavior but
do not replace the device pass.

### Backup and restore verification

Before any staging migration:

1. Make a timestamped database backup.
2. Record the migration number and application commit.
3. Copy the staging media/object-storage backup or verify the providers
   versioned backup policy.
4. Restore into a separate disposable database and storage prefix.
5. Run Django checks, migration checks, protected-file checks, and the
   certification read-only verification against the restore.
6. Record the restore time, row/file counts, and any missing object.

A backup is not accepted merely because it exists; the restore must be
readable and the protected delivery rules must still hold.

## 29. I rehearse staging after development certification

Staging is the deployment-infrastructure rehearsal, not the first functional
test. I create separate staging resources:

- Staging database
- Private staging object-storage bucket
- Staging-only storage credentials
- Email sink or test mail provider
- Staging-only push configuration
- HTTPS domain and trusted host configuration
- Selected pilot users and one pilot project

I keep AI disabled:

~~~powershell
$env:GCC_AI_ENABLED = "false"
$env:GCC_EXECUTION_LOOP_ENABLED = "true"
$env:GCC_EXECUTION_LOOP_PROJECT_IDS = "<staging-pilot-project-uuid>"
$env:GCC_EXECUTION_LOOP_USER_IDS = "<owner>,<manager>,<office>,<field>,<client>"
~~~

After a verified backup, I run:

~~~powershell
.\venv\Scripts\python.exe manage.py check --deploy
.\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\venv\Scripts\python.exe manage.py migrate --plan
.\venv\Scripts\python.exe manage.py collectstatic --noinput
~~~

I then run the same certification workflow against the staging deployment.
The remote browser run requires GCC_CERTIFICATION_TARGET=staging and
-AllowRemote. I verify HTTPS, secure cookies, CSRF, signed/protected storage
delivery, email links, push notifications, audit events, authorization
failures, failed deliveries, logs, backup, and restore before expanding the
pilot.

The staging pilot sequence is:

1. Generate or seed only disposable staging records.
2. Allowlist one project and selected Owner, Office, Project Manager, Field,
   Subcontractor, and Client users.
3. Run the lifecycle from inquiry through warranty.
4. Review the JSON report, audit history, notifications, storage cleanup,
   financial calculations, and authorization failures.
5. Fix and rerun any failed check.
6. Expand to additional staging projects only after the pilot is clean.

No certification command accepts a production URL. Production requires a
separate release decision and a fresh backup/restore verification.

## 30. I use the final release gate

The non-AI release gate is complete only when all of these have evidence:

~~~text
Django checks
Migration dry-run
Full Operations test suite
Full lifecycle simulation
Permission matrix
Private storage emulator tests
Real development-bucket smoke test
Browser role matrix
Financial and calendar tests
Notification failure isolation
Protected-file tests
Mobile dependency checks
iOS media smoke checklist
Backup and restore verification
Staging pilot report
~~~

The certification report must show:

- All checks passed.
- No secret leakage.
- No duplicate lifecycle records.
- No cross-project access.
- No financial redaction failures.
- No protected-file bypass.
- Storage cleanup completed, or the retained path is recorded.
- Browser artifacts generated when browser mode was requested.
- Temporary database, media, and emulator prefix removed unless -Keep was
  explicitly supplied.

Artifact locations:

- Disposable certification root: the path printed by the runner when -Keep is
  supplied.
- JSON lifecycle report: certification-report.json in that root.
- Browser report: browser-report.json in that root.
- Browser screenshots and CLI artifacts: the browser-artifacts directory in
  that root.

Troubleshooting:

- Missing browser: install a local supported browser, then rerun with
  -Browser. Do not mark browser certification passed without artifacts.
- Missing MinIO/Docker: start the local emulator or run a Provider-only
  development-bucket smoke test; the Both gate still requires both targets.
- Missing provider credentials: confirm the endpoint, bucket, region, and
  bucket-scoped development key in the current process. Never copy production
  secrets.
- Upload failures: inspect the sanitized report and Django audit events; check
  content signature, size, protected delivery, prefix, and bucket policy.
- Retained temporary data: inspect the printed root, then remove that exact
  generated directory after review. Do not use a wildcard or a workspace root
  for cleanup.
- No remote browser access: confirm GCC_CERTIFICATION_TARGET=staging and use
  -AllowRemote only with a staging HTTPS URL.

AI remains disabled for every development, certification, and staging run.


## 31. Disposable manual-test account logins

The following accounts are seeded only in the disposable manual-certification
database. They are intended to make the complete manual workflow testable
without manually creating employees or accepting invitation links.

These credentials are fake, temporary, and must never be used in staging,
production, or with real client information.

| Name | Role | Login email / username | Password | Login location |
| --- | --- | --- | --- | --- |
| Manual Owner | Owner / Admin | `owner@manual.test` | `GccManual-Owner-2026!` | `/gccad/` |
| Mike Rivera | Manager | `manager@manual.test` | `GccManual-Manager-2026!` | `/accounts/login/` |
| Olivia Bennett | Office | `office@manual.test` | `GccManual-Office-2026!` | `/accounts/login/` |
| Ethan Cole | Field | `field@manual.test` | `GccManual-Field-2026!` | `/accounts/login/` |
| Sam Lopez / Harbor Electrical LLC | Subcontractor | `subcontractor@manual.test` | `GccManual-Subcontractor-2026!` | `/accounts/login/` |
| Taylor Unassigned | Unassigned Field test user | `unauthorized@manual.test` | `GccManual-Unassigned-2026!` | `/accounts/login/` |

The Owner account is a superuser and enters through the protected administration
gate at `/gccad/`. Managers, Office, Field, Clients, and Subcontractors use
the normal account login flow. The `unauthorized@manual.test` account is an
active Field user intentionally left unassigned for project-boundary tests.

The temporary server is available at:

~~~text
http://127.0.0.1:8001/
~~~

The disposable database is:

~~~text
C:\Users\isaia\AppData\Local\Temp\gcc-manual-c98a3fc4e2864860ab697369c4484dbe\db.sqlite3
~~~

The database is configured with:

- AI disabled (`GCC_AI_ENABLED=false`).
- Execution-loop testing enabled.
- Local email and push delivery disabled.
- No pending employee invitations.
- Only Owner, Manager, Office, and Field internal roles.
- An active Harbor Electrical LLC subcontractor portal identity.

The clean workflow state is:

~~~text
Leads:       0
Estimates:   0
Projects:    0
Tasks:       0
Site visits: 0
Pending invites: 0
Clients:      0
~~~

The client portal identity is intentionally not pre-seeded. Create a dummy
Client record through the Operations workspace, create its client portal
invite, and complete that invite with a temporary password. Use the same
client email for the website inquiry so lead conversion can reuse the client
record instead of creating a duplicate.

### Manual test start

1. Open `/gccad/` and sign in as `owner@manual.test`.
2. Open Overview, then use Projects to manage a project record.
3. Create a dummy Client record and client portal invite through Operations.
4. Complete the client invite in a separate browser profile and establish a
   temporary client password.
5. Submit a website inquiry using that same client email.
6. Convert the lead and create the estimate, agreement, deposit, and project.
7. In the Projects tab, select the project and assign:
   - `manager@manual.test` as Project Manager.
   - `office@manual.test` as Office staff.
   - `field@manual.test` as Field staff.
8. Log in separately as each employee and verify their scoped workspace.
9. Use the Client account to approve selections, change orders, and review
   shared project information.
10. Verify the shared milestone sequence, project updates, employee tasks,
    and the currently implemented project status workflow. Additional
    readiness and closeout steps await Owner requirements.

Refresh or sign out and back in if a browser session was open before the
accounts were seeded. The temporary credentials and database are disposable;
delete the exact generated `gcc-manual-*` directory after testing and never
delete a broad temporary or workspace directory.

## 32. Estimate details stay in the service your team chooses

Grand Coast does not connect to or synchronize with a third-party estimate
service. That service remains responsible for detailed estimates, signatures,
invoices, payment collection, reminders, and its own customer notifications.
Grand Coast stores only the client, a protected estimate link, a lightweight
status, and the confirmation history needed to continue its own workflow.

The estimate link/status layer is automatic. No estimate-specific environment
variables, pilot IDs, or allowlists are required. Once an authorized staff
member creates an estimate record, the estimate link and status actions are
available according to the normal server-side permissions.

AI remains disabled during this phase:

~~~powershell
$env:GCC_AI_ENABLED = "false"
~~~

The normal flow is: create the client and lead; open Estimates in Operations;
enter only the client and the secure estimate link; then either send the link
through your usual email/text process, use the optional "Send link to client"
action in GCC, or publish it to the client portal. Update the lightweight
status in GCC when the client responds, then continue the agreement, deposit,
project, construction, portal-update, and closeout workflows. Do not enter
provider credentials, payment credentials, API keys, or detailed estimate line
items into GCC.

Owner, Manager, and Office users can change the manual status. Field users and
clients can see only the permitted lightweight status surface. Clients can open
an estimate link only after staff explicitly publishes that link. Invoice and
payment links are handled the same way: GCC records only links and confirmed
status, while the detailed commercial document remains in the selected service.

## 33. Google Reviews carousel

The public homepage can show up to five cached Google Places reviews in the section
immediately below the hero. Google supplies the returned relevance order; GCC does
not rewrite review text. The existing review-link CTA remains available separately.

Keep the Google Places credentials only in website/.env:

~~~dotenv
GCC_GOOGLE_PLACES_API_KEY=your_server_side_key
GCC_GOOGLE_PLACE_ID=your_grand_coast_place_id
GCC_GOOGLE_PLACES_TIMEOUT_SECONDS=8
~~~

The API key is read only by Django. It is never placed in HTML, browser JavaScript,
the mobile environment, or database content. The Owner opens Content Studio and
selects “Sync Google reviews” after the values are configured. A successful sync
replaces the cache and records the last-sync time; a failed request leaves the last
successful reviews visible. If no cache exists, the carousel stays hidden and the
existing Google review link is still available.

Run the migration and an initial sync from the website directory:

~~~powershell
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py sync_google_reviews
~~~

Schedule the sync command daily in the deployment environment. The Places API
returns a maximum of five reviews per request, and each displayed card includes
Google attribution plus a direct “Read on Google” link.

## 34. Mobile owner Operations access

The existing admin/superuser account can optionally use the Expo WebView as the Owner
workspace. This does not create a new Owner role, does not expose the `/gccad/` admin
catalog in the app, and does not change normal browser login behavior.

Enable it only for a development or staging test session:

~~~powershell
$env:GCC_MOBILE_OWNER_ACCESS_ENABLED = "true"
$env:GCC_MOBILE_OWNER_PUSH_ENABLED = "true"
$env:GCC_AI_ENABLED = "false"
~~~

The mobile app opens `/accounts/login/?mobile=1`. The owner must still complete the
existing administrator PIN and authenticator verification steps when those protections
are enabled. Only after the factors succeed is the Django session created, and the app
lands at `/dashboard/` (the standard Overview). The app redirects any `/gccad/` navigation
back to `/dashboard/`; browser access to `/gccad/` remains available through its existing
protected administration gate.

The mobile owner flags are disabled by default. Keep the server-side flags authoritative;
the optional mobile setting below only controls whether the native shell attempts owner
push registration before the server accepts it:

~~~dotenv
EXPO_PUBLIC_MOBILE_OWNER_PUSH_ENABLED=true
~~~

Owner push registration is accepted only for a marked Grand Coast mobile WebView while
`GCC_MOBILE_OWNER_PUSH_ENABLED=true`. Push links are permission-checked Operations paths
and never open `/gccad/`. Logout deactivates the registered device. Passwords, PINs,
authenticator codes, session cookies, recovery tokens, and push credentials are not
stored in native plaintext storage or written to logs.

### Mobile owner validation checklist

1. Leave `GCC_MOBILE_OWNER_ACCESS_ENABLED=false` and verify the superuser is rejected by
   the mobile login form.
2. Enable the access flag, sign in through the Expo app, and verify the owner lands on
   the standard Operations Overview.
3. If enabled, verify the PIN page appears before a session exists; then verify the
   authenticator page appears before a session exists.
4. Open the app drawer and verify Operations links work while `/gccad/` redirects to
   the standard Overview.
5. Enable owner push, register a device, open an Operations notification, and verify the
   destination is internal and not `/gccad/`.
6. Log out and verify the device is deactivated.
7. Repeat the same checks with the flags disabled and confirm employee and client login
   behavior is unchanged.

Do not enable these flags in production until HTTPS, secure cookies, Turnstile, PIN/TOTP,
push credentials, audit events, and logout behavior have been verified in staging. The
same superuser credentials can still access `/gccad/` in a browser by design; restricting
one credential cryptographically to only one app would require a separate scoped account
or device-attestation system.
