export interface StorageProvider {
  put(key: string, sourcePath: string): Promise<{ key: string; url?: string }>;
  remove(key: string): Promise<void>;
  exists(key: string): Promise<boolean>;
}
