import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Animated,
  BackHandler,
  Easing,
  Image,
  KeyboardAvoidingView,
  Linking,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import * as SplashScreen from 'expo-splash-screen';
import * as Constants from 'expo-constants';
import * as Notifications from 'expo-notifications';
import { Ionicons } from '@expo/vector-icons';
import { StatusBar } from 'expo-status-bar';
import { createNativeMediaBridge } from './nativeMediaBridge';
import { LEGAL_DOCUMENTS } from './legalDocuments';
import {
  DrawerContentScrollView,
  DrawerItem,
  createDrawerNavigator,
  useDrawerStatus,
} from '@react-navigation/drawer';
import {
  DefaultTheme,
  NavigationContainer,
} from '@react-navigation/native';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';
import { WebView } from 'react-native-webview';

const colors = {
  navy: '#102B4E',
  blue: '#31518C',
  gold: '#C7943E',
  ink: '#18324D',
  muted: '#718096',
  canvas: '#F5F7FA',
  danger: '#A34843',
  white: '#FFFFFF',
  border: '#E3E8EF',
};

const WEB_APP_URL = (
  process.env.EXPO_PUBLIC_WEB_APP_URL || 'http://127.0.0.1:8000'
).replace(/\/$/, '');

function getTrustedWebAppDetails() {
  try {
    const appUrl = new URL(WEB_APP_URL);
    if (
      !['http:', 'https:'].includes(appUrl.protocol)
      || appUrl.username
      || appUrl.password
      || appUrl.pathname !== '/'
      || appUrl.search
      || appUrl.hash
    ) {
      return null;
    }
    const uploadUrl = new URL('/api/v1/native-upload/', appUrl.origin);
    return { origin: appUrl.origin, uploadPath: uploadUrl.pathname };
  } catch {
    return null;
  }
}

function createNativeMediaBridgeToken() {
  const bytes = new Uint8Array(32);
  if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

const TRUSTED_WEB_APP = getTrustedWebAppDetails();

const AUTH_PATH = '/accounts/login/';
const ACCOUNT_DELETE_PATH = '/accounts/delete/';
const PRIVACY_PATH = '/privacy/';
const TERMS_PATH = '/terms/';
const HOME_PATH = '/';
const ADMIN_WORKSPACE_PATH = '/dashboard/';
const COMMAND_CENTER_PATH = ADMIN_WORKSPACE_PATH;
const EMPLOYEE_WORKSPACE_PATH = '/team/';
const FIELD_TODAY_PATH = '/team/field/';
const EMPLOYEE_PROJECTS_PATH = '/team/projects/';
const EMPLOYEE_PROFILE_PATH = '/team/profile/';
const EMPLOYEE_NOTIFICATIONS_PATH = '/team/notifications/';
const ADMIN_NOTIFICATIONS_PATH = '/dashboard/notifications/';
const EMPLOYEE_PUSH_REGISTER_PATH = '/team/notifications/devices/';
const EMPLOYEE_PUSH_DEACTIVATE_PATH = '/team/notifications/devices/deactivate/';
const CLIENT_WORKSPACE_PATH = '/portal/';
const CLIENT_OVERVIEW_PATH = '/portal/overview/';
const CLIENT_PROFILE_PATH = '/portal/profile/';
const CLIENT_NOTIFICATIONS_PATH = '/portal/notifications/';
const CLIENT_NAVIGATION_COUNTS_PATH = '/portal/mobile-navigation-counts/';
const CLIENT_PUSH_REGISTER_PATH = '/portal/notifications/devices/';
const CLIENT_PUSH_DEACTIVATE_PATH = '/portal/notifications/devices/deactivate/';
const CLIENT_NAVIGATION_COUNTS_REFRESH_SCRIPT = [
  '(function () {',
  '  try {',
  '    if (!window.ReactNativeWebView || typeof window.fetch !== \'function\') { return true; }',
  '    if (window.__gccClientNavigationCountsController) {',
  '      window.__gccClientNavigationCountsController.abort();',
  '    }',
  '    var controller = new AbortController();',
  '    window.__gccClientNavigationCountsController = controller;',
  `    fetch(${JSON.stringify(CLIENT_NAVIGATION_COUNTS_PATH)}, { credentials: 'same-origin', signal: controller.signal })`,
  '      .then(function (response) { return response.ok ? response.json() : null; })',
  '      .then(function (counts) {',
  '        if (!counts || !window.ReactNativeWebView) { return; }',
  '        window.ReactNativeWebView.postMessage(JSON.stringify({',
  "          type: 'client-navigation-counts',",
  "          workspaceKind: 'client',",
  '          counts: counts,',
  '        }));',
  '      })',
  '      .catch(function () {});',
  '  } catch (error) {',
  '    // Keep the last known counts if a refresh is unavailable.',
  '  }',
  '  true;',
  '})();',
].join('\n');
const PRIVATE_ROUTE_PREFIXES = ['/dashboard', '/team', '/portal'];
const LAUNCH_SPLASH_HOLD_MS = 1000;
const LAUNCH_SPLASH_FADE_MS = 600;
const SPLASH_LOGO = require('./assets/gcc-logo.png');
const MOBILE_WEBVIEW_USER_AGENT = 'GrandCoastMobile/1.0';
const MOBILE_WEBVIEW_REQUEST_HEADERS = { 'X-Grand-Coast-Mobile': '1' };
const NATIVE_CONTACT_PATH = '/contact/';
const NATIVE_CONTACT_SUCCESS_MESSAGE = 'Thanks for sharing your project. We will be in touch soon.';
const MOBILE_CONTACT_REQUEST_HEADERS = MOBILE_WEBVIEW_REQUEST_HEADERS;
const EMPTY_NATIVE_CONTACT_FORM = {
  first_name: '',
  last_name: '',
  email: '',
  phone: '',
  project_type: '',
  location: '',
  message: '',
};

SplashScreen.setOptions({ duration: 0, fade: false });
SplashScreen.preventAutoHideAsync().catch(() => {});
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

const Drawer = createDrawerNavigator();
const SessionContext = createContext(null);
const SharedWebViewContext = createContext({
  beginAuthTransition: () => {},
  cancelAuthTransition: () => {},
  consumeLogoutRequest: () => false,
  finishAuthTransition: () => {},
  hideLaunchSplash: () => Promise.resolve(),
  launchSplashVisible: true,
  navigate: () => {},
  requestLogout: () => {},
  setCurrentPath: () => {},
  webViewRef: null,
});
const NativeShellContext = createContext({
  activeTab: null,
  activeWebPath: null,
  setActiveTab: () => {},
  setActiveWebPath: () => {},
});

const transparentNavigationTheme = {
  ...DefaultTheme,
  colors: {
    ...DefaultTheme.colors,
    background: 'transparent',
  },
};

const clientDrawerPages = [
  { label: 'Project overview', icon: 'home-outline', path: '/portal/overview/', tab: 'Workspace' },
  { label: 'Updates', icon: 'megaphone-outline', path: '/portal/updates/', tab: 'Workspace' },
  { label: 'Photos & videos', icon: 'images-outline', path: '/portal/photos/', tab: 'Workspace' },
  {
    label: 'Messages',
    icon: 'chatbubbles-outline',
    path: '/portal/messages/',
    tab: 'Workspace',
    countKey: 'messages',
    countAccessibilityLabel: (count) => `${count} unread message${count === 1 ? '' : 's'}`,
  },
  {
    label: 'Estimate & files',
    icon: 'receipt-outline',
    path: '/portal/estimate-files/',
    tab: 'Workspace',
    countKey: 'estimates',
    countAccessibilityLabel: (count) => `${count} estimate${count === 1 ? '' : 's'} to review`,
  },
  {
    label: 'Notifications',
    icon: 'notifications-outline',
    path: '/portal/notifications/',
    tab: 'Workspace',
    countKey: 'notifications',
    countAccessibilityLabel: (count) => `${count} unread notification${count === 1 ? '' : 's'}`,
  },
  { label: 'Profile', icon: 'person-circle-outline', path: CLIENT_PROFILE_PATH, tab: 'Overview' },
];

const employeeDrawerPages = [
  { label: 'Overview', icon: 'home-outline', path: EMPLOYEE_WORKSPACE_PATH, tab: 'Dashboard', countKey: 'overview', group: null },
  { label: 'Projects', icon: 'folder-outline', path: EMPLOYEE_PROJECTS_PATH, tab: 'Workspace', countKey: 'projects', group: 'Client & Job Operations' },
  { label: 'Tasks', icon: 'checkmark-outline', path: '/team/tasks/', tab: 'Workspace', countKey: 'tasks', group: 'Client & Job Operations' },
  { label: 'Calendar', icon: 'calendar-outline', path: '/team/calendar/', tab: 'Workspace', countKey: 'calendar', group: 'Client & Job Operations' },
  { label: 'Time', icon: 'time-outline', path: '/team/time/', tab: 'Workspace', countKey: 'time', group: 'Client & Job Operations' },
  { label: 'Media', icon: 'images-outline', path: '/team/media/', tab: 'Workspace', countKey: 'media', group: 'Client & Job Operations' },
  { label: 'Notifications', icon: 'notifications-outline', path: EMPLOYEE_NOTIFICATIONS_PATH, tab: 'Workspace', countKey: 'notifications', group: 'Miscellaneous' },
  { label: 'Profile', icon: 'person-circle-outline', path: '/team/profile/', tab: 'Workspace', countKey: 'profile', group: 'Miscellaneous' },
];

const adminDrawerPages = [
  { label: 'Overview', icon: 'home-outline', path: COMMAND_CENTER_PATH, tab: 'Dashboard', countKey: 'overview', group: null },
  { label: 'Leads', icon: 'funnel-outline', path: '/dashboard/leads/', tab: 'Workspace', countKey: 'leads', group: 'Client & Job Operations' },
  { label: 'Clients', icon: 'people-outline', path: '/dashboard/clients/', tab: 'Workspace', countKey: 'clients', group: 'Client & Job Operations' },
  { label: 'Estimates', icon: 'receipt-outline', path: '/dashboard/estimates/', tab: 'Workspace', countKey: 'estimates', group: 'Client & Job Operations' },
  { label: 'Projects', icon: 'folder-outline', path: '/dashboard/projects/', tab: 'Workspace', countKey: 'projects', group: 'Client & Job Operations' },
  { label: 'Tasks', icon: 'checkmark-outline', path: '/dashboard/tasks/', tab: 'Workspace', countKey: 'tasks', group: 'Client & Job Operations' },
  { label: 'Calendar', icon: 'calendar-outline', path: '/dashboard/calendar/', tab: 'Workspace', countKey: 'calendar', group: 'Client & Job Operations' },
  { label: 'Time', icon: 'time-outline', path: '/dashboard/time/', tab: 'Workspace', countKey: 'time', group: 'Client & Job Operations' },
  { label: 'Documents', icon: 'document-text-outline', path: '/dashboard/documents/', tab: 'Workspace', countKey: 'documents', group: 'Client & Job Operations' },
  { label: 'Media', icon: 'images-outline', path: '/dashboard/media/', tab: 'Workspace', countKey: 'media', group: 'Client & Job Operations' },
  { label: 'Team', icon: 'people-circle-outline', path: '/dashboard/team/', tab: 'Workspace', countKey: 'team', group: 'Miscellaneous' },
  { label: 'Messages', icon: 'chatbubbles-outline', path: '/dashboard/clients/?messages=1', tab: 'Workspace', countKey: 'messages', group: 'Miscellaneous' },
  { label: 'Notifications', icon: 'notifications-outline', path: '/dashboard/notifications/', tab: 'Workspace', countKey: 'notifications', group: 'Miscellaneous' },
  { label: 'Content', icon: 'sparkles-outline', path: '/dashboard/content/', tab: 'Workspace', countKey: 'content', group: 'Miscellaneous' },
];

const clientTabIcons = {
  Overview: ['home-outline', 'home'],
  Notifications: ['notifications-outline', 'notifications'],
  More: ['menu-outline', 'menu-outline'],
};

const employeeTabIcons = {
  Dashboard: ['home-outline', 'home'],
  Workspace: ['briefcase-outline', 'briefcase'],
  More: ['menu-outline', 'menu-outline'],
};

const employeeBottomTabs = [
  { label: 'Dashboard', name: 'Dashboard', path: EMPLOYEE_WORKSPACE_PATH },
  { label: 'Workspace', name: 'Workspace', path: EMPLOYEE_PROJECTS_PATH },
  { label: 'More', name: 'More', path: null },
];

const clientBottomTabs = [
  { label: 'Overview', name: 'Overview', path: CLIENT_OVERVIEW_PATH },
  { label: 'Notifications', name: 'Notifications', path: CLIENT_NOTIFICATIONS_PATH },
  { label: 'More', name: 'More', path: null },
];

const employeeMorePages = [
  { label: 'Privacy Policy', icon: 'shield-checkmark-outline', path: PRIVACY_PATH },
  { label: 'Terms of Service', icon: 'document-text-outline', path: TERMS_PATH },
  { label: 'Log out', icon: 'log-out-outline', path: EMPLOYEE_WORKSPACE_PATH, action: 'logout' },
  { label: 'Delete account', icon: 'trash-outline', path: ACCOUNT_DELETE_PATH, danger: true },
];

const adminMorePages = [
  { label: 'Privacy Policy', icon: 'shield-checkmark-outline', path: PRIVACY_PATH },
  { label: 'Terms of Service', icon: 'document-text-outline', path: TERMS_PATH },
  { label: 'Log out', icon: 'log-out-outline', path: ADMIN_WORKSPACE_PATH, action: 'logout' },
];

const clientMorePages = [
  { label: 'Privacy Policy', icon: 'shield-checkmark-outline', path: PRIVACY_PATH },
  { label: 'Terms of Service', icon: 'document-text-outline', path: TERMS_PATH },
  { label: 'Log out', icon: 'log-out-outline', path: CLIENT_OVERVIEW_PATH, action: 'logout' },
  { label: 'Delete account', icon: 'trash-outline', path: ACCOUNT_DELETE_PATH, danger: true },
];

const employeeMoreContactPage = {
  label: 'Contact Grand Coast',
  icon: 'chatbubble-ellipses-outline',
};

const employeeDrawerLogout = employeeMorePages[2];
const adminDrawerLogout = adminMorePages[2];

const MOBILE_CHROME_SCRIPT = [
  '(function () {',
  '  try {',
  "    document.documentElement.classList.add('mobile-app-webview');",
  "    var styleId = 'grand-coast-mobile-webview-chrome';",
  '    var style = document.getElementById(styleId);',
  '',
  '    if (!style) {',
  "      style = document.createElement('style');",
  '      style.id = styleId;',
  '      (document.head || document.documentElement).appendChild(style);',
  '    }',
  '',
  "    style.textContent = '.site-header, .site-footer, .portal-header, .portal-footer, .admin-topbar, .admin-sidebar, .staging-bar, .portal-staging-bar, .admin-workspace-notice { display: none !important; } .admin-main { margin-left: 0 !important; } .auth-site .auth-card > .text-link { display: none !important; }';",
  '  } catch (error) {',
  '    // The page can still render if a WebView engine rejects the style injection.',
  '  }',
  '',
  '  try {',
  "    var navData = document.querySelector('[data-operations-notifications]');",
  '    if (navData && window.ReactNativeWebView) {',
  '      window.ReactNativeWebView.postMessage(JSON.stringify({',
  "        type: 'operations-navigation-counts',",
  '        workspaceKind: navData.getAttribute(\'data-workspace-kind\'),',
  '        counts: {',
  "          overview: Number(navData.getAttribute('data-count-overview')) || 0,",
  "          clients: Number(navData.getAttribute('data-count-clients')) || 0,",
  "          tasks: Number(navData.getAttribute('data-count-tasks')) || 0,",
  "          calendar: Number(navData.getAttribute('data-count-calendar')) || 0,",
  "          time: Number(navData.getAttribute('data-count-time')) || 0,",
  "          documents: Number(navData.getAttribute('data-count-documents')) || 0,",
  "          leads: Number(navData.getAttribute('data-count-leads')) || 0,",
  "          estimates: Number(navData.getAttribute('data-count-estimates')) || 0,",
  "          projects: Number(navData.getAttribute('data-count-projects')) || 0,",
  "          media: Number(navData.getAttribute('data-count-media')) || 0,",
  "          content: Number(navData.getAttribute('data-count-content')) || 0,",
  "          team: Number(navData.getAttribute('data-count-team')) || 0,",
  "          messages: Number(navData.getAttribute('data-count-messages')) || 0,",
  "          profile: Number(navData.getAttribute('data-count-profile')) || 0,",
  "          notifications: Number(navData.getAttribute('data-count-notifications')) || 0,",
  '        },',
  '      }));',
  '    }',
  '  } catch (error) {',
  '    // Count metadata is optional on public pages and must never block navigation.',
  '  }',
  '',
  '  try {',
  "    var clientNavData = document.querySelector('[data-client-navigation-counts]');",
  '    if (clientNavData && window.ReactNativeWebView) {',
  '      window.ReactNativeWebView.postMessage(JSON.stringify({',
  "        type: 'client-navigation-counts',",
  "        workspaceKind: 'client',",
  '        counts: {',
  "          estimates: Number(clientNavData.getAttribute('data-count-estimates')) || 0,",
  "          messages: Number(clientNavData.getAttribute('data-count-messages')) || 0,",
  "          notifications: Number(clientNavData.getAttribute('data-count-notifications')) || 0,",
  '        },',
  '      }));',
  '    }',
  '  } catch (error) {',
  '    // Client count metadata is optional and must never block portal navigation.',
  '  }',
  '',
  '  try {',
  "    if (!window.__grandCoastPullRefreshBound) {",
  '      var pullStartY = null;',
  '      var getScrollTop = function () {',
  '        var scrollingElement = document.scrollingElement || document.documentElement;',
  '        return window.pageYOffset || scrollingElement.scrollTop || document.body.scrollTop || 0;',
  '      };',
  '      document.addEventListener(\'touchstart\', function (event) {',
  '        if (event.touches.length !== 1 || getScrollTop() > 0) {',
  '          pullStartY = null;',
  '          return;',
  '        }',
  '        var target = event.target;',
  '        var interactiveTarget = target && typeof target.closest === \'function\' ? target.closest(\'input, textarea, select, button, a, [contenteditable=true]\') : null;',
  '        pullStartY = interactiveTarget ? null : event.touches[0].clientY;',
  '      }, { passive: true });',
  '      document.addEventListener(\'touchmove\', function (event) {',
  '        if (pullStartY === null || event.touches.length !== 1 || !window.ReactNativeWebView) {',
  '          return;',
  '        }',
  '        var pullDistance = Math.max(0, Math.min((event.touches[0].clientY - pullStartY) * 0.55, 96));',
  "        window.ReactNativeWebView.postMessage(JSON.stringify({ type: 'mobile-pull-to-refresh-progress', distance: pullDistance }));",
  '      }, { passive: true });',
  '      document.addEventListener(\'touchend\', function (event) {',
  '        if (pullStartY !== null && event.changedTouches.length === 1 && window.ReactNativeWebView) {',
  '          var pullDistance = event.changedTouches[0].clientY - pullStartY;',
  "          window.ReactNativeWebView.postMessage(JSON.stringify({ type: 'mobile-pull-to-refresh-release', distance: pullDistance }));",
  '        }',
  '        pullStartY = null;',
  '      }, { passive: true });',
  '      document.addEventListener(\'touchcancel\', function () {',
  '        if (pullStartY !== null && window.ReactNativeWebView) {',
  "          window.ReactNativeWebView.postMessage(JSON.stringify({ type: 'mobile-pull-to-refresh-cancel' }));",
  '        }',
  '        pullStartY = null;',
  '      }, { passive: true });',
  '      window.__grandCoastPullRefreshBound = true;',
  '    }',
  '  } catch (error) {',
  '    // Pull-to-refresh is an enhancement and must never block page interaction.',
  '  }',
  '',
  '  true;',
  '})();',
].join('\n');

const MOBILE_LOGOUT_SCRIPT = [
  '(function () {',
  '  try {',
  '    var form = document.querySelector(\'form[action$="/accounts/logout/"]\');',
  '    if (!form) {',
  '      return true;',
  '    }',
  '    if (typeof form.requestSubmit === \'function\') {',
  '      form.requestSubmit();',
  '    } else {',
  '      form.submit();',
  '    }',
  '  } catch (error) {',
  '    // The existing Django logout form remains the source of truth.',
  '  }',
  '  true;',
  '})();',
].join('\n');

const MOBILE_LOGOUT_WITH_DEVICE_SCRIPT = `
(function () {
  try {
    var form = document.querySelector('form[action$="/accounts/logout/"]');
    if (!form) {
      return true;
    }
    var submitLogout = function () {
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit();
      } else {
        form.submit();
      }
    };
    var token = window.__grandCoastPushToken || '';
    if (!token || typeof fetch !== 'function') {
      submitLogout();
      return true;
    }
    var deviceDeactivatePath = window.__grandCoastPushDeviceDeactivatePath || '/team/notifications/devices/deactivate/';
    var csrfMatch = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    var csrfToken = csrfMatch ? decodeURIComponent(csrfMatch[1]) : '';
    fetch(deviceDeactivatePath, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken},
      body: 'token=' + encodeURIComponent(token),
    }).catch(function () {}).then(function () {
      window.__grandCoastPushToken = '';
      submitLogout();
    }, function () {
      window.__grandCoastPushToken = '';
      submitLogout();
    });
  } catch (error) {
    // The existing Django logout form remains the source of truth.
  }
  return true;
})();
`;

function useSession() {
  return useContext(SessionContext);
}

function pageUrl(path) {
  const normalizedPath = path && path.startsWith('/') ? path : '/' + (path || '');
  return WEB_APP_URL + normalizedPath;
}

function encodeFormBody(values) {
  return Object.entries(values)
    .map(([key, value]) => encodeURIComponent(key) + '=' + encodeURIComponent(String(value ?? '')))
    .join('&');
}

function extractCsrfToken(html) {
  const match = html.match(/name=["']csrfmiddlewaretoken["'][^>]*value=["']([^"']+)["']/i);
  return match ? match[1] : '';
}

async function submitNativeContactForm(values) {
  const contactUrl = pageUrl(NATIVE_CONTACT_PATH);
  const formResponse = await fetch(contactUrl, {
    credentials: 'include',
    headers: {
      Accept: 'text/html',
      ...MOBILE_CONTACT_REQUEST_HEADERS,
    },
  });

  if (!formResponse.ok) {
    throw new Error('The contact form could not be loaded.');
  }

  const csrfToken = extractCsrfToken(await formResponse.text());
  if (!csrfToken) {
    throw new Error('The contact form security token could not be loaded.');
  }

  const response = await fetch(contactUrl, {
    body: encodeFormBody({
      csrfmiddlewaretoken: csrfToken,
      ...values,
    }),
    credentials: 'include',
    headers: {
      Accept: 'text/html',
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-CSRFToken': csrfToken,
      Referer: contactUrl,
      ...MOBILE_CONTACT_REQUEST_HEADERS,
    },
    method: 'POST',
  });
  const responseHtml = await response.text();

  if (!response.ok || !responseHtml.includes(NATIVE_CONTACT_SUCCESS_MESSAGE)) {
    throw new Error('The contact message could not be sent.');
  }
}

function validateNativeContactForm(values) {
  const requiredFields = ['first_name', 'last_name', 'email', 'project_type', 'location', 'message'];
  if (requiredFields.some((field) => !values[field].trim())) {
    return 'Please complete all required fields.';
  }

  if (!/^\S+@\S+\.\S+$/.test(values.email.trim())) {
    return 'Enter a valid email address.';
  }

  return '';
}

function relativePathFromUrl(urlOrPath) {
  const value = String(urlOrPath || '');
  const withoutOrigin = value.replace(/^https?:\/\/[^/]+/i, '');
  if (!withoutOrigin) {
    return HOME_PATH;
  }

  return withoutOrigin.startsWith('/') ? withoutOrigin : '/' + withoutOrigin;
}

function pathnameFromUrl(urlOrPath) {
  return relativePathFromUrl(urlOrPath).split(/[?#]/)[0] || HOME_PATH;
}

function isLoginPath(pathname) {
  return pathname === '/accounts/login' || pathname.startsWith(AUTH_PATH);
}

function isPrivatePath(pathname) {
  return PRIVATE_ROUTE_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(prefix + '/'),
  );
}

function workspaceKindFromPath(pathname) {
  if (pathname === '/dashboard' || pathname.startsWith('/dashboard/')) {
    return 'admin';
  }

  if (pathname === '/team' || pathname.startsWith('/team/')) {
    return 'employee';
  }

  if (pathname === '/portal' || pathname.startsWith('/portal/')) {
    return 'client';
  }

  return null;
}

function isInternalUrl(url) {
  return (
    url === WEB_APP_URL ||
    url.startsWith(WEB_APP_URL + '/') ||
    url.startsWith(WEB_APP_URL + '?') ||
    url.startsWith(WEB_APP_URL + '#')
  );
}

function isAdminPath(pathname) {
  return pathname === '/gccad' || pathname.startsWith('/gccad/');
}

function openExternalUrl(url) {
  Linking.openURL(url).catch(() => {});
  return false;
}

function AppHeader({ navigation }) {
  const insets = useSafeAreaInsets();
  const { navigate } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);
  const { workspaceKind, workspacePath } = useSession();

  const openProfile = useCallback(() => {
    const isEmployee = workspaceKind === 'employee';
    const isAdmin = workspaceKind === 'admin';
    const isClient = workspaceKind === 'client';
    const profilePath = isEmployee
      ? EMPLOYEE_PROFILE_PATH
      : isAdmin ? ADMIN_WORKSPACE_PATH : isClient ? CLIENT_PROFILE_PATH : workspacePath || CLIENT_WORKSPACE_PATH;
    setActiveTab(isAdmin ? 'Dashboard' : isClient ? 'Overview' : 'Workspace');
    setActiveWebPath(profilePath);
    navigate(profilePath);
  }, [navigate, setActiveTab, setActiveWebPath, workspaceKind, workspacePath]);

  return (
    <View style={[styles.header, { paddingTop: insets.top }]}>
      <View style={styles.headerRow}>
        <Pressable
          accessibilityLabel="Open side navigation"
          hitSlop={10}
          onPress={() => navigation.openDrawer()}
          style={styles.headerButton}
        >
          <Ionicons color={colors.white} name="menu-outline" size={27} />
        </Pressable>

        <View style={styles.headerBrand}>
          <Text style={styles.headerTitle}>Grand Coast</Text>
          <Text style={styles.headerSubtitle}>Construction Inc.</Text>
        </View>

        <Pressable
          accessibilityLabel={workspaceKind === 'employee' || workspaceKind === 'client' ? 'Open profile' : workspaceKind === 'admin' ? 'Open operations' : 'Open workspace'}
          hitSlop={10}
          onPress={openProfile}
          style={styles.headerButton}
        >
          <Ionicons color={colors.white} name="person-circle-outline" size={28} />
        </Pressable>
      </View>
    </View>
  );
}

function NativeNotificationBadge({ count, page, lightSurface = false }) {
  const numericCount = Number.isFinite(Number(count)) ? Math.max(0, Math.floor(Number(count))) : 0;

  if (numericCount === 0) {
    return null;
  }

  const displayedCount = numericCount > 99 ? '99+' : String(numericCount);
  const accessibilityLabel = typeof page?.countAccessibilityLabel === 'function'
    ? page.countAccessibilityLabel(numericCount)
    : `${page?.label || 'Navigation item'}, ${numericCount}`;

  return (
    <View
      accessible
      accessibilityLabel={accessibilityLabel}
      accessibilityRole="text"
      style={[styles.nativeNavCount, lightSurface && styles.clientNativeNavCount]}
    >
      <Text style={[styles.nativeNavCountText, lightSurface && styles.clientNativeNavCountText]}>
        {displayedCount}
      </Text>
    </View>
  );
}

function NativeDrawerItem({ navigation, notificationCount, page, isEmployee }) {
  const { navigate } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);

  return (
    <DrawerItem
      activeBackgroundColor={isEmployee ? 'rgba(255, 255, 255, 0.12)' : undefined}
      activeTintColor={isEmployee ? colors.white : colors.blue}
      icon={({ color, size }) => (
        <Ionicons color={color} name={page.icon} size={size} />
      )}
      inactiveTintColor={isEmployee ? '#F5F7FA' : undefined}
      label={() => (
        <View style={styles.drawerItemLabelRow}>
          <Text style={[isEmployee ? styles.employeeDrawerItemLabel : styles.drawerItemLabel, styles.drawerItemLabelText]}>
            {page.label}
          </Text>
          {page.countKey ? (
            <NativeNotificationBadge count={notificationCount} lightSurface={!isEmployee} page={page} />
          ) : null}
        </View>
      )}
      onPress={() => {
        setActiveTab(page.tab);
        setActiveWebPath(page.path);
        navigate(page.path);
        navigation.closeDrawer();
      }}
      pressColor={isEmployee ? 'rgba(255, 255, 255, 0.08)' : '#EDF2F8'}
      style={isEmployee ? styles.employeeDrawerItem : styles.drawerItem}
    />
  );
}

function NativeDrawerAction({ action, isAdmin, navigation }) {
  const { requestLogout } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);

  return (
    <DrawerItem
      activeBackgroundColor="rgba(255, 255, 255, 0.12)"
      activeTintColor={colors.white}
      icon={({ color, size }) => <Ionicons color={color} name={action.icon} size={size} />}
      inactiveTintColor="#F5F7FA"
      label={action.label}
      labelStyle={styles.employeeDrawerItemLabel}
      onPress={() => {
        requestLogout(isAdmin ? ADMIN_WORKSPACE_PATH : EMPLOYEE_WORKSPACE_PATH);
        setActiveTab(isAdmin ? 'Dashboard' : 'Workspace');
        setActiveWebPath(isAdmin ? ADMIN_WORKSPACE_PATH : EMPLOYEE_WORKSPACE_PATH);
        navigation.closeDrawer();
      }}
      pressColor="rgba(255, 255, 255, 0.08)"
      style={styles.employeeDrawerItem}
    />
  );
}

function AppDrawerContent({ navigation, ...props }) {
  const { isAuthenticated, navigationCounts, workspaceKind, workspacePath } = useSession();
  const { webViewRef } = useContext(SharedWebViewContext);
  const drawerStatus = useDrawerStatus();
  const isAdmin = workspaceKind === 'admin';
  const isEmployee = workspaceKind === 'employee';
  const isClient = workspaceKind === 'client';
  const isOperations = isAdmin || isEmployee;
  const pages = isAdmin ? adminDrawerPages : isEmployee ? employeeDrawerPages : clientDrawerPages;

  useEffect(() => {
    if (isAuthenticated && isClient && drawerStatus === 'open') {
      webViewRef?.current?.injectJavaScript(CLIENT_NAVIGATION_COUNTS_REFRESH_SCRIPT);
    }
  }, [drawerStatus, isAuthenticated, isClient, webViewRef]);

  const accountPages = useMemo(
    () => [{
      label: isClient ? 'Portal home' : 'Workspace',
      icon: 'briefcase-outline',
      path: isClient ? CLIENT_OVERVIEW_PATH : workspacePath || CLIENT_WORKSPACE_PATH,
      tab: isClient ? 'Overview' : 'Workspace',
    }],
    [isClient, workspacePath],
  );

  if (!isAuthenticated) {
    return null;
  }

  return (
    <DrawerContentScrollView
      {...props}
      contentContainerStyle={isOperations ? styles.employeeDrawerContent : styles.drawerContent}
      showsVerticalScrollIndicator={false}
    >
      {isOperations ? (
        <>
          <View style={styles.employeeDrawerTopRule} />
          {pages.map((page, index) => (
            <View key={`${page.group || 'overview'}:${page.label}`}>
              {page.group && (index === 0 || page.group !== pages[index - 1].group) ? (
                <Text style={styles.employeeDrawerWorkflowGroup}>{page.group}</Text>
              ) : null}
              <NativeDrawerItem
                isEmployee
                navigation={navigation}
                notificationCount={navigationCounts?.[page.countKey] ?? 0}
                page={page}
              />
            </View>
          ))}
          <View style={styles.employeeDrawerBottomRule} />
          <NativeDrawerAction
            action={isAdmin ? adminDrawerLogout : employeeDrawerLogout}
            isAdmin={isAdmin}
            navigation={navigation}
          />
        </>
      ) : (
        <>
          <View style={styles.drawerBrand}>
            <View style={styles.drawerMark}>
              <Text style={styles.drawerMarkText}>GC</Text>
            </View>
            <View>
              <Text style={styles.drawerBrandTitle}>Grand Coast</Text>
              <Text style={styles.drawerBrandSubtitle}>Construction Inc.</Text>
            </View>
          </View>

          <Text style={styles.drawerSectionLabel}>{isClient ? 'Client portal' : 'Explore'}</Text>
          {pages.map((page) => (
            <NativeDrawerItem
              key={page.path}
              navigation={navigation}
              notificationCount={navigationCounts?.[page.countKey] ?? 0}
              page={page}
            />
          ))}

          <Text style={[styles.drawerSectionLabel, styles.drawerSectionLabelSpaced]}>Account</Text>
          {accountPages.map((page) => (
            <NativeDrawerItem key={page.path} navigation={navigation} page={page} />
          ))}

          <View style={styles.drawerFooter}>
            <Text style={styles.drawerFooterText}>Thoughtful construction.</Text>
            <Text style={styles.drawerFooterText}>Clear communication.</Text>
          </View>
        </>
      )}
    </DrawerContentScrollView>
  );
}

function buildMobileWebViewScript(bridgeToken) {
  const privateRouteTokenScript = [
    '(function () {',
    '  var path = window.location && window.location.pathname || "/";',
    '  var isPrivate = ["/dashboard", "/team", "/portal"].some(function (prefix) {',
    '    return path === prefix || path.indexOf(prefix + "/") === 0;',
    '  });',
    '  if (isPrivate) {',
    `    window.__GCC_NATIVE_MEDIA_BRIDGE_TOKEN = ${JSON.stringify(bridgeToken)};`,
    '  } else {',
    '    try { delete window.__GCC_NATIVE_MEDIA_BRIDGE_TOKEN; } catch (error) { window.__GCC_NATIVE_MEDIA_BRIDGE_TOKEN = null; }',
    '  }',
    '})();',
  ].join('\n');
  return `${privateRouteTokenScript}\n${MOBILE_CHROME_SCRIPT}`;
}

function useWebViewChrome(webViewRef, script) {
  return useCallback(() => {
    webViewRef.current?.injectJavaScript(script);
  }, [script, webViewRef]);
}

function PushNotificationBridge() {
  const { isAuthenticated, workspaceKind, workspacePath } = useSession();
  const { navigate, webViewRef } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);
  const pushTokenRef = useRef(null);
  const registrationAttemptedRef = useRef(false);
  const pendingNotificationDestinationRef = useRef(null);
  const isPushWorkspace = workspaceKind === 'employee' || workspaceKind === 'admin' || workspaceKind === 'client';
  const isClientWorkspace = workspaceKind === 'client';
  const ownerPushClientEnabled = process.env.EXPO_PUBLIC_MOBILE_OWNER_PUSH_ENABLED !== 'false';

  const safeNotificationDestination = useCallback((requestedDestination) => {
    const fallback = isClientWorkspace
      ? CLIENT_NOTIFICATIONS_PATH
      : workspaceKind === 'admin' ? ADMIN_NOTIFICATIONS_PATH : EMPLOYEE_NOTIFICATIONS_PATH;
    if (
      typeof requestedDestination !== 'string'
      || !requestedDestination.startsWith('/')
    ) {
      return fallback;
    }
    if (isClientWorkspace) {
      const requestedPath = pathnameFromUrl(requestedDestination);
      if (
        requestedPath === '/portal'
        || requestedPath === '/portal/'
        || !requestedPath.startsWith('/portal/')
      ) {
        return fallback;
      }
      return requestedDestination;
    }
    if (isAdminPath(requestedDestination)) {
      return fallback;
    }
    return requestedDestination;
  }, [isClientWorkspace, workspaceKind]);

  const registerTokenInWebView = useCallback((token) => {
    if (!token || !webViewRef.current) {
      return;
    }
    const platform = Platform.OS;
    const registerPath = isClientWorkspace ? CLIENT_PUSH_REGISTER_PATH : EMPLOYEE_PUSH_REGISTER_PATH;
    const deactivatePath = isClientWorkspace ? CLIENT_PUSH_DEACTIVATE_PATH : EMPLOYEE_PUSH_DEACTIVATE_PATH;
    const script = `
(function () {
  var token = ${JSON.stringify(token)};
  var platform = ${JSON.stringify(platform)};
  var registerPath = ${JSON.stringify(registerPath)};
  var deactivatePath = ${JSON.stringify(deactivatePath)};
  window.__grandCoastPushToken = token;
  window.__grandCoastPushDeviceDeactivatePath = deactivatePath;
  window.__grandCoastRegisterPushToken = function (value, devicePlatform) {
    var csrfMatch = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    var csrfToken = csrfMatch ? decodeURIComponent(csrfMatch[1]) : '';
    return fetch(registerPath, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken},
      body: 'token=' + encodeURIComponent(value) + '&platform=' + encodeURIComponent(devicePlatform || ''),
    }).catch(function () {});
  };
  window.__grandCoastRegisterPushToken(token, platform);
})();
true;`;
    webViewRef.current.injectJavaScript(script);
  }, [isClientWorkspace, webViewRef]);

  const openNotificationDestination = useCallback((response) => {
    const data = response?.notification?.request?.content?.data || {};
    const requestedDestination = typeof data.url === 'string' && data.url.startsWith('/')
      ? data.url
      : '';
    if (!isAuthenticated || !isPushWorkspace || !webViewRef.current) {
      pendingNotificationDestinationRef.current = requestedDestination;
      return false;
    }
    const destination = safeNotificationDestination(requestedDestination);
    setActiveTab(isClientWorkspace ? 'Notifications' : 'Workspace');
    setActiveWebPath(destination);
    return navigate(destination);
  }, [isAuthenticated, isClientWorkspace, isPushWorkspace, navigate, safeNotificationDestination, setActiveTab, setActiveWebPath, webViewRef]);

  useEffect(() => {
    const responseSubscription = Notifications.addNotificationResponseReceivedListener(
      openNotificationDestination,
    );
    Notifications.getLastNotificationResponseAsync()
      .then((response) => {
        if (response) {
          openNotificationDestination(response);
        }
      })
      .catch(() => {});
    return () => responseSubscription.remove();
  }, [openNotificationDestination]);

  useEffect(() => {
    const destination = pendingNotificationDestinationRef.current;
    if (
      destination === null
      || !isAuthenticated
      || !isPushWorkspace
      || !webViewRef.current
    ) {
      return;
    }
    pendingNotificationDestinationRef.current = null;
    const safeDestination = safeNotificationDestination(destination);
    setActiveTab(isClientWorkspace ? 'Notifications' : 'Workspace');
    setActiveWebPath(safeDestination);
    navigate(safeDestination);
  }, [isAuthenticated, isClientWorkspace, isPushWorkspace, navigate, safeNotificationDestination, setActiveTab, setActiveWebPath, webViewRef, workspacePath]);

  useEffect(() => {
    if (!isAuthenticated || !isPushWorkspace || !webViewRef.current) {
      return;
    }
    const deactivatePath = isClientWorkspace ? CLIENT_PUSH_DEACTIVATE_PATH : EMPLOYEE_PUSH_DEACTIVATE_PATH;
    webViewRef.current.injectJavaScript(
      `window.__grandCoastPushDeviceDeactivatePath=${JSON.stringify(deactivatePath)}; true;`,
    );
  }, [isAuthenticated, isClientWorkspace, isPushWorkspace, webViewRef, workspacePath]);

  useEffect(() => {
    if (Platform.OS === 'android') {
      Notifications.setNotificationChannelAsync('schedule-updates', {
        name: 'Schedule updates',
        importance: Notifications.AndroidImportance.DEFAULT,
        sound: 'default',
        vibrationPattern: [0, 250, 250, 250],
        lockscreenVisibility: Notifications.AndroidNotificationVisibility.PUBLIC,
      }).catch(() => {});
    }
  }, []);

  useEffect(() => {
    const canRegisterPush = isAuthenticated && (
      workspaceKind === 'employee'
      || workspaceKind === 'client'
      || (workspaceKind === 'admin' && ownerPushClientEnabled)
    );
    if (!canRegisterPush) {
      registrationAttemptedRef.current = false;
      pushTokenRef.current = null;
      return undefined;
    }
    if (registrationAttemptedRef.current) {
      return undefined;
    }
    registrationAttemptedRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        let permission = await Notifications.getPermissionsAsync();
        if (permission.status !== 'granted') {
          permission = await Notifications.requestPermissionsAsync();
        }
        if (permission.status !== 'granted' || cancelled) {
          return;
        }
        const configuredProjectId = Constants.expoConfig?.extra?.expoProjectId
          || Constants.easConfig?.projectId
          || process.env.EXPO_PUBLIC_EXPO_PROJECT_ID;
        const projectId = configuredProjectId && !configuredProjectId.startsWith('REPLACE_')
          ? configuredProjectId
          : undefined;
        const tokenResponse = await Notifications.getExpoPushTokenAsync(
          projectId ? { projectId } : {},
        );
        if (cancelled || !tokenResponse?.data) {
          return;
        }
        pushTokenRef.current = tokenResponse.data;
        registerTokenInWebView(tokenResponse.data);
      } catch {
        // Push is optional; the in-app inbox remains available when setup is incomplete.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, ownerPushClientEnabled, registerTokenInWebView, workspaceKind]);

  useEffect(() => {
    const canRegisterPush = isAuthenticated && (
      workspaceKind === 'employee'
      || workspaceKind === 'client'
      || (workspaceKind === 'admin' && ownerPushClientEnabled)
    );
    if (canRegisterPush && pushTokenRef.current && workspacePath) {
      registerTokenInWebView(pushTokenRef.current);
    }
  }, [isAuthenticated, ownerPushClientEnabled, registerTokenInWebView, workspaceKind, workspacePath]);

  return null;
}

function SignInGate({ onWebViewLoadEnd, webViewRef }) {
  const pendingWorkspacePathRef = useRef(null);
  const {
    isAuthenticated,
    markAuthenticated,
    markUnauthenticated,
    updateNavigationCounts,
    updateWorkspacePath,
  } = useSession();
  const {
    beginAuthTransition,
    cancelAuthTransition,
    consumeLogoutRequest,
    finishAuthTransition,
    hideLaunchSplash,
    launchSplashVisible,
    setCurrentPath,
  } = useContext(SharedWebViewContext);
  const [canGoBack, setCanGoBack] = useState(false);
  const [currentWebViewPath, setCurrentWebViewPath] = useState(AUTH_PATH);
  const [hasError, setHasError] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const androidPullOffset = useRef(new Animated.Value(0)).current;
  const refreshInFlightRef = useRef(false);
  const nativeMediaBridgeRef = useRef(null);
  const nativeMediaBridgeTokenRef = useRef(null);
  if (!nativeMediaBridgeTokenRef.current) {
    nativeMediaBridgeTokenRef.current = createNativeMediaBridgeToken();
  }
  const mobileWebViewScript = useMemo(
    () => buildMobileWebViewScript(nativeMediaBridgeTokenRef.current),
    [],
  );
  const nativeMediaEnabled = process.env.EXPO_PUBLIC_NATIVE_MEDIA_ENABLED !== 'false';
  const canPullToRefresh = isAuthenticated && isPrivatePath(pathnameFromUrl(currentWebViewPath));
  const injectMobileChrome = useWebViewChrome(webViewRef, mobileWebViewScript);
  const loginSource = useMemo(() => ({
    headers: MOBILE_WEBVIEW_REQUEST_HEADERS,
    uri: pageUrl(`${AUTH_PATH}?mobile=1`),
  }), []);
  const webViewMotionStyle = Platform.OS === 'android'
    ? { transform: [{ translateY: androidPullOffset }] }
    : null;
  const isRefreshActive = isRefreshing || refreshInFlightRef.current;

  const recoilPullOffset = useCallback(() => {
    if (Platform.OS !== 'android') {
      return;
    }

    androidPullOffset.stopAnimation();
    Animated.timing(androidPullOffset, {
      duration: 220,
      easing: Easing.out(Easing.cubic),
      toValue: 0,
      useNativeDriver: true,
    }).start();
  }, [androidPullOffset]);

  const emitNativeMediaMessage = useCallback((message) => {
    if (!webViewRef.current) {
      return;
    }
    const encoded = JSON.stringify(message);
    webViewRef.current.injectJavaScript(
      '(function () { if (window.__grandCoastReceiveNativeMedia) { window.__grandCoastReceiveNativeMedia('
      + encoded
      + '); } true; })();',
    );
  }, [webViewRef]);

  useEffect(() => {
    if (!nativeMediaEnabled) {
      return undefined;
    }
    nativeMediaBridgeRef.current = createNativeMediaBridge({
      emit: emitNativeMediaMessage,
      trustedUploadOrigin: TRUSTED_WEB_APP && TRUSTED_WEB_APP.origin,
      trustedUploadPath: TRUSTED_WEB_APP && TRUSTED_WEB_APP.uploadPath,
    });
    return () => {
      nativeMediaBridgeRef.current = null;
    };
  }, [emitNativeMediaMessage, nativeMediaEnabled]);

  useEffect(() => {
    if (Platform.OS !== 'android') {
      return undefined;
    }

    const subscription = BackHandler.addEventListener('hardwareBackPress', () => {
      if (!canGoBack) {
        return false;
      }

      webViewRef.current?.goBack();
      return true;
    });

    return () => subscription.remove();
  }, [canGoBack, webViewRef]);

  const handleNavigationStateChange = (state) => {
    setCanGoBack(Boolean(state.canGoBack));

    const relativePath = relativePathFromUrl(state.url);
    const pathname = pathnameFromUrl(relativePath);
    if (isAdminPath(pathname)) {
      webViewRef.current?.injectJavaScript(
        `window.location.replace(${JSON.stringify(pageUrl(ADMIN_WORKSPACE_PATH))}); true;`,
      );
      return;
    }
    setCurrentWebViewPath(relativePath);
    setCurrentPath(relativePath);

    if (isPrivatePath(pathname)) {
      if (isAuthenticated) {
        updateWorkspacePath(relativePath);
      } else {
        pendingWorkspacePathRef.current = relativePath;
        if (!launchSplashVisible) {
          beginAuthTransition();
        }
      }
    } else if (isLoginPath(pathname)) {
      pendingWorkspacePathRef.current = null;
      if (isAuthenticated) {
        if (!launchSplashVisible) {
          beginAuthTransition();
        }
        markUnauthenticated();
      }
    }
  };

  const handleRequest = (request) => {
    const requestUrl = request.url;

    if (!isInternalUrl(requestUrl)) {
      return openExternalUrl(requestUrl);
    }

    const requestPathname = pathnameFromUrl(requestUrl);
    if (isAdminPath(requestPathname)) {
      webViewRef.current?.injectJavaScript(
        `window.location.replace(${JSON.stringify(pageUrl(ADMIN_WORKSPACE_PATH))}); true;`,
      );
      return false;
    }

    if (requestPathname === HOME_PATH) {
      return false;
    }

    return true;
  };

  const handleLoadEnd = (event) => {
    setIsLoading(false);
    setIsRefreshing(false);
    refreshInFlightRef.current = false;
    recoilPullOffset();
    injectMobileChrome();

    const loadedPath = relativePathFromUrl(event?.nativeEvent?.url || AUTH_PATH);
    const loadedPathname = pathnameFromUrl(loadedPath);
    if (isAdminPath(loadedPathname)) {
      webViewRef.current?.injectJavaScript(
        `window.location.replace(${JSON.stringify(pageUrl(ADMIN_WORKSPACE_PATH))}); true;`,
      );
      return;
    }
    setCurrentWebViewPath(loadedPath);
    setCurrentPath(loadedPath);
    onWebViewLoadEnd?.(loadedPath);
    const pendingWorkspacePath = pendingWorkspacePathRef.current;
    pendingWorkspacePathRef.current = null;
    if (consumeLogoutRequest()) {
      webViewRef.current?.injectJavaScript(MOBILE_LOGOUT_WITH_DEVICE_SCRIPT);
    }
    hideLaunchSplash().then(() => {
      if (pendingWorkspacePath && !isAuthenticated) {
        markAuthenticated(pendingWorkspacePath);
      }
      if (isLoginPath(loadedPathname) || (pendingWorkspacePath && !isAuthenticated)) {
        finishAuthTransition();
      }
    });
  };

  const handleError = () => {
    pendingWorkspacePathRef.current = null;
    setHasError(true);
    setIsLoading(false);
    setIsRefreshing(false);
    refreshInFlightRef.current = false;
    recoilPullOffset();
    cancelAuthTransition();
    hideLaunchSplash();
  };

  const handleMessage = (event) => {
    try {
      const message = JSON.parse(event.nativeEvent.data);
      if (message && typeof message.type === 'string' && message.type.indexOf('native-media-') === 0) {
        const mediaMessageTypes = new Set([
          'native-media-request',
          'native-media-upload-grants',
          'native-media-page-ready',
          'native-media-retry-queue',
        ]);
        const senderUrl = event.nativeEvent.url;
        let sender;
        try {
          sender = new URL(senderUrl);
        } catch {
          return;
        }
        const privateWorkspaceOpen = isPrivatePath(pathnameFromUrl(currentWebViewPath));
        const senderPathIsPrivate = isPrivatePath(sender.pathname)
          || (Platform.OS === 'android' && sender.pathname === '/');
        if (
          !nativeMediaEnabled
          || !nativeMediaBridgeRef.current
          || !mediaMessageTypes.has(message.type)
          || !privateWorkspaceOpen
          || !senderPathIsPrivate
          || !TRUSTED_WEB_APP
          || sender.origin !== TRUSTED_WEB_APP.origin
          || !['http:', 'https:'].includes(sender.protocol)
          || sender.username
          || sender.password
          || message.bridge_token !== nativeMediaBridgeTokenRef.current
        ) {
          return;
        }

        const isBoundedString = (value, maxLength) => (
          typeof value === 'string' && value.length > 0 && value.length <= maxLength
        );
        if (message.request_id !== undefined && !isBoundedString(message.request_id, 160)) return;
        if (message.type === 'native-media-request') {
          if (
            !['capture-media', 'pick-media', 'pick-document'].includes(message.action)
            || !isBoundedString(message.request_id, 160)
            || ['target', 'project_id', 'target_id', 'visibility', 'context', 'mime_types'].some(
              (key) => message[key] !== undefined && !isBoundedString(message[key], 240),
            )
            || (message.allow_multiple !== undefined && typeof message.allow_multiple !== 'boolean')
          ) return;
        }
        if (message.type === 'native-media-upload-grants') {
          if (
            !Array.isArray(message.grants)
            || !isBoundedString(message.request_id, 160)
            || message.grants.length === 0
            || message.grants.length > 24
            || message.grants.some((grant) => (
              !grant
              || typeof grant !== 'object'
              || !isBoundedString(grant.file_id, 160)
              || !isBoundedString(grant.upload_url, 2048)
              || !isBoundedString(grant.grant_token, 2048)
              || !isBoundedString(grant.idempotency_key, 160)
            ))
          ) return;
        }

        const { bridge_token: _bridgeToken, ...verifiedMessage } = message;
        nativeMediaBridgeRef.current.handleMessage(verifiedMessage).catch(() => {
          emitNativeMediaMessage({
            type: 'native-media-error',
            request_id: message.request_id,
            message: 'The device capability is temporarily unavailable.',
          });
        });
        return;
      }
      if (message.type === 'mobile-pull-to-refresh-progress') {
        if (Platform.OS === 'android' && canPullToRefresh && !refreshInFlightRef.current) {
          const distance = Number(message.distance);
          androidPullOffset.stopAnimation();
          androidPullOffset.setValue(
            Number.isFinite(distance) ? Math.max(0, Math.min(distance, 96)) : 0,
          );
        }
        return;
      }

      if (message.type === 'mobile-pull-to-refresh-cancel') {
        recoilPullOffset();
        return;
      }

      if (
        message.type === 'mobile-pull-to-refresh-release'
        || message.type === 'mobile-pull-to-refresh'
      ) {
        if (
          Platform.OS !== 'android'
          || !canPullToRefresh
          || refreshInFlightRef.current
        ) {
          recoilPullOffset();
          return;
        }

        const pullDistance = message.type === 'mobile-pull-to-refresh'
          ? 72
          : Number(message.distance);
        if (!Number.isFinite(pullDistance) || pullDistance < 72) {
          recoilPullOffset();
          return;
        }

        refreshInFlightRef.current = true;
        setIsRefreshing(true);
        androidPullOffset.stopAnimation();
        Animated.timing(androidPullOffset, {
          duration: 180,
          easing: Easing.out(Easing.cubic),
          toValue: 56,
          useNativeDriver: true,
        }).start(() => {
          if (webViewRef.current) {
            webViewRef.current.reload();
            return;
          }

          refreshInFlightRef.current = false;
          setIsRefreshing(false);
          recoilPullOffset();
        });
        return;
      }

      if (
        message.type === 'operations-navigation-counts'
        && (message.workspaceKind === 'admin' || message.workspaceKind === 'employee')
        && message.counts
      ) {
        updateNavigationCounts(message.workspaceKind, message.counts);
      }
      if (
        message.type === 'client-navigation-counts'
        && message.workspaceKind === 'client'
        && message.counts
      ) {
        updateNavigationCounts(message.workspaceKind, message.counts);
      }
    } catch {
      // Ignore unrelated WebView messages.
    }
  };

  return (
    <View style={styles.authGate}>
      <Animated.View style={[styles.webViewFrame, webViewMotionStyle]}>
        <WebView
          allowsBackForwardNavigationGestures
          domStorageEnabled
          injectedJavaScript={mobileWebViewScript}
          injectedJavaScriptBeforeContentLoaded={mobileWebViewScript}
          injectedJavaScriptBeforeContentLoadedForMainFrameOnly
          injectedJavaScriptForMainFrameOnly
          javaScriptEnabled
          onError={handleError}
          onLoadEnd={handleLoadEnd}
          onLoadStart={() => {
            setHasError(false);
            setIsLoading(true);
            setIsRefreshing(refreshInFlightRef.current);
          }}
          onMessage={handleMessage}
          onNavigationStateChange={handleNavigationStateChange}
          onShouldStartLoadWithRequest={handleRequest}
          originWhitelist={['http://*', 'https://*']}
          overScrollMode={Platform.OS === 'android' ? 'never' : undefined}
          pullToRefreshEnabled={Platform.OS === 'ios' && canPullToRefresh}
          ref={webViewRef}
          renderError={() => null}
          sharedCookiesEnabled
          source={loginSource}
          startInLoadingState
          style={styles.webView}
          testID="webview-sign-in"
          thirdPartyCookiesEnabled
          userAgent={MOBILE_WEBVIEW_USER_AGENT}
        />
      </Animated.View>

      {isLoading
        && !launchSplashVisible
        && !hasError
        && !canPullToRefresh
        && !isRefreshActive ? (
        <View pointerEvents="none" style={styles.loadingOverlay}>
          <ActivityIndicator color={colors.blue} size="large" />
          <Text style={styles.loadingText}>
            {isAuthenticated ? 'Loading Grand Coast...' : 'Loading sign in...'}
          </Text>
        </View>
      ) : null}

      {isRefreshActive ? (
        <View pointerEvents="none" style={styles.refreshOverlay}>
          <ActivityIndicator color={colors.blue} size="small" />
          <Text style={styles.refreshText}>Refreshing Grand Coast...</Text>
        </View>
      ) : null}

      {hasError ? (
        <View style={styles.errorOverlay}>
          <Ionicons color={colors.blue} name="cloud-offline-outline" size={42} />
          <Text style={styles.errorTitle}>Unable to reach Grand Coast</Text>
          <Text style={styles.errorText}>
            Check that the Django server is running and that the app is using the right local network address.
          </Text>
          <Pressable onPress={() => webViewRef.current?.reload()} style={styles.retryButton}>
            <Text style={styles.retryButtonText}>Try again</Text>
          </Pressable>
        </View>
      ) : null}

    </View>
  );
}

function EmployeeMoreItem({ page, onPress }) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={styles.moreItem}
    >
      <Ionicons color={page.danger ? colors.danger : colors.blue} name={page.icon} size={23} />
      <Text style={[styles.moreItemLabel, page.danger && styles.moreItemDanger]}>{page.label}</Text>
      <Ionicons color={page.danger ? colors.danger : colors.muted} name="chevron-forward-outline" size={19} />
    </Pressable>
  );
}

function NativeLegalScreen({ pageKey, onBack }) {
  const document = LEGAL_DOCUMENTS[pageKey];

  useEffect(() => {
    const subscription = BackHandler.addEventListener('hardwareBackPress', () => {
      onBack();
      return true;
    });
    return () => subscription.remove();
  }, [onBack]);

  return (
    <View style={styles.nativeLegalScreen} testID={`native-legal-${pageKey}`}>
      <ScrollView
        contentContainerStyle={styles.nativeLegalContent}
        showsVerticalScrollIndicator
      >
        <Pressable
          accessibilityLabel="Back to More"
          accessibilityRole="button"
          onPress={onBack}
          style={styles.nativeLegalBack}
        >
          <Ionicons color={colors.blue} name="arrow-back-outline" size={20} />
          <Text style={styles.nativeLegalBackLabel}>More</Text>
        </Pressable>

        <Text style={styles.nativeLegalKicker}>Grand Coast Construction Inc.</Text>
        <Text accessibilityRole="header" style={styles.nativeLegalTitle}>
          {document.title}
        </Text>
        <Text style={styles.nativeLegalUpdated}>{document.updated}</Text>
        <Text style={styles.nativeLegalIntro}>{document.intro}</Text>

        {document.sections.map((section) => (
          <View key={section.title} style={styles.nativeLegalSection}>
            <Text accessibilityRole="header" style={styles.nativeLegalSectionTitle}>
              {section.title}
            </Text>
            {section.paragraphs.map((paragraph, index) => (
              <Text key={`${section.title}-${index}`} style={styles.nativeLegalParagraph}>
                {paragraph}
              </Text>
            ))}
          </View>
        ))}
      </ScrollView>
    </View>
  );
}

function NativeContactField({
  autoCapitalize = 'sentences',
  keyboardType,
  label,
  multiline = false,
  onChangeText,
  placeholder,
  testID,
  value,
}) {
  return (
    <View style={styles.nativeContactField}>
      <Text style={styles.nativeContactFieldLabel}>{label}</Text>
      <TextInput
        autoCapitalize={autoCapitalize}
        autoCorrect={false}
        keyboardType={keyboardType}
        multiline={multiline}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor="#94A3B8"
        style={[styles.nativeContactInput, multiline && styles.nativeContactMessageInput]}
        testID={testID}
        value={value}
      />
    </View>
  );
}

function EmployeeContactScreen({ onBack }) {
  const [form, setForm] = useState(() => ({ ...EMPTY_NATIVE_CONTACT_FORM }));
  const [status, setStatus] = useState('idle');
  const [submitError, setSubmitError] = useState('');

  const updateField = useCallback((field, value) => {
    setForm((currentForm) => ({ ...currentForm, [field]: value }));
    setStatus((currentStatus) => (currentStatus === 'success' ? 'idle' : currentStatus));
    setSubmitError('');
  }, []);

  const submit = useCallback(async () => {
    const validationError = validateNativeContactForm(form);
    if (validationError) {
      setStatus('idle');
      setSubmitError(validationError);
      return;
    }

    setStatus('submitting');
    setSubmitError('');
    try {
      await submitNativeContactForm(form);
      setForm({ ...EMPTY_NATIVE_CONTACT_FORM });
      setStatus('success');
    } catch {
      setStatus('idle');
      setSubmitError("We couldn't send your message. Check your connection and try again.");
    }
  }, [form]);

  const isSubmitting = status === 'submitting';

  return (
    <View style={styles.nativeContactScreen} testID="employee-contact-form">
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
        style={styles.nativeContactKeyboard}
      >
        <ScrollView
          contentContainerStyle={styles.nativeContactContent}
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}
        >
          <Pressable
            accessibilityLabel="Back to More"
            accessibilityRole="button"
            onPress={onBack}
            style={styles.nativeContactBack}
            testID="employee-contact-back"
          >
            <Ionicons color={colors.blue} name="arrow-back-outline" size={20} />
            <Text style={styles.nativeContactBackLabel}>More</Text>
          </Pressable>

          <Text style={styles.nativeContactKicker}>Employee support</Text>
          <Text style={styles.nativeContactTitle}>Contact Grand Coast</Text>
          <Text style={styles.nativeContactIntro}>
            Send a message directly to the Grand Coast team.
          </Text>

          {status === 'success' ? (
            <View accessibilityLiveRegion="polite" style={[styles.nativeContactNotice, styles.nativeContactSuccess]}>
              <Text style={styles.nativeContactNoticeTitle}>Message sent</Text>
              <Text style={styles.nativeContactNoticeBody}>
                Thanks for reaching out. We will be in touch soon.
              </Text>
            </View>
          ) : null}

          {submitError ? (
            <View accessibilityLiveRegion="polite" style={[styles.nativeContactNotice, styles.nativeContactError]}>
              <Text style={styles.nativeContactNoticeTitle}>Unable to send</Text>
              <Text style={styles.nativeContactNoticeBody}>{submitError}</Text>
            </View>
          ) : null}

          <View style={styles.nativeContactCard}>
            <NativeContactField
              label="First name"
              onChangeText={(value) => updateField('first_name', value)}
              placeholder="First name"
              testID="employee-contact-first-name"
              value={form.first_name}
            />
            <NativeContactField
              label="Last name"
              onChangeText={(value) => updateField('last_name', value)}
              placeholder="Last name"
              testID="employee-contact-last-name"
              value={form.last_name}
            />
            <NativeContactField
              autoCapitalize="none"
              keyboardType="email-address"
              label="Email"
              onChangeText={(value) => updateField('email', value)}
              placeholder="you@example.com"
              testID="employee-contact-email"
              value={form.email}
            />
            <NativeContactField
              keyboardType="phone-pad"
              label="Phone"
              onChangeText={(value) => updateField('phone', value)}
              placeholder="Optional"
              testID="employee-contact-phone"
              value={form.phone}
            />
            <NativeContactField
              label="Project type"
              onChangeText={(value) => updateField('project_type', value)}
              placeholder="Remodel, restoration, or other"
              testID="employee-contact-project-type"
              value={form.project_type}
            />
            <NativeContactField
              label="Location"
              onChangeText={(value) => updateField('location', value)}
              placeholder="City or project address"
              testID="employee-contact-location"
              value={form.location}
            />
            <NativeContactField
              label="Tell us about the project"
              multiline
              onChangeText={(value) => updateField('message', value)}
              placeholder="How can we help?"
              testID="employee-contact-message"
              value={form.message}
            />
            <Pressable
              accessibilityRole="button"
              disabled={isSubmitting}
              onPress={submit}
              style={[styles.nativeContactSubmit, isSubmitting && styles.nativeContactSubmitDisabled]}
              testID="employee-contact-submit"
            >
              {isSubmitting ? (
                <ActivityIndicator color={colors.white} />
              ) : (
                <Text style={styles.nativeContactSubmitText}>Send message</Text>
              )}
            </Pressable>
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </View>
  );
}

function EmployeeMoreScreen({ onNavigate }) {
  const [showContactForm, setShowContactForm] = useState(false);
  const [legalPage, setLegalPage] = useState(null);
  const { requestLogout } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);
  const openPage = useCallback((page) => {
    if (page.path === PRIVACY_PATH || page.path === TERMS_PATH) {
      setLegalPage(page.path === PRIVACY_PATH ? 'privacy' : 'terms');
      return;
    }

    if (page.action === 'logout') {
      requestLogout(EMPLOYEE_WORKSPACE_PATH);
      setActiveTab('Workspace');
      setActiveWebPath(EMPLOYEE_WORKSPACE_PATH);
      return;
    }

    onNavigate('Workspace', page.path);
  }, [onNavigate, requestLogout, setActiveTab, setActiveWebPath, setLegalPage]);
  const openContactForm = useCallback(() => {
    setShowContactForm(true);
  }, []);
  const closeContactForm = useCallback(() => {
    setShowContactForm(false);
  }, []);

  if (showContactForm) {
    return <EmployeeContactScreen onBack={closeContactForm} />;
  }

  if (legalPage) {
    return <NativeLegalScreen pageKey={legalPage} onBack={() => setLegalPage(null)} />;
  }

  return (
    <View style={styles.moreScreen}>
      <ScrollView contentContainerStyle={styles.moreContent} showsVerticalScrollIndicator={false}>
        <Text style={styles.moreSectionLabel}>Account & support</Text>
        {employeeMorePages.slice(0, 2).map((page) => (
          <EmployeeMoreItem
            key={page.path}
            onPress={() => openPage(page)}
            page={page}
          />
        ))}
        <EmployeeMoreItem page={employeeMoreContactPage} onPress={openContactForm} />

        <Text style={[styles.moreSectionLabel, styles.moreSectionLabelSpaced]}>Session</Text>
        {employeeMorePages.slice(2).map((page) => (
          <EmployeeMoreItem
            key={page.path}
            onPress={() => openPage(page)}
            page={page}
          />
        ))}
      </ScrollView>
    </View>
  );
}

function AdminMoreScreen({ onNavigate }) {
  const [legalPage, setLegalPage] = useState(null);
  const { requestLogout } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);
  const openPage = useCallback((page) => {
    if (page.path === PRIVACY_PATH || page.path === TERMS_PATH) {
      setLegalPage(page.path === PRIVACY_PATH ? 'privacy' : 'terms');
      return;
    }

    if (page.action === 'logout') {
      requestLogout(ADMIN_WORKSPACE_PATH);
      setActiveTab('Dashboard');
      setActiveWebPath(ADMIN_WORKSPACE_PATH);
      return;
    }

    onNavigate('Workspace', page.path);
  }, [onNavigate, requestLogout, setActiveTab, setActiveWebPath, setLegalPage]);

  if (legalPage) {
    return <NativeLegalScreen pageKey={legalPage} onBack={() => setLegalPage(null)} />;
  }

  return (
    <View style={styles.moreScreen}>
      <ScrollView contentContainerStyle={styles.moreContent} showsVerticalScrollIndicator={false}>
        <Text style={styles.moreSectionLabel}>Account & support</Text>
        {adminMorePages.slice(0, 2).map((page) => (
          <EmployeeMoreItem
            key={page.path}
            onPress={() => openPage(page)}
            page={page}
          />
        ))}

        <Text style={[styles.moreSectionLabel, styles.moreSectionLabelSpaced]}>Session</Text>
        <EmployeeMoreItem page={adminDrawerLogout} onPress={() => openPage(adminDrawerLogout)} />
      </ScrollView>
    </View>
  );
}

function ClientMoreScreen({ onNavigate }) {
  const [legalPage, setLegalPage] = useState(null);
  const { requestLogout } = useContext(SharedWebViewContext);
  const { setActiveTab, setActiveWebPath } = useContext(NativeShellContext);
  const openPage = useCallback((page) => {
    if (page.path === PRIVACY_PATH || page.path === TERMS_PATH) {
      setLegalPage(page.path === PRIVACY_PATH ? 'privacy' : 'terms');
      return;
    }

    if (page.action === 'logout') {
      requestLogout(CLIENT_OVERVIEW_PATH);
      setActiveTab('Overview');
      setActiveWebPath(CLIENT_OVERVIEW_PATH);
      return;
    }

    onNavigate('Overview', page.path);
  }, [onNavigate, requestLogout, setActiveTab, setActiveWebPath, setLegalPage]);

  if (legalPage) {
    return <NativeLegalScreen pageKey={legalPage} onBack={() => setLegalPage(null)} />;
  }

  return (
    <View style={styles.moreScreen}>
      <ScrollView contentContainerStyle={styles.moreContent} showsVerticalScrollIndicator={false}>
        <Text style={styles.moreSectionLabel}>Account & support</Text>
        {clientMorePages.slice(0, 2).map((page) => (
          <EmployeeMoreItem
            key={page.path}
            onPress={() => openPage(page)}
            page={page}
          />
        ))}

        <Text style={[styles.moreSectionLabel, styles.moreSectionLabelSpaced]}>Session</Text>
        {clientMorePages.slice(2).map((page) => (
          <EmployeeMoreItem
            key={page.path}
            onPress={() => openPage(page)}
            page={page}
          />
        ))}
      </ScrollView>
    </View>
  );
}

function NativeTabBar({ activeTab, insets, isEmployee, onSelect }) {
  const icons = isEmployee ? employeeTabIcons : clientTabIcons;
  const tabs = isEmployee ? employeeBottomTabs : clientBottomTabs;

  return (
    <View
      style={[
        styles.tabBar,
        { height: 62 + insets.bottom, paddingBottom: 7 + insets.bottom },
      ]}
    >
      {tabs.map((tab) => {
        const focused = activeTab === tab.name;
        const [outline, filled] = icons[tab.name];

        return (
          <Pressable
            accessibilityRole="button"
            accessibilityState={{ selected: focused }}
            key={tab.name}
            onPress={() => onSelect(tab.name)}
            style={styles.tabBarItem}
          >
            <Ionicons
              color={focused ? colors.gold : colors.muted}
              name={focused ? filled : outline}
              size={25}
            />
            <Text style={[styles.tabBarLabel, { color: focused ? colors.gold : colors.muted }]}>
              {tab.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

function MainTabs() {
  const insets = useSafeAreaInsets();
  const pendingTabRef = useRef(null);
  const { isAuthenticated, workspaceKind, workspacePath } = useSession();
  const { navigate, webViewRef } = useContext(SharedWebViewContext);
  const {
    activeTab,
    activeWebPath: selectedWebPath,
    setActiveTab,
    setActiveWebPath,
  } = useContext(NativeShellContext);
  const isAdmin = workspaceKind === 'admin';
  const isEmployee = workspaceKind === 'employee';
  const isOperations = isAdmin || isEmployee;
  const isMore = (isOperations || workspaceKind === 'client') && activeTab === 'More';
  const tabBarHeight = 62 + insets.bottom;
  const handleWebViewLoadEnd = useCallback((loadedPath) => {
    const pendingTab = pendingTabRef.current;
    if (!pendingTab || pathnameFromUrl(loadedPath) !== pathnameFromUrl(pendingTab.path)) {
      return;
    }

    pendingTabRef.current = null;
    setActiveTab(pendingTab.name);
  }, [setActiveTab]);
  const navigateFromMore = useCallback((tabName, path) => {
    const pendingTab = { name: tabName, path };
    pendingTabRef.current = pendingTab;
    setActiveWebPath(path);
    if (navigate(path) === false) {
      pendingTabRef.current = null;
      setActiveTab(tabName);
    }
  }, [navigate, setActiveTab, setActiveWebPath]);
  const activeWebPath = useMemo(() => {
    if (!isAuthenticated || isMore) {
      return null;
    }

    if (selectedWebPath) {
      return selectedWebPath;
    }

    if (isOperations) {
      if (isAdmin) {
        return ADMIN_WORKSPACE_PATH;
      }

      return activeTab === 'Dashboard' ? EMPLOYEE_WORKSPACE_PATH : EMPLOYEE_PROJECTS_PATH;
    }

    if (activeTab === 'Overview') {
      return CLIENT_OVERVIEW_PATH;
    }

    if (activeTab === 'Notifications') {
      return CLIENT_NOTIFICATIONS_PATH;
    }

    return workspacePath || CLIENT_OVERVIEW_PATH;
  }, [activeTab, isAdmin, isAuthenticated, isOperations, isMore, selectedWebPath, workspacePath]);

  useEffect(() => {
    if (activeWebPath) {
      navigate(activeWebPath);
    }
  }, [activeWebPath, navigate]);

  const selectTab = useCallback((tabName) => {
    if (tabName === 'More') {
      pendingTabRef.current = null;
      setActiveTab(tabName);
      setActiveWebPath(null);
      return;
    }

    const tabPath = isOperations
      ? isAdmin ? ADMIN_WORKSPACE_PATH
        : tabName === 'Dashboard' ? EMPLOYEE_WORKSPACE_PATH : EMPLOYEE_PROJECTS_PATH
      : tabName === 'Overview' ? CLIENT_OVERVIEW_PATH
        : tabName === 'Notifications' ? CLIENT_NOTIFICATIONS_PATH
          : CLIENT_OVERVIEW_PATH;

    if (activeTab === 'More') {
      navigateFromMore(tabName, tabPath);
      return;
    }

    pendingTabRef.current = null;
    setActiveTab(tabName);
    setActiveWebPath(tabPath);
  }, [activeTab, isAdmin, isOperations, navigateFromMore, setActiveTab, setActiveWebPath]);

  return (
    <View style={styles.mainTabs}>
      <View
        pointerEvents={isMore ? 'none' : 'auto'}
        style={[
          styles.mainTabsWebView,
          isAuthenticated && !isMore && { marginBottom: tabBarHeight },
          isMore && styles.mainTabsWebViewHidden,
        ]}
      >
        <SignInGate onWebViewLoadEnd={handleWebViewLoadEnd} webViewRef={webViewRef} />
      </View>

      {isMore ? (
        <View style={[styles.nativeContentOverlay, { bottom: tabBarHeight }]}>
          {isAdmin
            ? <AdminMoreScreen onNavigate={navigateFromMore} />
            : isEmployee
              ? <EmployeeMoreScreen onNavigate={navigateFromMore} />
              : <ClientMoreScreen onNavigate={navigateFromMore} />}
        </View>
      ) : null}

      {isAuthenticated ? (
        <NativeTabBar
          activeTab={activeTab}
          insets={insets}
          isEmployee={isOperations}
          onSelect={selectTab}
        />
      ) : null}
    </View>
  );
}

function AppShell() {
  const { isAuthenticated, workspaceKind } = useSession();
  const isAdmin = workspaceKind === 'admin';
  const isEmployee = workspaceKind === 'employee';
  const isOperations = isAdmin || isEmployee;
  const [activeTab, setActiveTab] = useState(null);
  const [activeWebPath, setActiveWebPath] = useState(null);
  const validTabs = isOperations ? employeeBottomTabs : clientBottomTabs;
  const selectedTab = !isAuthenticated
    ? null
    : validTabs.some((tab) => tab.name === activeTab)
      ? activeTab
      : isOperations ? 'Dashboard' : 'Overview';
  useEffect(() => {
    setActiveTab(isAuthenticated ? (isOperations ? 'Dashboard' : 'Overview') : null);
    setActiveWebPath(null);
  }, [isAuthenticated, isOperations]);

  const nativeShellValue = useMemo(
    () => ({
      activeTab: selectedTab,
      activeWebPath,
      setActiveTab,
      setActiveWebPath,
    }),
    [activeWebPath, selectedTab],
  );

  return (
    <NativeShellContext.Provider value={nativeShellValue}>
      <PushNotificationBridge />
      <View style={styles.appShell}>
        <NavigationContainer theme={transparentNavigationTheme}>
          <Drawer.Navigator
            drawerContent={(props) => <AppDrawerContent {...props} />}
            screenOptions={{
              drawerStyle: !isAuthenticated
                ? [styles.drawer, styles.hiddenDrawer]
                : isOperations ? [styles.drawer, styles.employeeDrawer] : styles.drawer,
              drawerType: 'front',
              header: (props) => <AppHeader {...props} />,
              headerShown: isAuthenticated,
              overlayColor: 'rgba(16, 43, 78, 0.38)',
              swipeEnabled: isAuthenticated,
              swipeEdgeWidth: 36,
            }}
          >
            <Drawer.Screen component={MainTabs} name="Main" options={{ title: 'Grand Coast' }} />
          </Drawer.Navigator>
        </NavigationContainer>
      </View>
    </NativeShellContext.Provider>
  );
}

export default function App() {
  const sharedWebViewRef = useRef(null);
  const sharedWebViewPathRef = useRef(null);
  const sharedWebViewLogoutPendingRef = useRef(false);
  const navigationCountsRef = useRef({});
  const launchSplashStartedAtRef = useRef(Date.now());
  const launchSplashHandledRef = useRef(false);
  const launchSplashOpacity = useRef(new Animated.Value(1)).current;
  const authTransitionTimeoutRef = useRef(null);
  const authTransitionActiveRef = useRef(false);
  const authTransitionOpacity = useRef(new Animated.Value(0)).current;
  const [launchSplashVisible, setLaunchSplashVisible] = useState(true);
  const [authTransitionVisible, setAuthTransitionVisible] = useState(false);
  const [session, setSession] = useState({
    isAuthenticated: false,
    navigationCounts: {},
    workspaceKind: null,
    workspacePath: null,
  });

  const beginAuthTransition = useCallback(() => {
    if (authTransitionTimeoutRef.current) {
      clearTimeout(authTransitionTimeoutRef.current);
      authTransitionTimeoutRef.current = null;
    }

    authTransitionActiveRef.current = true;
    setAuthTransitionVisible(true);
    authTransitionOpacity.stopAnimation();
    Animated.timing(authTransitionOpacity, {
      duration: 160,
      easing: Easing.out(Easing.cubic),
      toValue: 1,
      useNativeDriver: true,
    }).start();
  }, [authTransitionOpacity]);

  const finishAuthTransition = useCallback(() => {
    if (!authTransitionActiveRef.current) {
      return;
    }

    authTransitionActiveRef.current = false;
    if (authTransitionTimeoutRef.current) {
      clearTimeout(authTransitionTimeoutRef.current);
    }

    authTransitionTimeoutRef.current = setTimeout(() => {
      authTransitionTimeoutRef.current = null;
      Animated.timing(authTransitionOpacity, {
        duration: 280,
        easing: Easing.out(Easing.cubic),
        toValue: 0,
        useNativeDriver: true,
      }).start(({ finished }) => {
        if (finished) {
          setAuthTransitionVisible(false);
        }
      });
    }, 90);
  }, [authTransitionOpacity]);

  const cancelAuthTransition = useCallback(() => {
    authTransitionActiveRef.current = false;
    if (authTransitionTimeoutRef.current) {
      clearTimeout(authTransitionTimeoutRef.current);
      authTransitionTimeoutRef.current = null;
    }

    authTransitionOpacity.stopAnimation();
    authTransitionOpacity.setValue(0);
    setAuthTransitionVisible(false);
  }, [authTransitionOpacity]);

  useEffect(() => () => {
    authTransitionActiveRef.current = false;
    if (authTransitionTimeoutRef.current) {
      clearTimeout(authTransitionTimeoutRef.current);
    }
    authTransitionOpacity.stopAnimation();
  }, [authTransitionOpacity]);

  const markAuthenticated = useCallback((urlOrPath) => {
    const relativePath = relativePathFromUrl(urlOrPath);
    const pathname = pathnameFromUrl(relativePath);
    const workspaceKind = workspaceKindFromPath(pathname);

    setSession({
      isAuthenticated: true,
      navigationCounts: navigationCountsRef.current[workspaceKind] || {},
      workspaceKind,
      workspacePath: isPrivatePath(pathname) ? relativePath : null,
    });
  }, []);

  const markUnauthenticated = useCallback(() => {
    setSession({
      isAuthenticated: false,
      navigationCounts: {},
      workspaceKind: null,
      workspacePath: null,
    });
    navigationCountsRef.current = {};
  }, []);

  const setSharedWebViewPath = useCallback((urlOrPath) => {
    sharedWebViewPathRef.current = relativePathFromUrl(urlOrPath);
  }, []);

  const navigateSharedWebView = useCallback((path) => {
    const relativePath = relativePathFromUrl(path);
    if (sharedWebViewPathRef.current === relativePath) {
      return false;
    }

    const webView = sharedWebViewRef.current;
    if (!webView) {
      return false;
    }

    sharedWebViewPathRef.current = relativePath;
    webView.injectJavaScript(
      'window.location.assign(' + JSON.stringify(pageUrl(relativePath)) + '); true;',
    );
    return true;
  }, []);

  const requestSharedWebViewLogout = useCallback((fallbackWorkspacePath = EMPLOYEE_WORKSPACE_PATH) => {
    beginAuthTransition();
    const currentPath = sharedWebViewPathRef.current;
    if (!isPrivatePath(pathnameFromUrl(currentPath))) {
      sharedWebViewLogoutPendingRef.current = true;
      sharedWebViewPathRef.current = fallbackWorkspacePath;
      sharedWebViewRef.current?.injectJavaScript(
        'window.location.assign(' + JSON.stringify(pageUrl(fallbackWorkspacePath)) + '); true;',
      );
      return;
    }

    sharedWebViewLogoutPendingRef.current = false;
    sharedWebViewPathRef.current = fallbackWorkspacePath;
    sharedWebViewRef.current?.injectJavaScript(MOBILE_LOGOUT_WITH_DEVICE_SCRIPT);
  }, [beginAuthTransition]);

  const consumeLogoutRequest = useCallback(() => {
    const isPending = sharedWebViewLogoutPendingRef.current;
    sharedWebViewLogoutPendingRef.current = false;
    return isPending;
  }, []);

  const hideLaunchSplash = useCallback(() => {
    if (launchSplashHandledRef.current) {
      return Promise.resolve();
    }

    launchSplashHandledRef.current = true;

    const elapsedMs = Date.now() - launchSplashStartedAtRef.current;
    const remainingHoldMs = Math.max(0, LAUNCH_SPLASH_HOLD_MS - elapsedMs);
    const hold = remainingHoldMs
      ? new Promise((resolve) => setTimeout(resolve, remainingHoldMs))
      : Promise.resolve();

    return hold
      .then(() => SplashScreen.hideAsync().catch(() => {}))
      .then(
        () => new Promise((resolve) => {
          Animated.timing(launchSplashOpacity, {
            duration: LAUNCH_SPLASH_FADE_MS,
            easing: Easing.out(Easing.cubic),
            toValue: 0,
            useNativeDriver: true,
          }).start(() => {
            setLaunchSplashVisible(false);
            resolve();
          });
        }),
      );
  }, [launchSplashOpacity]);

  const updateWorkspacePath = useCallback((urlOrPath) => {
    const relativePath = relativePathFromUrl(urlOrPath);
    const pathname = pathnameFromUrl(relativePath);

    if (!isPrivatePath(pathname)) {
      return;
    }

    setSession((currentSession) => {
      const workspaceKind = currentSession.workspaceKind || workspaceKindFromPath(pathname);
      if (
        currentSession.workspaceKind === workspaceKind
        && currentSession.workspacePath === relativePath
      ) {
        return currentSession;
      }

      return {
        ...currentSession,
        workspaceKind,
        workspacePath: relativePath,
      };
    });
  }, []);

  const updateNavigationCounts = useCallback((workspaceKind, counts) => {
    if (workspaceKind !== 'admin' && workspaceKind !== 'employee' && workspaceKind !== 'client') {
      return;
    }

    const countKeys = workspaceKind === 'client'
      ? ['estimates', 'messages', 'notifications']
      : [
        'overview',
        'clients',
        'tasks',
        'calendar',
        'time',
        'documents',
        'leads',
        'estimates',
        'projects',
        'media',
        'content',
        'team',
        'messages',
        'notifications',
        'profile',
      ];
    const sanitizedCounts = countKeys.reduce((result, key) => {
      const value = Number(counts[key]);
      result[key] = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
      return result;
    }, {});
    navigationCountsRef.current[workspaceKind] = sanitizedCounts;

    setSession((currentSession) => {
      if (currentSession.workspaceKind && currentSession.workspaceKind !== workspaceKind) {
        return currentSession;
      }

      return {
        ...currentSession,
        navigationCounts: sanitizedCounts,
      };
    });
  }, []);

  const sessionValue = useMemo(
    () => ({
      ...session,
      markAuthenticated,
      markUnauthenticated,
      updateNavigationCounts,
      updateWorkspacePath,
    }),
    [markAuthenticated, markUnauthenticated, session, updateNavigationCounts, updateWorkspacePath],
  );

  const sharedWebViewValue = useMemo(
    () => ({
      beginAuthTransition,
      cancelAuthTransition,
      navigate: navigateSharedWebView,
      consumeLogoutRequest,
      finishAuthTransition,
      hideLaunchSplash,
      launchSplashVisible,
      requestLogout: requestSharedWebViewLogout,
      setCurrentPath: setSharedWebViewPath,
      webViewRef: sharedWebViewRef,
    }),
    [
      beginAuthTransition,
      cancelAuthTransition,
      consumeLogoutRequest,
      finishAuthTransition,
      hideLaunchSplash,
      launchSplashVisible,
      navigateSharedWebView,
      requestSharedWebViewLogout,
      setSharedWebViewPath,
    ],
  );

  return (
    <SafeAreaProvider>
      <SessionContext.Provider value={sessionValue}>
        <SharedWebViewContext.Provider value={sharedWebViewValue}>
          <View style={styles.mobileRoot}>
            <AppShell />
            {launchSplashVisible ? (
              <Animated.View
                pointerEvents="none"
                style={[styles.splashTransition, { opacity: launchSplashOpacity }]}
              >
                <Image source={SPLASH_LOGO} style={styles.splashTransitionLogo} />
              </Animated.View>
            ) : null}
            {authTransitionVisible ? (
              <Animated.View
                pointerEvents="auto"
                style={[styles.authTransition, { opacity: authTransitionOpacity }]}
              >
                <ActivityIndicator color={colors.blue} size="large" />
                <Text style={styles.loadingText}>Opening workspace...</Text>
              </Animated.View>
            ) : null}
          </View>
        </SharedWebViewContext.Provider>
      </SessionContext.Provider>
      <StatusBar
        backgroundColor={session.isAuthenticated ? colors.navy : colors.white}
        style={session.isAuthenticated ? 'light' : 'dark'}
      />
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  appShell: {
    flex: 1,
  },
  authGate: {
    backgroundColor: colors.white,
    flex: 1,
    overflow: 'hidden',
  },
  mobileRoot: {
    flex: 1,
  },
  hiddenDrawer: {
    width: 0,
  },
  mainTabs: {
    backgroundColor: colors.canvas,
    flex: 1,
    position: 'relative',
  },
  mainTabsWebView: {
    flex: 1,
  },
  mainTabsWebViewHidden: {
    bottom: 0,
    left: 0,
    opacity: 0,
    position: 'absolute',
    right: 0,
    top: 0,
  },
  nativeContentOverlay: {
    backgroundColor: colors.canvas,
    bottom: 0,
    elevation: 2,
    left: 0,
    position: 'absolute',
    right: 0,
    top: 0,
    zIndex: 2,
  },
  drawer: {
    backgroundColor: colors.white,
    width: 306,
  },
  employeeDrawer: {
    backgroundColor: colors.navy,
  },
  drawerBrand: {
    alignItems: 'center',
    borderBottomColor: colors.border,
    borderBottomWidth: StyleSheet.hairlineWidth,
    flexDirection: 'row',
    gap: 12,
    marginBottom: 22,
    paddingBottom: 22,
    paddingHorizontal: 20,
    paddingTop: 12,
  },
  drawerBrandSubtitle: {
    color: colors.muted,
    fontSize: 11,
    letterSpacing: 0.7,
    marginTop: 2,
    textTransform: 'uppercase',
  },
  drawerBrandTitle: {
    color: colors.navy,
    fontFamily: Platform.select({ ios: 'Georgia', default: 'serif' }),
    fontSize: 20,
    fontWeight: '700',
  },
  drawerContent: {
    flexGrow: 1,
    paddingBottom: 20,
  },
  employeeDrawerContent: {
    backgroundColor: colors.navy,
    flexGrow: 1,
    paddingBottom: 24,
    paddingHorizontal: 10,
  },
  employeeDrawerItem: {
    borderRadius: 8,
    marginHorizontal: 0,
    marginVertical: 2,
  },
  employeeDrawerItemLabel: {
    color: '#F5F7FA',
    fontSize: 15,
    fontWeight: '700',
    marginLeft: -8,
  },
  drawerItemLabelRow: {
    alignItems: 'center',
    flex: 1,
    flexDirection: 'row',
    gap: 8,
    justifyContent: 'space-between',
  },
  drawerItemLabelText: {
    flex: 1,
  },
  nativeNavCount: {
    alignItems: 'center',
    backgroundColor: 'rgba(214, 163, 42, 0.2)',
    borderRadius: 20,
    justifyContent: 'center',
    minWidth: 24,
    paddingHorizontal: 6,
    paddingVertical: 2,
  },
  clientNativeNavCount: {
    backgroundColor: '#EDF2F8',
    borderColor: '#D9E1EF',
    borderWidth: 1,
  },
  nativeNavCountText: {
    color: '#F7E8BB',
    fontSize: 11,
    fontWeight: '800',
    textAlign: 'center',
  },
  clientNativeNavCountText: {
    color: colors.blue,
  },
  employeeDrawerSectionLabel: {
    color: '#AFC4E2',
    marginBottom: 12,
    marginHorizontal: 14,
    marginTop: 34,
  },
  employeeDrawerWorkflowGroup: {
    borderTopColor: 'rgba(255, 255, 255, 0.28)',
    borderTopWidth: StyleSheet.hairlineWidth,
    color: '#AFC4E2',
    fontSize: 10,
    fontWeight: '800',
    letterSpacing: 1.1,
    marginBottom: 7,
    marginHorizontal: 14,
    marginTop: 18,
    paddingTop: 16,
    textTransform: 'uppercase',
  },
  employeeDrawerTopRule: {
    borderTopColor: 'rgba(255, 255, 255, 0.28)',
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  employeeDrawerBottomRule: {
    borderTopColor: 'rgba(255, 255, 255, 0.28)',
    borderTopWidth: StyleSheet.hairlineWidth,
    marginBottom: 8,
    marginTop: 'auto',
  },
  drawerFooter: {
    borderTopColor: colors.border,
    borderTopWidth: StyleSheet.hairlineWidth,
    marginHorizontal: 20,
    marginTop: 'auto',
    paddingTop: 18,
  },
  drawerFooterText: {
    color: colors.muted,
    fontSize: 12,
    lineHeight: 19,
  },
  drawerItem: {
    borderRadius: 12,
    marginHorizontal: 10,
    marginVertical: 1,
  },
  drawerItemLabel: {
    fontSize: 15,
    fontWeight: '600',
    marginLeft: -8,
  },
  nativeContactBack: {
    alignItems: 'center',
    flexDirection: 'row',
    gap: 6,
    marginBottom: 26,
  },
  nativeContactBackLabel: {
    color: colors.blue,
    fontSize: 14,
    fontWeight: '800',
  },
  nativeContactCard: {
    backgroundColor: colors.white,
    borderColor: colors.border,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    padding: 18,
  },
  nativeContactContent: {
    padding: 24,
    paddingBottom: 44,
  },
  nativeContactError: {
    backgroundColor: '#FFF4F3',
    borderColor: '#E8C4C0',
    borderWidth: 1,
  },
  nativeContactField: {
    marginBottom: 16,
  },
  nativeContactFieldLabel: {
    color: colors.navy,
    fontSize: 12,
    fontWeight: '800',
    marginBottom: 7,
  },
  nativeContactInput: {
    backgroundColor: '#FBFCFE',
    borderColor: '#CDD7E4',
    borderRadius: 6,
    borderWidth: 1,
    color: colors.ink,
    fontSize: 15,
    minHeight: 46,
    paddingHorizontal: 12,
    paddingVertical: 10,
  },
  nativeContactIntro: {
    color: colors.muted,
    fontSize: 14,
    lineHeight: 21,
    marginBottom: 22,
    marginTop: 8,
  },
  nativeContactKeyboard: {
    flex: 1,
  },
  nativeContactKicker: {
    color: colors.blue,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1.2,
    textTransform: 'uppercase',
  },
  nativeContactMessageInput: {
    minHeight: 120,
    textAlignVertical: 'top',
  },
  nativeContactNotice: {
    borderRadius: 10,
    marginBottom: 16,
    padding: 14,
  },
  nativeContactNoticeBody: {
    color: colors.muted,
    fontSize: 13,
    lineHeight: 19,
    marginTop: 4,
  },
  nativeContactNoticeTitle: {
    color: colors.navy,
    fontSize: 14,
    fontWeight: '800',
  },
  nativeContactScreen: {
    backgroundColor: colors.canvas,
    flex: 1,
  },
  nativeContactSubmit: {
    alignItems: 'center',
    backgroundColor: colors.blue,
    borderRadius: 10,
    justifyContent: 'center',
    marginTop: 2,
    minHeight: 50,
    paddingHorizontal: 20,
  },
  nativeContactSubmitDisabled: {
    opacity: 0.65,
  },
  nativeContactSubmitText: {
    color: colors.white,
    fontSize: 15,
    fontWeight: '800',
  },
  nativeContactSuccess: {
    backgroundColor: '#E9F7EF',
    borderColor: '#B9DFC7',
    borderWidth: 1,
  },
  nativeContactTitle: {
    color: colors.navy,
    fontFamily: Platform.select({ ios: 'Georgia', default: 'serif' }),
    fontSize: 30,
    fontWeight: '700',
    marginTop: 7,
  },
  nativeLegalBack: {
    alignItems: 'center',
    flexDirection: 'row',
    gap: 7,
    marginBottom: 28,
    minHeight: 40,
  },
  nativeLegalBackLabel: {
    color: colors.blue,
    fontSize: 14,
    fontWeight: '800',
  },
  nativeLegalContent: {
    padding: 24,
    paddingBottom: 44,
  },
  nativeLegalIntro: {
    color: colors.ink,
    fontSize: 15,
    lineHeight: 23,
    marginBottom: 8,
  },
  nativeLegalKicker: {
    color: colors.blue,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1.2,
    textTransform: 'uppercase',
  },
  nativeLegalParagraph: {
    color: colors.ink,
    fontSize: 14,
    lineHeight: 22,
    marginBottom: 12,
  },
  nativeLegalScreen: {
    backgroundColor: colors.canvas,
    flex: 1,
  },
  nativeLegalSection: {
    borderTopColor: colors.border,
    borderTopWidth: StyleSheet.hairlineWidth,
    marginTop: 14,
    paddingTop: 18,
  },
  nativeLegalSectionTitle: {
    color: colors.navy,
    fontSize: 17,
    fontWeight: '800',
    marginBottom: 10,
  },
  nativeLegalTitle: {
    color: colors.navy,
    fontFamily: Platform.select({ ios: 'Georgia', default: 'serif' }),
    fontSize: 32,
    fontWeight: '700',
    marginTop: 7,
  },
  nativeLegalUpdated: {
    color: colors.muted,
    fontSize: 12,
    marginBottom: 18,
    marginTop: 8,
  },
  moreContent: {
    padding: 24,
    paddingBottom: 34,
  },
  moreItem: {
    alignItems: 'center',
    backgroundColor: colors.white,
    borderColor: colors.border,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    flexDirection: 'row',
    gap: 14,
    marginBottom: 12,
    minHeight: 66,
    paddingHorizontal: 18,
  },
  moreItemDanger: {
    color: colors.danger,
  },
  moreItemLabel: {
    color: colors.navy,
    flex: 1,
    fontSize: 15,
    fontWeight: '700',
  },
  moreScreen: {
    backgroundColor: colors.canvas,
    flex: 1,
  },
  moreSectionLabel: {
    color: colors.blue,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1.2,
    marginBottom: 12,
    textTransform: 'uppercase',
  },
  moreSectionLabelSpaced: {
    marginTop: 22,
  },
  drawerMark: {
    alignItems: 'center',
    backgroundColor: colors.navy,
    borderRadius: 12,
    height: 46,
    justifyContent: 'center',
    width: 46,
  },
  drawerMarkText: {
    color: colors.gold,
    fontSize: 16,
    fontWeight: '800',
    letterSpacing: 1,
  },
  drawerSectionLabel: {
    color: colors.muted,
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 1.3,
    marginBottom: 6,
    marginHorizontal: 20,
    textTransform: 'uppercase',
  },
  drawerSectionLabelSpaced: {
    marginTop: 22,
  },
  errorOverlay: {
    alignItems: 'center',
    backgroundColor: colors.canvas,
    bottom: 0,
    justifyContent: 'center',
    left: 0,
    padding: 30,
    position: 'absolute',
    right: 0,
    top: 0,
  },
  errorText: {
    color: colors.muted,
    fontSize: 14,
    lineHeight: 21,
    maxWidth: 320,
    textAlign: 'center',
  },
  errorTitle: {
    color: colors.navy,
    fontSize: 20,
    fontWeight: '800',
    marginBottom: 8,
    marginTop: 16,
  },
  header: {
    backgroundColor: colors.navy,
  },
  headerBrand: {
    flex: 1,
    marginHorizontal: 14,
  },
  headerButton: {
    alignItems: 'center',
    height: 44,
    justifyContent: 'center',
    width: 44,
  },
  headerRow: {
    alignItems: 'center',
    flexDirection: 'row',
    height: 58,
    paddingHorizontal: 10,
  },
  headerSubtitle: {
    color: '#CAD6E7',
    fontSize: 10,
    letterSpacing: 1.1,
    marginTop: 1,
    textTransform: 'uppercase',
  },
  headerTitle: {
    color: colors.white,
    fontFamily: Platform.select({ ios: 'Georgia', default: 'serif' }),
    fontSize: 20,
    fontWeight: '700',
  },
  loadingOverlay: {
    alignItems: 'center',
    backgroundColor: 'rgba(245, 247, 250, 0.92)',
    bottom: 0,
    justifyContent: 'center',
    left: 0,
    position: 'absolute',
    right: 0,
    top: 0,
  },
  loadingText: {
    color: colors.ink,
    fontSize: 13,
    fontWeight: '600',
    marginTop: 12,
  },
  refreshOverlay: {
    alignItems: 'center',
    backgroundColor: 'rgba(255, 255, 255, 0.96)',
    borderColor: colors.border,
    borderRadius: 18,
    borderWidth: StyleSheet.hairlineWidth,
    flexDirection: 'row',
    left: 16,
    paddingHorizontal: 14,
    paddingVertical: 10,
    position: 'absolute',
    right: 16,
    top: 12,
    zIndex: 3,
  },
  refreshText: {
    color: colors.ink,
    fontSize: 12,
    fontWeight: '700',
    marginLeft: 9,
  },
  retryButton: {
    backgroundColor: colors.blue,
    borderRadius: 10,
    marginTop: 22,
    paddingHorizontal: 24,
    paddingVertical: 12,
  },
  retryButtonText: {
    color: colors.white,
    fontSize: 14,
    fontWeight: '800',
  },
  splashTransition: {
    alignItems: 'center',
    backgroundColor: colors.white,
    bottom: 0,
    justifyContent: 'center',
    left: 0,
    position: 'absolute',
    right: 0,
    top: 0,
  },
  authTransition: {
    alignItems: 'center',
    backgroundColor: colors.white,
    bottom: 0,
    justifyContent: 'center',
    left: 0,
    position: 'absolute',
    right: 0,
    top: 0,
    zIndex: 20,
  },
  splashTransitionLogo: {
    height: 220,
    width: 220,
  },
  tabBar: {
    bottom: 0,
    flexDirection: 'row',
    backgroundColor: colors.white,
    borderTopColor: colors.border,
    borderTopWidth: StyleSheet.hairlineWidth,
    elevation: 12,
    left: 0,
    paddingTop: 6,
    position: 'absolute',
    right: 0,
    zIndex: 10,
  },
  tabBarItem: {
    alignItems: 'center',
    flex: 1,
    justifyContent: 'center',
  },
  tabBarLabel: {
    fontSize: 11,
    fontWeight: '700',
  },
  webView: {
    backgroundColor: colors.canvas,
    flex: 1,
  },
  webViewFrame: {
    flex: 1,
  },
});
