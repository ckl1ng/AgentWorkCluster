import { writable } from 'svelte/store';

const DB_NAME = 'chat-client';
const STORE_NAME = 'stickers';
const MAX_STICKERS = 100;

export const stickers = writable([]);
let activeUserId = null;

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
      store.createIndex('user_id', 'user_id', { unique: false });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function loadStickers(userId) {
  activeUserId = userId;
  try {
    const db = await openDatabase();
    const transaction = db.transaction(STORE_NAME, 'readonly');
    const entries = await requestResult(transaction.objectStore(STORE_NAME).index('user_id').getAll(userId));
    db.close();
    stickers.set(entries.sort((a, b) => b.created_at - a.created_at));
  } catch {
    stickers.set([]);
  }
}

export async function addSticker(file) {
  if (!activeUserId) throw new Error('表情库尚未就绪');
  const db = await openDatabase();
  const transaction = db.transaction(STORE_NAME, 'readwrite');
  const store = transaction.objectStore(STORE_NAME);
  const existing = await requestResult(store.index('user_id').getAll(activeUserId));
  if (existing.length >= MAX_STICKERS) {
    existing.sort((a, b) => a.created_at - b.created_at).slice(0, existing.length - MAX_STICKERS + 1)
      .forEach(entry => store.delete(entry.id));
  }
  await requestResult(store.add({
    user_id: activeUserId,
    name: file.name || 'sticker',
    type: file.type,
    blob: file instanceof Blob ? file : new Blob([file], { type: file.type }),
    created_at: Date.now(),
  }));
  db.close();
  await loadStickers(activeUserId);
}

export async function removeSticker(id) {
  const db = await openDatabase();
  const transaction = db.transaction(STORE_NAME, 'readwrite');
  await requestResult(transaction.objectStore(STORE_NAME).delete(id));
  db.close();
  if (activeUserId) await loadStickers(activeUserId);
}
