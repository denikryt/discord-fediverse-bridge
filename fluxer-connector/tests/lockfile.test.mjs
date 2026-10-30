/** Missing or mismatched release locks must fail rather than skip the image. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {checkLock} from '../scripts/check-lock.mjs';

test('checked-in lock validates and missing or drifted locks fail', () => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  checkLock(root);
  const temporary = mkdtempSync(join(tmpdir(), 'fluxer-lock-'));
  try {
    writeFileSync(join(temporary, 'package.json'), readFileSync(join(root, 'package.json')));
    assert.throws(() => checkLock(temporary));
    const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
    lock.packages['node_modules/@fluxerjs/core'].version = '0.0.0';
    writeFileSync(join(temporary, 'package-lock.json'), JSON.stringify(lock));
    assert.throws(() => checkLock(temporary));
  } finally {
    rmSync(temporary, {recursive: true, force: true});
  }
});
