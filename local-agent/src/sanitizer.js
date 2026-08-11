const SECRET_PATTERNS = [
  [/((?:authorization)\s*:\s*bearer\s+)[^\s,;]+/gi, '$1[REDACTED]'],
  [/(\b(?:cookie|set-cookie|basic)\s*:\s*)[^\r\n]+/gi, '$1[REDACTED]'],
  [/(\b(?:api[_-]?key|access[_-]?key|secret|token|password|passwd)\s*[=:]\s*)[^\s,;]+/gi, '$1[REDACTED]'],
  [/\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_.-]{10,}\.[a-zA-Z0-9_.-]{10,}\b/g, '[REDACTED_JWT]'],
  [/-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----/g, '[REDACTED_PRIVATE_KEY]'],
  [/(postgres(?:ql)?|mysql|redis):\/\/([^\s/@]+):([^\s/@]+)@/gi, '$1://[REDACTED]@[REDACTED]@'],
];

export function sanitize(value, { workspaceRoot, homeDir, secrets = [] } = {}) {
  if (value == null) return { value: value == null ? '' : String(value), redactionCount: 0 };
  let output = String(value);
  let redactionCount = 0;
  for (const secret of secrets) {
    if (!secret) continue;
    const escaped = String(secret).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(escaped, 'g');
    output = output.replace(re, () => { redactionCount += 1; return '[REDACTED]'; });
  }
  for (const [pattern, replacement] of SECRET_PATTERNS) {
    output = output.replace(pattern, (...args) => {
      redactionCount += 1;
      if (typeof replacement === 'function') return replacement(...args);
      return replacement.replace(/\$(\d+)/g, (_, index) => args[Number(index)] || '');
    });
  }
  for (const prefix of [homeDir, workspaceRoot]) {
    if (prefix) {
      const escaped = String(prefix).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const re = new RegExp(escaped, 'g');
      output = output.replace(re, () => { redactionCount += 1; return prefix === workspaceRoot ? '<workspace>' : '~'; });
    }
  }
  return { value: output, redactionCount };
}

export class StreamSanitizer {
  constructor(options = {}) { this.options = options; this.carry = ''; }
  push(chunk) {
    const input = this.carry + String(chunk);
    this.carry = input.slice(-8192);
    const complete = input.slice(0, Math.max(0, input.length - this.carry.length));
    return sanitize(complete, this.options);
  }
  flush() {
    const result = sanitize(this.carry, this.options);
    this.carry = '';
    return result;
  }
}
