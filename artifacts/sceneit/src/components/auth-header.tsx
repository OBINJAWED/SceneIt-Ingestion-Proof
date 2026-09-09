import { useAuth } from '@workspace/replit-auth-web';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Video, LogIn, LogOut, UploadCloud, Loader2 } from 'lucide-react';
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
    <header className="border-b p-4 bg-background sticky top-0 z-10 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
      <div>
        <Link href="/" className="inline-block">
          <h1 className="text-2xl font-bold font-sans tracking-tight uppercase flex items-center gap-2 cursor-pointer hover:text-primary transition-colors">
            <Video className="size-6 text-primary" />
            SCENE<span className="text-primary">IT</span>
            <Badge variant="outline" className="ml-2 bg-primary/10 text-primary border-primary">
              BETA
            </Badge>
          </h1>
        </Link>
        <div className="flex items-center gap-3 mt-2 text-sm text-muted-foreground">
          <Link href="/demo" className="hover:text-primary transition-colors">View Demo</Link>
        </div>
      </div>

      <div className="flex items-center gap-4">
        {config && !config.workerAvailable && (
          <Badge variant="outline" className="border-yellow-500 text-yellow-500">
            WORKER_UNAVAILABLE
          </Badge>
        )}
        {authLoading ? (
          <Loader2 className="size-4 animate-spin text-muted-foreground" />
        ) : isAuthenticated ? (
          <div className="flex items-center gap-3">
            <span className="text-sm font-mono text-muted-foreground">
              {user?.firstName || 'USER'}
            </span>
            <Button variant="outline" size="sm" onClick={logout}>
              <LogOut className="size-4 mr-2" /> LOG_OUT
            </Button>
          </div>
        ) : (
          <Button variant="outline" size="sm" onClick={login}>
            <LogIn className="size-4 mr-2" /> LOG_IN
          </Button>
        )}
      </div>
    </header>
  );
}