# Grand Coast Construction mobile wrapper

This is an Expo-managed React Native app. It keeps the existing Django website in a native WebView and adds:

- a white GCC logo splash held for at least one second before fading
- a sign-in-first launch flow using the existing Django login
- a native Grand Coast header with a menu button after sign-in
- a role-aware React Navigation side drawer without the Home page
- employee My Workspace tabs for Field, Manager, and Office users
- the existing Explore and Account drawer for clients
- employee bottom tabs for Dashboard, Workspace, and More
- client bottom tabs for Projects, Contact, and Workspace
- employee account actions for privacy, terms, logout, and account deletion
- Android back-button support inside the WebView
- employee schedule push notifications with an in-app notification inbox
- an optional native media bridge for camera, photo/video selection, and document picking
- upload progress with an app-private retry queue that contains no Django credentials

## Run locally

From PowerShell:

~~~powershell
cd .\mobile
npm start
~~~

The npm start command automatically loads mobile/.env.mobile. Set EXPO_PUBLIC_WEB_APP_URL there to the address reachable from the device:

- iOS Simulator: http://127.0.0.1:8000
- Android Emulator: http://10.0.2.2:8000
- Physical phone: http://YOUR-COMPUTER-LAN-IP:8000

Start Django separately from .\website:

~~~powershell
.\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
~~~

For a physical phone, the computer and phone must be on the same network and Windows Firewall must allow port 8000. Use HTTPS for production instead of a local HTTP URL.

## Push notification setup

Push registration is enabled for authenticated employee sessions. Set these values in `mobile/.env.mobile` before building:

~~~dotenv
EXPO_PUBLIC_EXPO_PROJECT_ID=your-expo-project-id
~~~

The project ID is intentionally a placeholder until the app is connected to your Expo account. Native remote push requires a development or production build with native credentials; Expo Go is not a valid remote-push test target for SDK 57. Configure Apple push credentials and the Firebase Cloud Messaging credentials for Android in the Expo/EAS project, then build with EAS or the native build workflow. The Android channel is `schedule-updates` and uses the device default sound. The Django server also needs `EXPO_PUSH_ENABLED=true` and, when used, `EXPO_ACCESS_TOKEN` in `website/.env`; run `python manage.py dispatch_push_notifications` from a scheduled worker to retry transient failures.

The logo-only native splash is configured in app.json and is applied to development and release builds. Expo Go uses its own project loading screen, so it may briefly show the project name instead of the configured logo.

## Native field media

The primary screens and authorization rules remain Django-rendered. The native layer only handles device actions and sends files to a short-lived, one-time Django upload grant. Session cookies, passwords, CSRF secrets, recovery tokens, and API credentials are not copied into native storage.

Enable the server capability independently during staging with GCC_NATIVE_MEDIA_ENABLED=true and enable the device bridge in mobile/.env.mobile with:

~~~dotenv
EXPO_PUBLIC_NATIVE_MEDIA_ENABLED=true
~~~

When a device is offline or an upload is interrupted, the selected file remains in the app-private queue until the authenticated WebView can prepare a fresh grant. Files are deleted from that queue only after the server confirms the upload. The server still validates project scope, role, file extension, content signature, size, and protected visibility.

## Commands

~~~powershell
npm start
npm run android
npm run ios
npm run doctor
~~~

## Mobile owner Command Center access

The Expo shell can use the existing Grand Coast admin/superuser identity as the Owner
workspace without adding a SwiftUI screen or a second native application. The server
must opt in explicitly:

~~~powershell
$env:GCC_MOBILE_OWNER_ACCESS_ENABLED = "true"
$env:GCC_MOBILE_OWNER_PUSH_ENABLED = "true"
$env:GCC_AI_ENABLED = "false"
~~~

The app loads `/accounts/login/?mobile=1`, preserves the WebView session, and opens the Operations workspace after successful authentication. If administrator PIN or TOTP is enabled, both checks finish before Django creates the session. Admin-catalog navigation remains inside Operations. Normal browser login still rejects superuser credentials, and browser administration stays behind its protected gate.

Owner push registration is accepted only when the server flag is enabled and the request is identified as the Grand Coast mobile WebView. The server remains authoritative if the optional client setting is omitted. Push destinations are limited to authorized Operations paths and fall back to the Operations Notifications page.

Use this sequence for a disposable development test:

1. Start Django with the two mobile owner flags enabled and AI disabled.
2. Start the Expo app with `EXPO_PUBLIC_WEB_APP_URL` pointing to the reachable Django
   development URL.
3. Sign in as the existing temporary superuser and complete PIN/TOTP if prompted.
4. Verify the Command Center, Operations drawer, owner financial visibility, logout, and that admin-catalog navigation returns to Operations.
5. Enable push only after native notification credentials are configured; verify device
   registration, an internal notification destination, and deactivation on logout.
6. Turn the flags off and verify the app returns to the existing compatibility behavior.

Never place passwords, PINs, OTP codes, session values, recovery tokens, Django CSRF secrets, or push credentials in `mobile/.env.mobile`, native storage, screenshots, or committed files. Browser administration remains protected by its separate verification flow.
