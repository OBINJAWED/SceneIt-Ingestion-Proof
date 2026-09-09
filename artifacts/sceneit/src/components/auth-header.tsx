import { useAuth } from '@workspace/replit-auth-web';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Video, LogIn, LogOut, Loader2 } from 'lucide-react';
import { Link } from 'wouter';
import { useGetImportConfig } from '@workspace/api-client-react';

export function AuthHeader() {
  const { user, isAuthenticated, isLoading: authLoading, login, logout } = useAuth();

  const { data: config } = useGetImportConfig({
    query: {
      queryKey: ['/api/imports/config'],
      staleTime: Infinity,
    }
  });

  return (
    <header className="border-b border-border/60 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80 sticky top-0 z-10 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 px-6 py-4">
      <div>
        <Link href="/" className="inline-block group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm">
          <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2.5 transition-colors group-hover:text-primary">
            <Video className="size-5 text-primary" />
            <span>Scene<span className="font-light text-muted-foreground">It</span></span>
          </h1>
        </Link>
        <div className="flex items-center gap-3 mt-1.5 text-sm text-muted-foreground font-medium">
          <Link href="/demo" className="hover:text-foreground transition-colors focus-visible:outline-none focus-visible:underline rounded-sm">View Demo</Link>
        </div>
      </div>

      <div className="flex max-w-full flex-wrap items-center gap-3">
        {config && !config.workerAvailable && (
          <Badge variant="outline" className="border-yellow-600/50 text-yellow-500 bg-yellow-950/10 font-medium">
            Worker Unavailable
          </Badge>
        )}
        {authLoading ? (
          <Loader2 className="size-4 animate-spin text-muted-foreground" />
        ) : isAuthenticated ? (
          <div className="flex min-w-0 max-w-full items-center gap-3">
            <span className="max-w-36 truncate text-sm font-medium text-muted-foreground">
              {user?.firstName || 'User'}
            </span>
            <Button variant="secondary" onClick={logout} className="font-medium">
              <LogOut className="size-4 mr-2" /> Log out
            </Button>
          </div>
        ) : (
          <Button variant="secondary" onClick={login} className="font-medium">
            <LogIn className="size-4 mr-2" /> Log in
          </Button>
        )}
      </div>
    </header>
  );
}
