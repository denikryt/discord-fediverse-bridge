import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import {
  exportJwk,
  generateCryptoKeyPair,
  importJwk,
} from "@fedify/fedify";

interface StoredKeyPair {
  privateKey: JsonWebKey;
  publicKey: JsonWebKey;
}

type StoredKeys = Record<string, StoredKeyPair>;

export class FileKeyStore {
  // The gateway uses one simple file-backed key store so actor keys survive
  // restarts without requiring external infrastructure.
  constructor(private readonly path: string) {}

  async getOrCreate(identifier: string): Promise<CryptoKeyPair> {
    const stored = await this.readAll();
    const existing = stored[identifier];
    if (existing) {
      return {
        privateKey: await importJwk(existing.privateKey, "private"),
        publicKey: await importJwk(existing.publicKey, "public"),
      };
    }

    const generated = await generateCryptoKeyPair("RSASSA-PKCS1-v1_5");
    stored[identifier] = {
      privateKey: await exportJwk(generated.privateKey),
      publicKey: await exportJwk(generated.publicKey),
    };
    await this.writeAll(stored);
    return generated;
  }

  private async readAll(): Promise<StoredKeys> {
    try {
      const contents = await readFile(this.path, "utf8");
      const parsed = JSON.parse(contents) as StoredKeys;
      return parsed;
    } catch (error) {
      if (isNotFound(error)) {
        return {};
      }
      throw error;
    }
  }

  private async writeAll(keys: StoredKeys): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true });
    await writeFile(this.path, JSON.stringify(keys, null, 2));
  }
}

function isNotFound(error: unknown): error is NodeJS.ErrnoException {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    error.code === "ENOENT"
  );
}
