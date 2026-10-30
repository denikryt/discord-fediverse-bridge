/** Fail closed when the release package lacks a matching immutable npm lock. */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';

/** Validate package identity and direct pinned dependencies before npm ci. */
export function checkLock(root) {
  const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
  const lock = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8'));
  assert.equal(lock.lockfileVersion, 3);
  assert.equal(lock.name, pkg.name);
  assert.equal(lock.version, pkg.version);
  assert.equal(lock.packages[''].version, pkg.version);
  for (const field of ['dependencies', 'devDependencies', 'engines']) {
    assert.deepEqual(lock.packages[''][field], pkg[field]);
  }
  for (const [name, version] of Object.entries({...pkg.dependencies, ...pkg.devDependencies})) {
    assert.match(version, /^\d+\.\d+\.\d+$/);
    const entry = lock.packages[`node_modules/${name}`];
    assert.equal(entry.version, version);
    assert.ok(entry.integrity?.startsWith('sha512-'));
    assert.ok(entry.resolved?.startsWith('https://registry.npmjs.org/'));
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    checkLock(fileURLToPath(new URL('..', import.meta.url)));
  } catch {
    // A missing lock is a release error, never a reason to skip an image job.
    process.stderr.write('Fluxer package-lock.json is missing or inconsistent\n');
    process.exitCode = 1;
  }
}
