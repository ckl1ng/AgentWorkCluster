const FILE_MAGIC = new Uint8Array([67, 72, 65, 84, 70, 73, 76, 69, 1]);
const MAX_FILE_BYTES = 10 * 1024 * 1024;

function bytesEqual(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function safeFileName(name) {
  return (name || 'file').replace(/[\\/:*?"<>|]/g, '_').slice(0, 180) || 'file';
}

export function isImageType(contentType) {
  return ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(contentType);
}

export function validateFile(file) {
  if (!file) return '未找到文件';
  const limit = isImageType(file.type) ? 5 * 1024 * 1024 : MAX_FILE_BYTES;
  if (file.size > limit) return `${isImageType(file.type) ? '图片' : '文件'}不能超过 ${limit / 1024 / 1024} MiB`;
  return null;
}

export async function createFilePayload(file, fileName = file.name, caption = '') {
  const data = new Uint8Array(await file.arrayBuffer());
  const header = new TextEncoder().encode(JSON.stringify({
    name: safeFileName(fileName),
    type: file.type || 'application/octet-stream',
    size: data.length,
    caption: String(caption).slice(0, 4000),
  }));
  const payload = new Uint8Array(FILE_MAGIC.length + 4 + header.length + data.length);
  const view = new DataView(payload.buffer);
  payload.set(FILE_MAGIC);
  view.setUint32(FILE_MAGIC.length, header.length);
  payload.set(header, FILE_MAGIC.length + 4);
  payload.set(data, FILE_MAGIC.length + 4 + header.length);
  return payload;
}

export function parseFilePayload(payload) {
  if (payload.length < FILE_MAGIC.length + 4 || !bytesEqual(payload.slice(0, FILE_MAGIC.length), FILE_MAGIC)) return null;
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const headerLength = view.getUint32(FILE_MAGIC.length);
  const dataOffset = FILE_MAGIC.length + 4 + headerLength;
  if (headerLength > 2048 || dataOffset > payload.length) return null;
  try {
    const header = JSON.parse(new TextDecoder().decode(payload.slice(FILE_MAGIC.length + 4, dataOffset)));
    const data = payload.slice(dataOffset);
    if (typeof header.name !== 'string' || typeof header.type !== 'string' || header.size !== data.length) return null;
    if (header.caption !== undefined && typeof header.caption !== 'string') return null;
    return {
      name: safeFileName(header.name),
      type: header.type,
      size: data.length,
      caption: header.caption || '',
      isImage: isImageType(header.type),
      data,
    };
  } catch {
    return null;
  }
}

export function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const FILE_CONTENT_TYPE = 'application/octet-stream';
