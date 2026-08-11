/**
 * 加密模块 — tweetnacl 封装
 *
 * 提供与后端 E2EE 模型匹配的加密/解密接口：
 * - 非对称加密（私聊）：nacl.box / nacl.box.open
 * - 对称加密（群聊）：nacl.secretbox / nacl.secretbox.open
 * - 群密钥分发：用成员公钥加密群对称密钥
 */

import nacl from 'tweetnacl';
import { bytesToBase64, base64ToBytes } from './utils.js';

const PASSWORD_SALT_BYTES = 16;
const NONCE_BYTES = 24;
const PBKDF2_ITERATIONS = 210000;
const FALLBACK_PBKDF2_ITERATIONS = 100000;

function hasWebCrypto() {
  return Boolean(globalThis.crypto?.subtle?.importKey && globalThis.crypto?.subtle?.deriveBits);
}

function concatBytes(...parts) {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const combined = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    combined.set(part, offset);
    offset += part.length;
  }
  return combined;
}

function hmacSha512(key, data) {
  const block = new Uint8Array(128);
  block.set(key.length > 128 ? nacl.hash(key) : key);
  const inner = new Uint8Array(128);
  const outer = new Uint8Array(128);
  for (let i = 0; i < block.length; i++) {
    inner[i] = block[i] ^ 0x36;
    outer[i] = block[i] ^ 0x5c;
  }
  return nacl.hash(concatBytes(outer, nacl.hash(concatBytes(inner, data))));
}

async function derivePasswordKeyFallback(password, salt) {
  const passwordBytes = new TextEncoder().encode(password);
  const blockIndex = new Uint8Array([0, 0, 0, 1]);
  let block = hmacSha512(passwordBytes, concatBytes(salt, blockIndex));
  const output = new Uint8Array(block);
  for (let i = 1; i < FALLBACK_PBKDF2_ITERATIONS; i++) {
    block = hmacSha512(passwordBytes, block);
    for (let j = 0; j < output.length; j++) output[j] ^= block[j];
    // Yield periodically so registration remains responsive on older browsers.
    if (i % 512 === 0) await new Promise(resolve => setTimeout(resolve, 0));
  }
  return output.slice(0, 32);
}

async function derivePasswordKey(password, salt, useFallback = false) {
  if (useFallback || !hasWebCrypto()) return derivePasswordKeyFallback(password, salt);
  const material = await globalThis.crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(password),
    'PBKDF2',
    false,
    ['deriveBits']
  );
  const bits = await globalThis.crypto.subtle.deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations: PBKDF2_ITERATIONS },
    material,
    256
  );
  return new Uint8Array(bits);
}

export async function encryptSecretKeyForPassword(secretKey, password) {
  const useFallback = !hasWebCrypto();
  const salt = nacl.randomBytes(PASSWORD_SALT_BYTES);
  const key = await derivePasswordKey(password, salt, useFallback);
  const nonce = nacl.randomBytes(NONCE_BYTES);
  const ciphertext = nacl.secretbox(secretKey, nonce, key);
  const combined = new Uint8Array(salt.length + nonce.length + ciphertext.length);
  combined.set(salt);
  combined.set(nonce, salt.length);
  combined.set(ciphertext, salt.length + nonce.length);
  // v2 uses the TweetNaCl PBKDF2 fallback for HTTP origins where Web Crypto is unavailable.
  return `${useFallback ? 'v2.' : 'v1.'}${bytesToBase64(combined)}`;
}

export async function decryptSecretKeyForPassword(encryptedSecretKey, password) {
  try {
    const useFallback = encryptedSecretKey.startsWith('v2.');
    const encoded = encryptedSecretKey.replace(/^v[12]\./, '');
    const combined = base64ToBytes(encoded);
    if (combined.length < PASSWORD_SALT_BYTES + NONCE_BYTES + nacl.secretbox.overheadLength) {
      return null;
    }
    const salt = combined.slice(0, PASSWORD_SALT_BYTES);
    const nonce = combined.slice(PASSWORD_SALT_BYTES, PASSWORD_SALT_BYTES + NONCE_BYTES);
    const ciphertext = combined.slice(PASSWORD_SALT_BYTES + NONCE_BYTES);
    const key = await derivePasswordKey(password, salt, useFallback);
    return nacl.secretbox.open(ciphertext, nonce, key);
  } catch {
    return null;
  }
}

// ── 密钥对生成 ──

/** 生成 Curve25519 密钥对 */
export function generateKeyPair() {
  const kp = nacl.box.keyPair();
  return {
    publicKey: kp.publicKey,   // Uint8Array(32)
    secretKey: kp.secretKey,   // Uint8Array(32)
  };
}

/** 生成对称群密钥（32 字节随机数）*/
export function generateGroupKey() {
  return nacl.randomBytes(32);
}

// ── 私聊：非对称加密 ──

/**
 * 加密私聊消息
 * 格式：nonce(24B) || ciphertext → Base64
 *
 * @param {string} plaintext
 * @param {Uint8Array} recipientPublicKey
 * @param {Uint8Array} senderSecretKey
 * @returns {string} Base64 编码的 nonce + 密文
 */
export function encryptPrivate(plaintext, recipientPublicKey, senderSecretKey) {
  return encryptPrivateBytes(new TextEncoder().encode(plaintext), recipientPublicKey, senderSecretKey);
}

export function encryptPrivateBytes(message, recipientPublicKey, senderSecretKey) {
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.box(message, nonce, recipientPublicKey, senderSecretKey);
  // 拼接 nonce + ciphertext
  const combined = new Uint8Array(nonce.length + encrypted.length);
  combined.set(nonce);
  combined.set(encrypted, nonce.length);
  return bytesToBase64(combined);
}

/**
 * 解密私聊消息
 * 格式：Base64 → nonce(24B) + ciphertext → 解密
 *
 * @param {string} encryptedBase64 - Base64 编码的 nonce + 密文
 * @param {Uint8Array} senderPublicKey
 * @param {Uint8Array} recipientSecretKey
 * @returns {string|null} 明文，解密失败返回 null
 */
export function decryptPrivate(encryptedBase64, senderPublicKey, recipientSecretKey) {
  const decrypted = decryptPrivateBytes(encryptedBase64, senderPublicKey, recipientSecretKey);
  return decrypted ? new TextDecoder().decode(decrypted) : null;
}

export function decryptPrivateBytes(encryptedBase64, senderPublicKey, recipientSecretKey) {
  try {
    const combined = base64ToBytes(encryptedBase64);
    const nonce = combined.slice(0, 24);
    const ciphertext = combined.slice(24);
    const decrypted = nacl.box.open(ciphertext, nonce, senderPublicKey, recipientSecretKey);
    if (!decrypted) return null;
    return decrypted;
  } catch {
    return null;
  }
}

// ── 群聊：对称加密 ──

/**
 * 对称加密群消息
 * 格式：nonce(24B) || ciphertext → Base64
 *
 * @param {string} plaintext
 * @param {Uint8Array} groupKey
 * @returns {string} Base64 编码
 */
export function encryptGroup(plaintext, groupKey) {
  return encryptGroupBytes(new TextEncoder().encode(plaintext), groupKey);
}

export function encryptGroupBytes(message, groupKey) {
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.secretbox(message, nonce, groupKey);
  const combined = new Uint8Array(nonce.length + encrypted.length);
  combined.set(nonce);
  combined.set(encrypted, nonce.length);
  return bytesToBase64(combined);
}

/**
 * 对称解密群消息
 *
 * @param {string} encryptedBase64 - Base64 编码的 nonce + 密文
 * @param {Uint8Array} groupKey
 * @returns {string|null} 明文
 */
export function decryptGroup(encryptedBase64, groupKey) {
  const decrypted = decryptGroupBytes(encryptedBase64, groupKey);
  return decrypted ? new TextDecoder().decode(decrypted) : null;
}

export function decryptGroupBytes(encryptedBase64, groupKey) {
  try {
    const combined = base64ToBytes(encryptedBase64);
    const nonce = combined.slice(0, 24);
    const ciphertext = combined.slice(24);
    const decrypted = nacl.secretbox.open(ciphertext, nonce, groupKey);
    if (!decrypted) return null;
    return decrypted;
  } catch {
    return null;
  }
}

// ── 群密钥分发 ──

/**
 * 用成员公钥加密群密钥
 *
 * @param {Uint8Array} groupKey - 群对称密钥
 * @param {Uint8Array} memberPublicKey - 成员公钥
 * @param {Uint8Array} creatorSecretKey - 创建者私钥
 * @returns {string} Base64 编码的 nonce + 加密群密钥
 */
export function encryptGroupKeyForMember(groupKey, memberPublicKey, creatorSecretKey) {
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.box(groupKey, nonce, memberPublicKey, creatorSecretKey);
  const combined = new Uint8Array(nonce.length + encrypted.length);
  combined.set(nonce);
  combined.set(encrypted, nonce.length);
  return bytesToBase64(combined);
}

/**
 * 解密群密钥（成员获取群密钥时调用）
 *
 * @param {string} encryptedKeyBase64 - Base64 编码
 * @param {Uint8Array} creatorPublicKey - 创建者公钥
 * @param {Uint8Array} memberSecretKey - 成员私钥
 * @returns {Uint8Array|null} 群密钥（32 字节）
 */
export function decryptGroupKey(encryptedKeyBase64, creatorPublicKey, memberSecretKey) {
  try {
    const combined = base64ToBytes(encryptedKeyBase64);
    const nonce = combined.slice(0, 24);
    const ciphertext = combined.slice(24);
    return nacl.box.open(ciphertext, nonce, creatorPublicKey, memberSecretKey);
  } catch {
    return null;
  }
}
