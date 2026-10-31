export const LEGAL_DOCUMENTS = {
  privacy: {
    title: 'Privacy Policy',
    updated: 'Effective October 30, 2026',
    intro: 'This policy describes how Grand Coast Construction Inc. ("Grand Coast," "we," or "us") handles information when you visit our website, use the mobile app, submit a project inquiry, or use a client or employee account.',
    sections: [
      { title: 'Information you provide', paragraphs: [
        'A project or estimate request may include your name, email address, phone number, project type, location, message, and any photos or files you choose to attach.',
        'When you use an account, we process account and profile details and the records needed to operate the workspace. These can include project, task, schedule and time, message, update, notification, document, and media records.',
      ] },
      { title: 'App, device, and website information', paragraphs: [
        'The website uses necessary session and security information, including cookies and CSRF tokens, to keep you signed in and protect form submissions. The mobile app uses the website inside a WebView for account and project features.',
        'If you allow push notifications, the app registers a device push token and platform with your account so we can deliver alerts. You can turn off notifications in your device settings.',
      ] },
      { title: 'How we use information', paragraphs: [
        'We use information to respond to inquiries, prepare and manage projects, provide account and portal features, coordinate work, send messages and notifications, protect accounts, prevent misuse, and maintain business and operational records.',
      ] },
      { title: 'Who can see information', paragraphs: [
        'Access depends on account role and project permissions. Grand Coast staff can access information needed for their work. Clients can see information and files shared with their account. Employees can see the work and records available to their role.',
        'Project photos and videos appear on the public website only after an authorized administrator publishes them. Client-shared files and internal files are handled separately from public website publishing.',
      ] },
      { title: 'Service providers', paragraphs: [
        'We use service providers to operate the website and app. Production uploads are stored with Supabase Storage; development uses local media storage. Depending on configuration and the feature used, providers may include our hosting and email services, Expo for mobile push delivery, Cloudflare Turnstile for abuse prevention, and Google Places for displaying business reviews.',
        'Those providers process information as needed to provide their services to Grand Coast and may handle it under their own terms and privacy practices.',
      ] },
      { title: 'Retention and account deletion', paragraphs: [
        'We keep information while it is needed to provide the service, manage a request or project, protect the system, and maintain business records. We do not have one fixed deletion period for every type of record.',
        'The current self-service account deletion process for client and employee accounts disables sign-in and anonymizes account and profile details. It does not automatically erase related project, message, task, lead, upload, or activity history. Those records may be retained for operational and business recordkeeping. Uploaded files remain until removed through the applicable file-management controls.',
      ] },
      { title: 'Your choices and contacting us', paragraphs: [
        'You can manage mobile push permissions in your device settings and may contact us to ask about access, correction, or deletion of information. Requests may be subject to records we need to retain for ongoing work or business history.',
        'Contact Grand Coast Construction Inc. at (805) 718-1502 or kevin@grandcoastconstruction.com.',
      ] },
      { title: 'Changes to this policy', paragraphs: [
        'We may update this policy as the website, app, or our practices change. The effective date above shows when this version was published.',
      ] },
    ],
  },
  terms: {
    title: 'Terms of Service',
    updated: 'Effective October 30, 2026',
    intro: 'These terms apply when you use the Grand Coast Construction Inc. website, mobile app, estimate-request form, or client and employee account features. By using those services, you agree to these terms.',
    sections: [
      { title: 'Accounts and access', paragraphs: [
        'Some features require an account issued or approved by Grand Coast. Keep your sign-in details private, provide accurate information, and tell us if you believe someone has accessed your account without permission. Your access is limited to the role and projects assigned to you.',
      ] },
      { title: 'Project inquiries and agreements', paragraphs: [
        'Submitting an estimate or project inquiry is a request for follow-up. It is not an acceptance of work, a final estimate, or a construction contract. Project scope, pricing, schedule, and other binding terms are established in a separate agreement signed or otherwise accepted by the parties.',
        'Website project descriptions, photos, progress information, and other content are provided for general information and do not change a separate project agreement.',
      ] },
      { title: 'Messages, records, and notifications', paragraphs: [
        'The website and app may provide project messages, updates, tasks, schedules, time records, documents, and notifications. These features support coordination; they do not replace a signed agreement or other formal approval required for a project decision.',
        'Do not use the services to send emergency requests. Contact the appropriate emergency service when immediate assistance is needed.',
      ] },
      { title: 'Files and other content', paragraphs: [
        'Only upload or send material you are permitted to share. You remain responsible for the content you provide and for obtaining any permissions needed to use it.',
        'You allow Grand Coast to store and use your submitted content to respond to your request and operate the project or account features you use. Content is shared with the recipients and audiences selected in those features. Website publication is public and occurs only when an authorized administrator publishes the material.',
      ] },
      { title: 'Acceptable use', paragraphs: [
        'Do not use the services unlawfully, attempt to access accounts or information without authorization, interfere with service operation or security, or upload malicious or infringing content.',
      ] },
      { title: 'Ownership', paragraphs: [
        'The website, app, branding, and materials supplied by Grand Coast are owned by Grand Coast or its licensors. You may use them only as needed to access the services. You keep any rights you have in content you submit, subject to the limited permission above.',
      ] },
      { title: 'Third-party services and availability', paragraphs: [
        'Some features rely on third-party services, such as hosting, file storage, email, mobile push delivery, and security checks. Their services may be governed by their own terms. We may update, suspend, or discontinue a feature and may restrict access when needed to protect accounts, projects, or the services.',
        'We work to keep the website and app available, but cannot promise uninterrupted or error-free service.',
      ] },
      { title: 'Privacy and account deletion', paragraphs: [
        'Our Privacy Policy explains how information is handled and retained. Deleting an eligible client or employee account disables sign-in and anonymizes account and profile details; related project and operational history may remain.',
      ] },
      { title: 'Changes and contact', paragraphs: [
        'We may update these terms as the services change. Continued use after updated terms are posted means you accept the revised terms.',
        'Questions about these terms can be directed to Grand Coast Construction Inc. at (805) 718-1502 or kevin@grandcoastconstruction.com.',
      ] },
    ],
  },
};
