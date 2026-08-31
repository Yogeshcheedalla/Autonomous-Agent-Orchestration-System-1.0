import { imageHosts } from './image-hosts.config.mjs';

/** @type {import('next').NextConfig} */
const nextConfig = {
  productionBrowserSourceMaps: false,
  distDir: process.env.DIST_DIR || '.next',
  // Both gates below were open, which meant a type error or a lint error could
  // ship to production silently. `tsc --noEmit` and `next lint` are both clean
  // now (0 errors, 0 warnings), so the build is the thing that enforces it from
  // here on.
  typescript: {
    ignoreBuildErrors: false,
  },
  eslint: {
    ignoreDuringBuilds: false,
  },
  images: {
    remotePatterns: imageHosts,
    minimumCacheTTL: 60,
  },
  webpack(config, { dev }) {
    if (dev) {
      const ignoredPaths = (process.env.WATCH_IGNORED_PATHS || '')
        .split(',')
        .map((p) => p.trim())
        .filter(Boolean);
      config.watchOptions = {
        ignored: ignoredPaths.length
          ? ignoredPaths.map((p) => `**/${p.replace(/^\/+|\/+$/g, '')}/**`)
          : undefined,
      };
    }
    return config;
  },
  // The app's front door. `src/app/page.tsx` called `redirect('/chat-interface')`
  // from the root server component and it did not take -- a request to `/` fell
  // through to the not-found page and answered 404, so the bare origin was a dead
  // link. Handled here instead: a config redirect is resolved before routing, so
  // there is no page for the request to fall out of.
  async redirects() {
    return [
      {
        source: '/',
        destination: '/chat-interface',
        permanent: false,
      },
      // Three routes were removed and their working parts moved. Redirects rather
      // than deletions, because a path that shipped is somebody's bookmark:
      //   - the two connect pages collapsed into one real registry at /connections
      //   - /browser-automation's features moved into the chat interface, where the
      //     conversation that triggers most of them already lives
      { source: '/api-keys', destination: '/connections', permanent: false },
      { source: '/channel-integrations', destination: '/connections', permanent: false },
      { source: '/browser-automation', destination: '/chat-interface', permanent: false },
    ];
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://127.0.0.1:8000/api/:path*',
      },
    ];
  },
};
export default nextConfig;
