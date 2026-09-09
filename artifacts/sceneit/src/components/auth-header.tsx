import { useAuth } from '@workspace/replit-auth-web';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Video, LogIn, LogOut, Loader2, CreditCard } from 'lucide-react';
import { Link } from 'wouter';
import { useGetImportConfig } from '@workspace/api-client-react';
import { canReadPrivate } from '@/lib/private-access';
import { useState } from 'react';

export function AuthHeader() {
  const auth = useAuth();
  const { user, isAuthenticated, isLoading: authLoading, login, logout } = auth;
  const [signingOut, setSigningOut] = useState(false);
  const attemptLogout = async () => {
    setSigningOut(true);
    try { await (auth.signoutUnconfirmed ? auth.retrySignout() : logout()); }
    finally { setSigningOut(false); }
  };

  const { data: config } = useGetImportConfig({
    query: {
      queryKey: ['/api/imports/config'],
      staleTime: 30000,
      enabled: canReadPrivate(auth),
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
          {auth.pilotAdmitted && <Link href="/demo" className="hover:text-foreground transition-colors focus-visible:outline-none focus-visible:underline rounded-sm">View Demo</Link>}
          {isAuthenticated && <Link href="/billing" data-testid="link-billing" className="inline-flex items-center gap-1 hover:text-foreground transition-colors focus-visible:outline-none focus-visible:underline rounded-sm"><CreditCard className="size-3.5" /> Billing</Link>}
          {!auth.pilotAdmitted && <span className="text-xs">Private video search</span>}
        </div>
      </div>

      <div className="flex max-w-full flex-wrap items-center gap-3">
        {config && !config.workerAvailable && (
          <Badge variant="outline" className="border-yellow-600/50 text-yellow-500 bg-yellow-950/10 font-medium">
            Worker Unavailable
          </Badge>
        )}
        {auth.signoutUnconfirmed ? (
          <div className="flex flex-wrap items-center gap-2">
            <span role="status" className="text-xs text-muted-foreground">Private access cleared. Sign out not confirmed.</span>
            <Button variant="outline" onClick={attemptLogout} disabled={signingOut}>Retry sign out</Button>
          </div>
        ) : authLoading ? (
          <Loader2 className="size-4 animate-spin text-muted-foreground" />
        ) : isAuthenticated ? (
          <div className="flex min-w-0 max-w-full items-center gap-3">
            <span className="max-w-36 truncate text-sm font-medium text-muted-foreground">
              {user?.firstName || 'User'}
            </span>
            <Button variant="secondary" onClick={attemptLogout} disabled={signingOut} className="font-medium">
              <LogOut className="size-4 mr-2" /> Log out
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" asChild><Link href="/auth?mode=signin"><LogIn className="size-4 mr-2" /> Email sign in</Link></Button>
            <Button variant="ghost" onClick={login} className="font-medium">Replit pilot</Button>
          </div>
        )}
      </div>
    </header>
  );
}
