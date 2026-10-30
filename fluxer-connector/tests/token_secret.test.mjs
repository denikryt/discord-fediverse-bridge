/** Node-only token provisioning is intentionally independent from Python config. */
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, rm, chmod} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {test} from 'node:test';
import {readBotTokenFromFile} from '../src/token_secret.ts';

test('read-only Node secret file supplies exact token without Python transport config', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'fluxer-token-'));
  try {
    const file = join(dir, 'bot-token');
    await writeFile(file, 'opaque.token-012345\n', {mode: 0o400});
    await chmod(file, 0o400);
    assert.equal(await readBotTokenFromFile(file), 'opaque.token-012345');
  } finally {
    await rm(dir, {recursive: true, force: true});
  }
});

test('invalid or missing Node secret fails without echoing credential or path', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'fluxer-token-'));
  try {
    const file = join(dir, 'sensitive-filename');
    for (const content of ['', 'token with spaces', 'token\nmore', ' \n', 'x'.repeat(4097)]) {
      await writeFile(file, content);
      await assert.rejects(readBotTokenFromFile(file), error => {
        assert.doesNotMatch(error.message, /sensitive-filename|token with spaces|more|x{10}/);
        return true;
      });
    }
    await rm(file);
    await assert.rejects(readBotTokenFromFile(file), /unavailable or invalid/);
    await assert.rejects(readBotTokenFromFile('relative-file'), /must be absolute/);
  } finally {
    await rm(dir, {recursive: true, force: true});
  }
});
