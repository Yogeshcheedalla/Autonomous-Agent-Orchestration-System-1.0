import React from 'react';
import AppLayout from '@/components/AppLayout';
import ConnectionsDirectory from '@/components/integrations/ConnectionsDirectory';

/**
 * One connect surface, at one path.
 *
 * There used to be two. `/channel-integrations` was in the main sidebar as
 * "Channels" and rendered a 1069-line view over `/api/social/*`; the real
 * registry — twenty-seven apps across four connection strategies, live per-app
 * reachability, one-click OAuth — was mounted at `/api-keys` and reachable only
 * from a footer link labelled "API Keys". So the first thing anyone looking for
 * app connections found was the old one, which is what was reported: *"what is
 * this? It's still the old one, not the real one"* and *"please look for the app
 * connection. It is not there, actually."*
 *
 * Both old paths now redirect here (see `next.config.mjs`), because deleting a
 * route that shipped is how you turn someone's bookmark into a 404.
 *
 * What renders here is now `ConnectionsDirectory`, not the card grid it replaced:
 * a fixed list of twenty-seven cards could not answer *"all apps which have windows
 * apps can connect by clicking once ... can also in one click authenticate any
 * website"*, because the answer is not a longer list — it is a scan and a URL box.
 */
export default function ConnectionsPage() {
  return (
    <AppLayout activePath="/connections">
      <div className="flex-1 overflow-y-auto scrollbar-thin bg-[radial-gradient(circle_at_top,_rgba(108,71,255,0.18),_transparent_30%),linear-gradient(180deg,_#020617_0%,_#0f172a_100%)] px-4 py-6 pb-16 lg:px-8 lg:py-10">
        <div className="mx-auto w-full max-w-[1400px]">
          <ConnectionsDirectory />
        </div>
      </div>
    </AppLayout>
  );
}
