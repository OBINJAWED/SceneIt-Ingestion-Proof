import { AudioLines, Eye, Layers, Loader2, Search, ShieldAlert } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

type SearchMode = 'both' | 'visual' | 'audio';

interface SceneSearchFormProps {
  query: string;
  onQueryChange: (query: string) => void;
  modality: SearchMode;
  onModalityChange: (mode: SearchMode) => void;
  onSubmit: (event: React.FormEvent) => void;
  ready: boolean;
  pending: boolean;
  quotaReached: boolean;
}

const modes = [
  { value: 'both', label: 'Visual + audio', icon: Layers },
  { value: 'visual', label: 'Visual', icon: Eye },
  { value: 'audio', label: 'Audio', icon: AudioLines },
] as const;

export function SceneSearchForm({
  query, onQueryChange, modality, onModalityChange, onSubmit, ready, pending, quotaReached,
}: SceneSearchFormProps) {
  return (
    <section className="rounded-xl border bg-card p-4 sm:p-6" aria-labelledby="search-heading">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="search-heading" className="text-base font-semibold">Search scenes</h2>
        <p className="text-xs text-muted-foreground">Describe what you see, hear, or remember.</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="flex flex-col gap-3 md:flex-row">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-4 top-4 size-5 text-muted-foreground" aria-hidden="true" />
            <label htmlFor="demo-scene-query" className="sr-only">Describe a scene or event</label>
            <Input
              id="demo-scene-query"
              type="search"
              placeholder="For example, someone opening a door"
              value={query}
              onChange={event => onQueryChange(event.target.value)}
              disabled={!ready || pending}
              className="h-13 pl-12 pr-4 text-base"
              aria-describedby="demo-search-privacy"
            />
          </div>
          <Button type="submit" disabled={!ready || pending || quotaReached || !query.trim()} className="h-13 px-7">
            {pending ? <><Loader2 className="size-4 animate-spin" /> Searching…</> : <><Search /> Search scenes</>}
          </Button>
        </div>
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div role="group" aria-label="Search mode" className="flex w-fit max-w-full flex-wrap gap-1 rounded-lg border bg-background p-1">
            {modes.map(({ value, label, icon: Icon }) => (
              <button key={value} type="button" aria-pressed={modality === value}
                disabled={!ready || pending} onClick={() => onModalityChange(value)}
                className={cn(
                  'flex min-h-11 items-center gap-2 rounded-md px-3 text-xs font-medium transition-colors disabled:opacity-50',
                  modality === value ? 'bg-primary/15 text-primary ring-1 ring-inset ring-primary/35' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                )}>
                <Icon className="size-3.5" aria-hidden="true" /> {label}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">Try a prompt</span>
            {['someone opening a door', 'loud explosion', 'creepy hallway'].map(prompt => (
              <button key={prompt} type="button" disabled={!ready || pending}
                onClick={() => onQueryChange(prompt)}
                className="min-h-11 rounded-full border px-3 py-2 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:opacity-50">
                {prompt}
              </button>
            ))}
          </div>
        </div>
        <p id="demo-search-privacy" className="flex items-start gap-2 text-xs leading-relaxed text-muted-foreground">
          <ShieldAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          Searches are saved to this shared demo. Do not enter private information.
        </p>
        {quotaReached && <p role="status" className="rounded-lg border border-amber-400/25 bg-amber-400/5 p-3 text-sm text-amber-300">
          This demo’s search allowance is used up. You can still reopen saved searches and download the evidence.
        </p>}
      </form>
    </section>
  );
}