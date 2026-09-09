import { useEffect, useRef, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, Route, Switch, useLocation, Router as WouterRouter } from 'wouter';
import { ArrowLeft, Film } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import Home from '@/pages/home';
import ImportsIndex from '@/pages/index';
import SingleImport from '@/pages/import';
import AuthPage from '@/pages/auth';
import AuthActionPage from '@/pages/auth-action';
import BillingPage from '@/pages/billing';
import { useAuth } from '@workspace/replit-auth-web';
import { isPilotAllowed } from '@/lib/polling';
import { ErrorBoundary } from '@/components/error-boundary';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function AccountStateBoundary({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const previousOwner = useRef<string | null | undefined>(undefined);
  const owner = auth.user?.id ?? null;

  useEffect(() => {
    if (previousOwner.current !== undefined && previousOwner.current !== null && previousOwner.current !== owner) {
      queryClient.clear();
      sessionStorage.removeItem('pendingImportLink');
      for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = sessionStorage.key(index);
        if (key?.startsWith('sceneit:billing:')) sessionStorage.removeItem(key);
      }
    }
    previousOwner.current = owner;
  }, [owner]);

  return <>{children}</>;
}

function PilotGate({ children }: { children: ReactNode }) {
  const auth = useAuth();

  if (auth.isLoading) {
    return <main className="min-h-screen grid place-items-center p-6" role="status">Checking pilot access…</main>;
  }
  if (auth.error) {
    return <main className="min-h-screen grid place-items-center p-6 text-center">
      <div><h1 className="text-2xl font-bold">Session check unavailable</h1>
        <p className="mt-2 text-muted-foreground">Access is closed until your pilot session can be verified.</p>
        <Button className="mt-4" onClick={() => window.location.reload()}>Refresh</Button></div>
    </main>;
  }
  if (!auth.isAuthenticated) {
    return <main className="min-h-screen grid place-items-center p-6 text-center">
      <div><h1 className="text-2xl font-bold">SceneIt controlled pilot</h1>
        <p className="mt-2 text-muted-foreground">Sign in with an admitted pilot account to continue.</p>
        <Button className="mt-4" onClick={auth.login}>Sign in with Replit</Button></div>
    </main>;
  }
  if (!isPilotAllowed({ user: auth.user, pilotAdmitted: auth.pilotAdmitted })) {
    return <main className="min-h-screen grid place-items-center p-6 text-center">
      <div><h1 className="text-2xl font-bold">Pilot access required</h1>
        <p className="mt-2 text-muted-foreground">This signed-in account has not been admitted to the controlled pilot.</p>
        <Button className="mt-4" variant="outline" onClick={auth.logout}>Sign out</Button></div>
    </main>;
  }
  return <div key={`${auth.user?.id}:${auth.csrfToken}:admitted`}>{children}</div>;
}

function Router() {
  const [location] = useLocation();
  useEffect(() => {
    document.title = location === '/' ? 'SceneIt — Private video search'
      : location === '/demo' ? 'SceneIt — Scene search demo'
      : location.startsWith('/imports/') ? 'SceneIt — Private analysis'
      : location === '/billing' ? 'SceneIt — Membership & billing'
      : location.startsWith('/auth') ? 'SceneIt — Authentication'
      : 'SceneIt — Page not found';
  }, [location]);

  return (
    <Switch>
      <Route path="/" component={ImportsIndex} />
      <Route path="/auth" component={AuthPage} />
      <Route path="/auth/action" component={AuthActionPage} />
      <Route path="/billing" component={BillingPage} />
      <Route path="/imports/:id" component={SingleImport} />
      <Route path="/demo">
        <PilotGate><Home /></PilotGate>
      </Route>
      <Route>
        <main className="min-h-[100dvh] flex items-center justify-center bg-background p-6">
          <section className="w-full max-w-lg rounded-2xl border bg-card p-8 text-center sm:p-12">
            <div className="mx-auto mb-6 flex size-14 items-center justify-center rounded-2xl bg-primary/10 text-primary">
              <Film className="size-7" aria-hidden="true" />
            </div>
            <p className="mb-3 text-sm text-muted-foreground">SceneIt · 404</p>
            <h1 className="text-3xl font-semibold tracking-tight">This scene is missing.</h1>
            <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
              We couldn’t find this page. Return to SceneIt to start with a video or explore the demo.
            </p>
            <Button className="mt-7" asChild>
              <Link href="/"><ArrowLeft /> Back to SceneIt</Link>
            </Button>
          </section>
        </main>
      </Route>
    </Switch>
  );
}

function App() {
  // SceneIt uses a consistent dark viewing environment.
  useEffect(() => {
    document.documentElement.classList.add('dark');
  }, []);

  return (
    <ErrorBoundary><QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <AccountStateBoundary><Router /></AccountStateBoundary>
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider></ErrorBoundary>
  );
}

export default App;
