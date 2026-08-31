/**
 * Unreachable, and deliberately so.
 *
 * This file used to be the redirect itself -- `redirect('/chat-interface')` in the
 * component body -- and it did not work: a request to `/` fell through to the
 * not-found page and answered 404. The redirect now lives in next.config.mjs,
 * where it is resolved before routing reaches any page.
 *
 * The file stays because the App Router needs a `page.tsx` for the `/` segment to
 * exist as a route at all, and because deleting it would make `/` depend solely on
 * a config entry with nothing in the route tree to point at. It renders nothing:
 * anyone who somehow arrives here has already bypassed the redirect.
 */
export default function RootPage() {
  return null;
}
