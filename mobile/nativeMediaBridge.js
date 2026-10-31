import * as DocumentPicker from 'expo-document-picker';
import * as FileSystem from 'expo-file-system';
import * as ImagePicker from 'expo-image-picker';

const QUEUE_DIRECTORY = (FileSystem.documentDirectory || '') + 'grand-coast-upload-queue/';
const QUEUE_FILE = QUEUE_DIRECTORY + 'queue.json';
const MAX_QUEUE_ITEMS = 24;

function randomId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return 'gcc-' + Date.now() + '-' + Math.random().toString(36).slice(2);
}

function safeFilename(name) {
  const normalized = String(name || 'upload')
    .replace(/[\\/]/g, '-')
    .replace(/[^A-Za-z0-9._-]/g, '-');
  return normalized.slice(-180) || 'upload';
}

const MEDIA_MIME_EXTENSIONS = {
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
  'video/mp4': '.mp4',
  'video/quicktime': '.mov',
};
const MEDIA_EXTENSION_MIMES = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.mp4': 'video/mp4',
  '.mov': 'video/quicktime',
};
const ALLOWED_MEDIA_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov']);

function supportedMediaFilename(asset, fallbackName) {
  const originalName = safeFilename(asset.name || asset.fileName || fallbackName);
  const dot = originalName.lastIndexOf('.');
  const hasExtension = dot > 0;
  const suppliedExtension = hasExtension ? originalName.slice(dot).toLowerCase() : '';
  const mimeType = String(asset.mimeType || '').split(';', 1)[0].trim().toLowerCase();
  const assetType = String(asset.type || '').trim().toLowerCase();
  const mimeExtension = MEDIA_MIME_EXTENSIONS[mimeType];
  const extension = suppliedExtension || mimeExtension;

  if (!extension || !ALLOWED_MEDIA_EXTENSIONS.has(extension)) {
    throw new Error('Use JPG, PNG, or WebP photos, or MP4 or MOV videos. GIF and HEIC files are not supported.');
  }
  if (
    (mimeType.startsWith('image/') && !['image/jpeg', 'image/png', 'image/webp'].includes(mimeType))
    || (mimeType.startsWith('video/') && !['video/mp4', 'video/quicktime'].includes(mimeType))
  ) {
    throw new Error('Use JPG, PNG, or WebP photos, or MP4 or MOV videos. GIF and HEIC files are not supported.');
  }
  if (
    (mimeType.startsWith('image/') && ['.mp4', '.mov'].includes(extension))
    || (mimeType.startsWith('video/') && ['.jpg', '.jpeg', '.png', '.webp'].includes(extension))
    || (assetType === 'image' && ['.mp4', '.mov'].includes(extension))
    || (assetType === 'video' && ['.jpg', '.jpeg', '.png', '.webp'].includes(extension))
  ) {
    throw new Error('The selected file type does not match its photo or video format.');
  }

  const stem = hasExtension ? originalName.slice(0, dot) : originalName;
  return `${stem}${extension}`;
}

function fileMetadata(entry) {
  return {
    request_id: entry.requestId,
    file_id: entry.fileId,
    name: entry.name,
    size: entry.size,
    type: entry.type,
    action: entry.action,
    target: entry.target,
    project_id: entry.projectId,
    target_id: entry.targetId,
    visibility: entry.visibility,
    context: entry.context,
    status: entry.status,
  };
}

export function createNativeMediaBridge({ emit, trustedUploadOrigin, trustedUploadPath }) {
  let queue = [];
  let queueLoaded = false;
  let queueLoadPromise = null;

  const post = (message) => {
    try {
      emit(message);
    } catch {
      // Native capability failures must never interrupt WebView navigation.
    }
  };

  function isTrustedUploadDestination(value) {
    if (!trustedUploadOrigin || !trustedUploadPath || typeof value !== 'string') {
      return false;
    }
    try {
      const uploadUrl = new URL(value);
      return (
        uploadUrl.origin === trustedUploadOrigin
        && uploadUrl.pathname === trustedUploadPath
        && !uploadUrl.search
        && !uploadUrl.hash
        && !uploadUrl.username
        && !uploadUrl.password
      );
    } catch {
      return false;
    }
  }

  async function ensureQueueDirectory() {
    if (!FileSystem.documentDirectory) {
      throw new Error('The app-private file directory is unavailable.');
    }
    await FileSystem.makeDirectoryAsync(QUEUE_DIRECTORY, { intermediates: true });
  }

  async function loadQueue() {
    if (queueLoaded) {
      return queue;
    }
    if (!queueLoadPromise) {
      queueLoadPromise = (async () => {
        try {
          await ensureQueueDirectory();
          const info = await FileSystem.getInfoAsync(QUEUE_FILE);
          if (info.exists) {
            const stored = JSON.parse(await FileSystem.readAsStringAsync(QUEUE_FILE));
            queue = Array.isArray(stored) ? stored.slice(-MAX_QUEUE_ITEMS) : [];
          }
        } catch {
          queue = [];
        } finally {
          queueLoaded = true;
        }
        return queue;
      })();
    }
    return queueLoadPromise;
  }

  async function persistQueue() {
    await ensureQueueDirectory();
    await FileSystem.writeAsStringAsync(QUEUE_FILE, JSON.stringify(queue.slice(-MAX_QUEUE_ITEMS)));
  }

  async function removeLocalFile(uri) {
    if (!uri) {
      return;
    }
    try {
      await FileSystem.deleteAsync(uri, { idempotent: true });
    } catch {
      // A missing cache file is already clean enough for the retry queue.
    }
  }

  async function copyToPrivateQueue(uri, name, fileId) {
    await ensureQueueDirectory();
    const destination = QUEUE_DIRECTORY + fileId + '-' + safeFilename(name);
    await FileSystem.copyAsync({ from: uri, to: destination });
    return destination;
  }

  async function selectMedia(message) {
    const requestId = message.request_id || randomId();
    let result;
    try {
      if (message.action === 'capture-media') {
        const permission = await ImagePicker.requestCameraPermissionsAsync();
        if (!permission.granted) {
          post({ type: 'native-media-error', request_id: requestId, message: 'Camera permission was not granted.' });
          return;
        }
        result = await ImagePicker.launchCameraAsync({
          allowsEditing: false,
          mediaTypes: ImagePicker.MediaTypeOptions.All,
          quality: 1,
          videoMaxDuration: 120,
        });
      } else if (message.action === 'pick-media') {
        const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
        if (!permission.granted) {
          post({ type: 'native-media-error', request_id: requestId, message: 'Photo library permission was not granted.' });
          return;
        }
        result = await ImagePicker.launchImageLibraryAsync({
          allowsEditing: false,
          mediaTypes: ImagePicker.MediaTypeOptions.All,
          allowsMultipleSelection: message.allow_multiple !== false,
          quality: 1,
          selectionLimit: message.allow_multiple === false ? 1 : 0,
        });
      } else if (message.action === 'pick-document') {
        result = await DocumentPicker.getDocumentAsync({
          copyToCacheDirectory: true,
          multiple: message.allow_multiple !== false,
          type: message.mime_types || '*/*',
        });
      } else {
        post({ type: 'native-media-error', request_id: requestId, message: 'Unsupported native media action.' });
        return;
      }
    } catch {
      post({ type: 'native-media-error', request_id: requestId, message: 'The device picker could not be opened.' });
      return;
    }

    if (!result || result.canceled || result.cancelled) {
      post({ type: 'native-media-canceled', request_id: requestId });
      return;
    }

    const assets = result.assets || (result.uri ? [result] : []);
    let mediaAssets;
    if (message.action === 'pick-document') {
      mediaAssets = assets.slice(0, MAX_QUEUE_ITEMS).map((asset) => ({
        asset,
        name: safeFilename(asset.name || asset.fileName || ('upload-' + randomId())),
      }));
    } else {
      try {
        mediaAssets = assets.slice(0, MAX_QUEUE_ITEMS).map((asset) => {
          const fallbackName = (message.action === 'capture-media' ? 'capture-' : 'upload-') + randomId();
          return { asset, name: supportedMediaFilename(asset, fallbackName) };
        });
      } catch (error) {
        post({
          type: 'native-media-error',
          request_id: requestId,
          message: error instanceof Error ? error.message : 'Choose JPG, PNG, or WebP photos, or MP4 or MOV videos.',
        });
        return;
      }
    }
    const entries = [];
    try {
      for (const { asset, name } of mediaAssets) {
        if (!asset?.uri) {
          continue;
        }
        const fileId = randomId();
        const privateUri = await copyToPrivateQueue(asset.uri, name, fileId);
        const privateInfo = await FileSystem.getInfoAsync(privateUri, { size: true });
        entries.push({
          requestId,
          fileId,
          name,
          size: Number(asset.fileSize || asset.size || privateInfo.size || 0) || null,
          type: MEDIA_EXTENSION_MIMES[name.slice(name.lastIndexOf('.')).toLowerCase()]
            || asset.mimeType
            || asset.type
            || 'application/octet-stream',
          localUri: privateUri,
          action: message.action,
          target: message.target || 'project_media',
          projectId: message.project_id || '',
          targetId: message.target_id || '',
          visibility: message.visibility || 'internal',
          context: message.context || 'progress',
          status: 'selected',
        });
      }
    } catch {
      await Promise.all(entries.map((entry) => removeLocalFile(entry.localUri)));
      post({ type: 'native-media-error', request_id: requestId, message: 'The selected file could not be staged privately.' });
      return;
    }

    if (!entries.length) {
      post({ type: 'native-media-canceled', request_id: requestId });
      return;
    }

    queue = queue.filter((entry) => entry.requestId !== requestId);
    queue.push(...entries);
    await persistQueue();
    post({
      type: 'native-media-selected',
      request_id: requestId,
      files: entries.map(fileMetadata),
    });
  }

  function uploadEntry(entry, grant) {
    return new Promise((resolve) => {
      if (!isTrustedUploadDestination(grant.upload_url)) {
        entry.status = 'retry';
        persistQueue().catch(() => {});
        post({
          type: 'native-media-upload-error',
          request_id: entry.requestId,
          file_id: entry.fileId,
          status: 400,
          message: 'The upload destination was rejected.',
          retry_available: false,
        });
        resolve(false);
        return;
      }
      const request = new XMLHttpRequest();
      const form = new FormData();
      const uploadUrl = grant.upload_url;
      entry.status = 'uploading';
      persistQueue().catch(() => {});
      post({ type: 'native-media-upload-progress', request_id: entry.requestId, file_id: entry.fileId, progress: 0, status: 'uploading' });

      request.open('POST', uploadUrl);
      request.timeout = 120000;
      request.setRequestHeader('Accept', 'application/json');
      request.setRequestHeader('X-Grand-Coast-Upload-Token', grant.grant_token);
      request.setRequestHeader('Idempotency-Key', grant.idempotency_key);
      request.upload.onprogress = (event) => {
        const progress = event.lengthComputable ? Math.round((event.loaded / event.total) * 100) : 0;
        post({ type: 'native-media-upload-progress', request_id: entry.requestId, file_id: entry.fileId, progress, status: 'uploading' });
      };
      request.onload = async () => {
        let payload = {};
        try {
          payload = JSON.parse(request.responseText || '{}');
        } catch {
          payload = {};
        }
        if (request.status >= 200 && request.status < 300 && payload.uploaded) {
          await removeLocalFile(entry.localUri);
          queue = queue.filter((item) => item.fileId !== entry.fileId);
          await persistQueue();
          post({ type: 'native-media-uploaded', request_id: entry.requestId, file_id: entry.fileId, result: payload });
          resolve(true);
          return;
        }
        entry.status = 'retry';
        await persistQueue();
        post({
          type: 'native-media-upload-error',
          request_id: entry.requestId,
          file_id: entry.fileId,
          status: request.status,
          message: payload.error || 'The upload was rejected.',
          retry_available: true,
        });
        resolve(false);
      };
      request.onerror = async () => {
        entry.status = 'retry';
        await persistQueue();
        post({ type: 'native-media-upload-error', request_id: entry.requestId, file_id: entry.fileId, status: 0, message: 'The upload could not reach Grand Coast. It is saved for retry.', retry_available: true });
        resolve(false);
      };
      request.ontimeout = request.onerror;
      form.append('file', {
        uri: entry.localUri,
        name: entry.name,
        type: entry.type || 'application/octet-stream',
      });
      request.send(form);
    });
  }

  async function uploadGrants(message) {
    await loadQueue();
    if (typeof message.request_id !== 'string' || !message.request_id || message.request_id.length > 160) {
      return;
    }
    const grants = Array.isArray(message.grants) ? message.grants : [];
    for (const grant of grants) {
      if (
        !grant
        || typeof grant.file_id !== 'string'
        || !grant.file_id
        || grant.file_id.length > 160
        || typeof grant.grant_token !== 'string'
        || !grant.grant_token
        || grant.grant_token.length > 2048
        || typeof grant.idempotency_key !== 'string'
        || !grant.idempotency_key
        || grant.idempotency_key.length > 160
        || !isTrustedUploadDestination(grant.upload_url)
      ) {
        post({
          type: 'native-media-upload-error',
          request_id: message.request_id,
          file_id: grant && grant.file_id,
          status: 400,
          message: 'The upload grant was invalid.',
          retry_available: false,
        });
        continue;
      }
      const entry = queue.find((item) => (
        item.fileId === grant.file_id
        && item.requestId === message.request_id
      ));
      if (!entry) {
        post({ type: 'native-media-upload-error', request_id: message.request_id, file_id: grant.file_id, status: 404, message: 'The staged file is no longer available.', retry_available: false });
        continue;
      }
      await uploadEntry(entry, grant);
    }
  }

  async function emitPendingQueue() {
    await loadQueue();
    if (!queue.length) {
      return;
    }
    post({
      type: 'native-media-queue-pending',
      files: queue.map(fileMetadata),
    });
  }

  async function handleMessage(message) {
    if (!message || typeof message.type !== 'string') {
      return;
    }
    if (message.type === 'native-media-request') {
      await selectMedia(message);
      return;
    }
    if (message.type === 'native-media-upload-grants') {
      await uploadGrants(message);
      return;
    }
    if (message.type === 'native-media-page-ready' || message.type === 'native-media-retry-queue') {
      await emitPendingQueue();
    }
  }

  return {
    handleMessage,
    loadQueue,
    emitPendingQueue,
  };
}
