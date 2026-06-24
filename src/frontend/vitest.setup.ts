import "@testing-library/jest-dom";

// The jsdom build resolved for the Vitest runner does not implement the Web
// Storage API, so `localStorage` / `sessionStorage` are `undefined` and any
// component test that touches storage throws. Install a spec-faithful in-memory
// polyfill when storage is absent. Methods live on the class prototype (a normal
// `class`, not arrow-function instance fields) so that tests spying on
// `Storage.prototype.setItem` intercept calls made on instances.
if (typeof globalThis.localStorage === "undefined") {
  class MemoryStorage implements Storage {
    private store = new Map<string, string>();

    get length(): number {
      return this.store.size;
    }

    clear(): void {
      this.store.clear();
    }

    getItem(key: string): string | null {
      const value = this.store.get(String(key));
      return value === undefined ? null : value;
    }

    key(index: number): string | null {
      return Array.from(this.store.keys())[index] ?? null;
    }

    removeItem(key: string): void {
      this.store.delete(String(key));
    }

    setItem(key: string, value: string): void {
      this.store.set(String(key), String(value));
    }
  }

  const g = globalThis as typeof globalThis & {
    Storage: typeof Storage;
    localStorage: Storage;
    sessionStorage: Storage;
  };

  g.Storage = MemoryStorage as unknown as typeof Storage;
  g.localStorage = new MemoryStorage();
  g.sessionStorage = new MemoryStorage();

  if (typeof window !== "undefined") {
    const w = window as Window & typeof globalThis;
    w.Storage = MemoryStorage as unknown as typeof Storage;
    w.localStorage = new MemoryStorage();
    w.sessionStorage = new MemoryStorage();
  }
}
