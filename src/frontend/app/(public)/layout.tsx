export default function PublicLayout({ children }: { children: React.ReactNode }) {
  const year = new Date().getFullYear();

  return (
    <div className="flex min-h-screen flex-col bg-muted/40">
      <header className="flex h-14 items-center border-b bg-background px-4">
        <span className="text-base font-semibold tracking-tight">DataSpoke</span>
      </header>

      <main className="flex flex-1 items-center justify-center p-4">
        <div className="w-full max-w-md">{children}</div>
      </main>

      <footer className="border-t py-3 text-center text-xs text-muted-foreground">
        DataSpoke &copy; {year}
      </footer>
    </div>
  );
}
