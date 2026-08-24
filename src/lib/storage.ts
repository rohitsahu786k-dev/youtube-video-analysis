import { copyFile, mkdir, readFile, rm } from "node:fs/promises";
import path from "node:path";
import { env } from "./config";

export interface StorageProvider {
  put(localPath: string, key: string): Promise<string>;
  read(key: string): Promise<Buffer>;
  delete(key: string): Promise<void>;
  url(key: string): string;
}

export class LocalStorageProvider implements StorageProvider {
  constructor(private readonly root = path.resolve(env.STORAGE_PATH, "evidence")) {}

  private resolve(key: string) {
    const normalized = key.replace(/\\/g, "/").replace(/^\/+/, "");
    const target = path.resolve(this.root, normalized);
    if (!target.startsWith(this.root + path.sep) && target !== this.root) throw new Error("Invalid storage key");
    return target;
  }

  async put(localPath: string, key: string) {
    const target = this.resolve(key);
    await mkdir(path.dirname(target), { recursive: true });
    await copyFile(localPath, target);
    return key;
  }

  read(key: string) {
    return readFile(this.resolve(key));
  }

  async delete(key: string) {
    await rm(this.resolve(key), { force: true });
  }

  url(key: string) {
    return `/api/storage/${encodeURIComponent(key)}`;
  }
}

export interface S3StorageProvider extends StorageProvider {}
export interface SupabaseStorageProvider extends StorageProvider {}

export function storageProvider(): StorageProvider {
  return new LocalStorageProvider();
}
