/** Read the one trusted Fluxer bot credential from a Node-only secret mount.
 *
 * The Python connector config and private RPC carry only their own IPC bearer.
 * Missing/invalid secret files fail before SDK login and never echo their path
 * or content through startup diagnostics.
 */
import {readFile, stat} from 'node:fs/promises';
import {isAbsolute} from 'node:path';

/** Load one nonempty opaque token from a bounded, absolute secret file. */
export async function readBotTokenFromFile(path: string): Promise<string> {
  if (!path || !isAbsolute(path) || path.includes('\0')) {
    throw new Error('Fluxer bot token secret file path must be absolute');
  }
  let raw: string;
  try {
    const info = await stat(path);
    if (!info.isFile() || info.size < 1 || info.size > 4096) {
      throw new Error('bad secret file');
    }
    raw = await readFile(path, {encoding: 'utf8'});
  } catch {
    // Node's filesystem errors can contain the configured filename. Never
    // interpolate them into a log reachable by the Python bridge.
    throw new Error('Fluxer bot token secret file is unavailable or invalid');
  }
  // A final LF is a normal secret-file terminator; do not trim the opaque
  // credential itself or silently repair an operator's malformed token.
  const token = raw.replace(/\r?\n$/, '');
  if (!token || token.length > 4096 || /\s|\p{Cc}/u.test(token)) {
    throw new Error('Fluxer bot token secret file is unavailable or invalid');
  }
  return token;
}
