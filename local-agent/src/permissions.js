import fs from 'node:fs/promises';

export async function ensurePrivateDirectory(directory) {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('local-agent data directory must be a real directory');
  if (stat.mode & 0o077) throw new Error('local-agent data directory permissions are too broad');
}

export async function assertPrivateFile(file) {
  let stat;
  try { stat = await fs.lstat(file); } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('local-agent state file must be a regular file');
  if (stat.mode & 0o077) throw new Error('local-agent state file permissions are too broad');
  return true;
}
